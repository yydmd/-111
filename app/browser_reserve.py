"""Official-page reservation with human verification; no captcha solving.

All Playwright objects stay on the owning reservation worker thread. Only
Events cross threads. The request gate is installed before page navigation,
and records a possible submission durably BEFORE allowing it onto the wire.
"""
from __future__ import annotations

import ctypes
import hashlib
import datetime as dt
import importlib.util
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter_ns
from urllib.parse import parse_qs, urlencode, urlparse

from . import clock
from .chaoxing_client import normalize_select_context_path, normalize_seat, EPHEMERAL_SELECT_KEYS
from .db import DATA_DIR
from .run_state import VERIFY_SECONDS, BROWSER_RUN_SECONDS
from .security import protect_secret, unprotect_secret
from utils import AES_Encrypt

ORIGIN = "https://office.chaoxing.com"
ACCOUNT_URL = "https://passport2.chaoxing.com/mooc/accountManage"
logger = logging.getLogger(__name__)
SUBMIT_PATH = "/data/apps/seat/submit"
READ_SEAT_PATHS = {"index", "room/info", "room/info/switch", "getusedtimes", "getusedseatnums", "address", "captcha"}
REQUEST_GATE_PATTERNS = (
    "https://passport2.chaoxing.com/fanyalogin**",
    re.compile(r"^https://office\.chaoxing\.com/(?:.*/)?data/apps/seat(?:/.*)?(?:\?.*)?$"),
)
RATE_LIMIT_BACKOFF_MS = 500
# The platform's own 303 remedy: "you sat on the page too long, refresh and
# resubmit". Unlike a risk verdict this is an explicit refusal that asks for a
# resubmit, so re-sending the same seat after a fresh page load cannot double
# book — the same reasoning the HTTP path already applies to TOKEN_STALE
# (app/service.py:39-42). Checked BEFORE the human-challenge words because the
# platform's wording also contains 安全验证.
REFRESH_REJECTION_WORDS = ("刷新后再提交", "刷新页面后重试", "请刷新后重试", "请刷新页面",
                           "页面停留过久", "安全验证已超时")
# One automatic refresh-and-resubmit per seat; a persistently stale page then
# hands over to the remaining candidates instead of hammering.
REFRESH_RETRY_LIMIT = 1


@dataclass
class BrowserResult:
    status: str
    code: str | None
    message: str
    seat: str | None = None
    attempt_timings: list[dict] = field(default_factory=list)


@dataclass
class Control:
    cancel: threading.Event = field(default_factory=threading.Event)
    focus: threading.Event = field(default_factory=threading.Event)
    awaiting_identity: str | None = None
    confirmed_identity: str | None = None


def _browser_target_closed(page, exc: BaseException | None = None) -> bool:
    """Recognise a dead Playwright target without letting the probe mask it."""
    if exc is not None and exc.__class__.__name__ == "TargetClosedError":
        return True
    if page is None:
        return False
    try:
        return bool(page.is_closed())
    except Exception:
        # Once the browser/context connection is gone, even is_closed() may
        # raise.  That is still a closed-target condition, not a page-layout
        # incompatibility.
        return True


_controls: dict[int, Control] = {}
_control_lock = threading.Lock()


def command(run_id: int, action: str) -> bool:
    with _control_lock:
        control = _controls.get(run_id)
        if control and action == "confirm_account":
            if not control.awaiting_identity or control.cancel.is_set():
                return False
            control.confirmed_identity = control.awaiting_identity
            return True
        if not control or action not in {"focus", "cancel"}:
            return False
        getattr(control, action).set()
        return True


def cancel_all() -> None:
    with _control_lock:
        for control in _controls.values():
            control.cancel.set()


def desktop_available() -> bool:
    if os.name != "nt":
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    handle = user32.OpenInputDesktop(0, False, 0x0100)
    if not handle:
        return False
    try:
        return bool(user32.SwitchDesktop(handle))
    finally:
        user32.CloseDesktop(handle)


def browser_channel() -> str | None:
    """Prefer the maintained system browser on Windows, with a separate profile."""
    if os.name == "nt":
        for channel, suffix in (("msedge", "Microsoft/Edge/Application/msedge.exe"),
                                ("chrome", "Google/Chrome/Application/chrome.exe")):
            for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
                root = os.environ.get(variable)
                if root and (Path(root) / suffix).is_file():
                    return channel
    return None


def launch_context(pw, profile):
    # Human-operated windows must follow native resizing. A fixed mobile-size
    # viewport clips the desktop login form even when the window is maximized.
    options = {"headless": False, "no_viewport": True, "chromium_sandbox": True,
               "service_workers": "block", "args": ["--window-size=1200,900"]}
    channel = browser_channel()
    if channel:
        options["channel"] = channel
    return pw.chromium.launch_persistent_context(str(profile), **options)


def install_request_gate(context, gate) -> None:
    """Intercept only login and seat mutations, leaving risk/static traffic native."""
    for pattern in REQUEST_GATE_PATTERNS:
        context.route(pattern, gate.route)


def remove_request_gate(context, gate) -> None:
    for pattern in REQUEST_GATE_PATTERNS:
        context.unroute(pattern, gate.route)


def window_state(page, foreground=False):
    """Operate only the owned browser window; never click other applications."""
    try:
        cdp = page.context.new_cdp_session(page)
        window = cdp.send('Browser.getWindowForTarget')
        cdp.send('Browser.setWindowBounds', {'windowId': window['windowId'],
                 'bounds': {'windowState': 'normal' if foreground else 'minimized'}})
        cdp.detach()
        if foreground:
            page.bring_to_front()
            _flash_owned_window(page)
    except Exception:
        if foreground and not page.is_closed():
            page.bring_to_front()


def _raise_native_window(user32, hwnd, topmost, notopmost):
    """Restore an owned browser window even when Windows rejects activation.

    Background processes are sometimes refused by ``SetForegroundWindow``.
    In that case a short topmost/not-topmost bounce still makes the verification
    window visible without leaving it permanently above the user's other apps.
    """
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    if user32.SetForegroundWindow(hwnd):
        return True
    flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd, topmost, 0, 0, 0, 0, flags)
    user32.SetWindowPos(hwnd, notopmost, 0, 0, 0, 0, flags)
    user32.BringWindowToTop(hwnd)
    return bool(user32.SetForegroundWindow(hwnd))


def _flash_owned_window(page):
    if os.name != 'nt':
        return
    try:
        # Obtain the PID from this Playwright-owned browser, never by matching
        # another user's window title or enumerating unrelated browser profiles.
        session = page.context.browser.new_browser_cdp_session()
        processes = session.send('SystemInfo.getProcessInfo')['processInfo']
        session.detach()
        browser_pid = next(int(p['id']) for p in processes if p['type'] == 'browser')
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32')
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [('cbSize',wintypes.UINT),('hwnd',wintypes.HWND),('dwFlags',wintypes.DWORD),
                        ('uCount',wintypes.UINT),('dwTimeout',wintypes.DWORD)]
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND,ctypes.c_int]
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetWindowPos.argtypes = [wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,
                                       ctypes.c_int,ctypes.c_int,wintypes.UINT]
        user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
        topmost, notopmost = wintypes.HWND(-1), wintypes.HWND(-2)
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            if pid.value == browser_pid and user32.IsWindowVisible(hwnd):
                focused = _raise_native_window(user32, hwnd, topmost, notopmost)
                if not focused:
                    # Keep flashing until the user opens the window. Three
                    # short flashes were easy to miss during an opening race.
                    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO),hwnd,0x0000000F,0,0)
                    user32.FlashWindowEx(ctypes.byref(info))
                return False
            return True
        callback = callback_type(visit)
        user32.EnumWindows(callback,0)
    except Exception:
        pass  # Chromium focus plus local sound remain available.


def official_account_page(page):
    parsed = urlparse(page.url)
    return (parsed.hostname, parsed.path) == ('passport2.chaoxing.com', '/mooc/accountManage') and page.locator('#phone[name=user_mobile]').count() == 1


def readiness() -> dict:
    spec = importlib.util.find_spec("playwright")
    if spec is None:
        return {"ready": False, "message": "尚未安装浏览器组件；请运行 scripts/install_browser.ps1"}
    try:
        # Read the installed package's own revision manifest. Starting a driver
        # solely for a status check can leak an initializing asyncio task.
        package = Path(spec.origin).parent / "driver" / "package"
        manifest = json.loads((package / "browsers.json").read_text(encoding="utf-8"))
        revision = next(b["revision"] for b in manifest["browsers"] if b["name"] == "chromium")
        configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        root = (package / ".local-browsers" if configured == "0" else Path(configured)) if configured else (
            Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright" if os.name == "nt" else Path.home() / ".cache" / "ms-playwright")
        folder = root / f"chromium-{revision}"
        installed = (folder / "chrome-win64" / "chrome.exe").exists() if os.name == "nt" else any(folder.glob("**/chrome"))
        channel = browser_channel()
        if not installed and not channel:
            return {"ready": False, "message": "Chromium 未安装；请运行 scripts/install_browser.ps1"}
        if not desktop_available():
            return {"ready": False, "message": "请登录并解锁运行程序的 Windows 桌面"}
        name = {"msedge": "Microsoft Edge", "chrome": "Google Chrome"}.get(channel, "Chromium")
        return {"ready": True, "message": f"浏览器组件就绪（{name}）；可检查账号登录并等待自动预约", "channel": channel or "chromium"}
    except Exception:
        return {"ready": False, "message": "浏览器组件无法启动；请重新运行 scripts/install_browser.ps1"}


def select_url(values: dict, day: str, seat: str) -> str:
    path = normalize_select_context_path(values.get("select_context_path") or "/front/third/apps/seat/select")
    params = {k: v for k, v in (values.get("select_params") or {}).items()
              if k.lower() not in EPHEMERAL_SELECT_KEYS}
    params.update(id=values["room_id"], day=day, seatNum=seat)
    return ORIGIN + path + "?" + urlencode(params)


def account_identity(cookies: list[dict], username: str) -> str | None:
    # Passport sets _uid immediately; oa_uid may only appear once an office
    # application page actually opens. An unopened reservation date has no oa_uid.
    uids = {c["value"] for c in cookies if c["name"] in {"oa_uid", "_uid"} and c["value"]}
    if len(uids) != 1 or not username:
        return None
    return hashlib.sha256((username + "|" + next(iter(uids))).encode()).hexdigest()


def save_browser_session(context, profile, username, expected_identity):
    """Chromium may discard session cookies on exit, even in persistent profiles."""
    if not expected_identity or account_identity(context.cookies(ORIGIN), username) != expected_identity:
        return False
    cookies = [c for c in context.cookies() if c["domain"].lstrip(".") == "chaoxing.com"
               or c["domain"].lstrip(".").endswith(".chaoxing.com")]
    encrypted = protect_secret(json.dumps({"identity": expected_identity, "cookies": cookies}))
    temporary = profile / "session.dpapi.tmp"
    temporary.write_text(encrypted, encoding="utf-8")
    temporary.replace(profile / "session.dpapi")
    return True


def restore_browser_session(context, profile, username, expected_identity):
    if not expected_identity or account_identity(context.cookies(ORIGIN), username):
        return False  # Never replace a different live account with saved cookies.
    try:
        encrypted = (profile / "session.dpapi").read_text(encoding="utf-8")
        if not encrypted.startswith("dpapi:v1:"):
            return False
        saved = json.loads(unprotect_secret(encrypted))
        cookies = saved["cookies"]
        if (saved.get("identity") != expected_identity or
                account_identity(cookies, username) != expected_identity or
                any(c["domain"].lstrip(".") != "chaoxing.com" and
                    not c["domain"].lstrip(".").endswith(".chaoxing.com") for c in cookies)):
            return False
        context.add_cookies(cookies)
        return True
    except Exception:
        return False  # Corrupt/another Windows user's store requires a new login.


def opening_notice(page) -> bool:
    if urlparse(page.url).hostname != "office.chaoxing.com":
        return False
    body = page.locator("body").inner_text(timeout=2000)
    return any(text in body for text in ("当前区域未到开放预约时间", "该区域未到开放预约时间"))


def reservation_match(
    items: list,
    values: dict,
    day: str,
    previous_items: list | None = None,
) -> tuple[str, str | None, str]:
    """Confirm only an exact official record for the requested interval.

    A wider envelope shown by the reservation list is not proof that every
    half-hour inside it was actually booked. It normally needs review. One
    exception is a post-submit record that exactly extends an official record
    captured immediately before submission by this run: the before/after pair
    proves that the requested adjacent interval was merged by the platform.
    """
    tz = dt.timezone(dt.timedelta(hours=8))
    start = dt.datetime.fromisoformat(day + "T" + values["start_time"]).replace(tzinfo=tz)
    end = dt.datetime.fromisoformat(day + "T" + values["end_time"]).replace(tzinfo=tz)
    def parse(item):
        if not isinstance(item, dict):
            raise ValueError("bad item")
        a = dt.datetime.fromtimestamp(int(item["startTime"]) / 1000, tz)
        b = dt.datetime.fromtimestamp(int(item["endTime"]) / 1000, tz)
        item_day = str(item["today"])
        seat = normalize_seat(str(item["seatNum"]))
        room = str(item["roomId"])
        if b <= a or a.date().isoformat() != item_day:
            raise ValueError("bad interval")
        return a, b, item_day, seat, room
    try:
        previous = [parse(item) for item in previous_items] if previous_items is not None else None
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return "unavailable", None, "提交前预约列表缺少日期、座位或有效起止时间"
    exact, conflicts = [], []
    for item in items:
        try:
            a, b, item_day, seat, room = parse(item)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            return "unavailable", None, "预约列表缺少日期、座位或有效起止时间"
        if a < end and b > start:
            description = f"{item_day}，阅览室 {room}，座位 {seat}，{a:%H:%M}–{b:%H:%M}"
            if (item_day == day and a == start and b == end and
                    room == values["room_id"] and seat in values["seats"]):
                exact.append((seat, description))
            elif (previous is not None and item_day == day and room == values["room_id"]
                    and seat in values["seats"] and a <= start and b >= end
                    and (a < start or b > end)):
                adjacent = [(pa, pb) for pa, pb, pd, ps, pr in previous
                            if pd == day and ps == seat and pr == room
                            and (pb <= start or pa >= end)]
                previous_overlap = any(
                    pd == day and ps == seat and pr == room and pa < end and pb > start
                    for pa, pb, pd, ps, pr in previous
                )
                left_proven = a == start or any(pa == a and pb == start for pa, pb in adjacent)
                right_proven = b == end or any(pa == end and pb == b for pa, pb in adjacent)
                if not previous_overlap and left_proven and right_proven:
                    exact.append((seat, description +
                                  f"（由提交前相邻预约与本次 {start:%H:%M}–{end:%H:%M} 合并）"))
                else:
                    conflicts.append(description)
            else:
                conflicts.append(description)
    if conflicts or len(exact) > 1:
        return "conflict", None, "发现重叠或多条预约：" + "；".join(conflicts + [x[1] for x in exact])
    if exact:
        return "exact", exact[0][0], exact[0][1]
    return "absent", None, "尚未查到本次预约"


class RequestGate:
    def __init__(self, values, day, preview, checkpoint, control, deadline, timing_sink=None):
        self.values, self.day, self.preview = values, day, preview
        self.checkpoint, self.control, self.deadline = checkpoint, control, deadline
        self.armed_seat = None
        self.pending = False
        self.routing = False
        self.sent_seat = None
        self.attempted = set()
        self.rejected = False
        self.rejection_code = None
        self.rejection_message = ""
        self.challenge_pending = False
        self.challenge_message = ""
        self.refresh_pending = False
        self.refresh_message = ""
        # Bumped every time the platform asks for a refresh-and-resubmit. The
        # runner compares it against the value it saw before clicking, so an
        # unlock armed for the current attempt is not mistaken for a fresh
        # rejection arriving while that attempt is still on the wire.
        self.refresh_serial = 0
        self.blocked_reason = ""
        self.terminal_code = None
        self.terminal_message = ""
        self.login_ok = False
        self.login_matches = False
        self.identity_check = lambda: True
        self.attempt_timings = timing_sink if timing_sink is not None else []
        self._click_started = None

    def mark_click(self, seat: str) -> None:
        self._click_started = (seat, perf_counter_ns())

    def _mark_response(self, response) -> None:
        request = getattr(response, "request", None)
        timing = next((
            item for item in self.attempt_timings
            if request is not None and item.get("_request") is request
        ), None)
        if timing is None:
            # Lightweight unit transports do not expose response.request. A
            # single outstanding request is still unambiguous; with two or
            # more, omit the metric rather than attach it to the wrong try.
            outstanding = [
                item for item in self.attempt_timings
                if "request_to_response_ms" not in item
            ]
            timing = outstanding[0] if len(outstanding) == 1 else None
        if timing is not None and "request_to_response_ms" not in timing:
            timing["request_to_response_ms"] = (perf_counter_ns() - timing["_route_finished_ns"]) / 1_000_000

    def route(self, route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.hostname == "passport2.chaoxing.com" and parsed.path == "/fanyalogin" and self.values.get("username"):
            params = parse_qs(parsed.query)
            params.update(parse_qs(request.post_data or ""))
            name = params.get("uname", [""])[0]
            self.login_matches = name in {self.values["username"], AES_Encrypt(self.values["username"])}
            if not self.login_matches:
                self.blocked_reason = "登录账号与计划账号不一致；请使用对应账号的密码登录"
                route.abort()
                return
        if parsed.hostname != "office.chaoxing.com":
            route.fallback()
            return
        path = parsed.path.rstrip("/")
        if path != SUBMIT_PATH:
            # Prevent preview/manual clicks from cancelling, signing or renewing
            # another booking. Known read-only seat endpoints use POST as well.
            if "/data/apps/seat/" in path and path.split("/data/apps/seat/", 1)[1] not in READ_SEAT_PATHS:
                route.abort()
            else:
                route.fallback()
            return
        route_started_ns = perf_counter_ns()
        click_started_ns = None
        if self._click_started is not None and self._click_started[0] == self.armed_seat:
            click_started_ns = self._click_started[1]
            # A submit callback consumes the click even when the gate blocks
            # it. It must never inflate a later candidate/challenge timing.
            self._click_started = None
        released = False
        if self.preview or self.control.cancel.is_set() or time.monotonic() >= self.deadline:
            route.abort()
            return
        # An explicit platform unlock — a completed human challenge, or the
        # platform's own "refresh and resubmit" refusal — is the only way the
        # same seat may be sent twice. ``attempted`` itself is never relaxed.
        continuation = (self.challenge_pending or self.refresh_pending) and self.armed_seat == self.sent_seat
        if (not self.armed_seat or self.pending or self.routing or
                (self.armed_seat in self.attempted and not continuation)):
            route.abort()
            return
        # Only validate non-secret intent fields. Never log or retain body data.
        # Playwright calls inside this callback can dispatch a second request
        # reentrantly. Acquire the latch before the first such call (cookies).
        self.routing = True
        try:
            if not self.identity_check():
                raise ValueError("account changed")
            params = parse_qs(parsed.query, keep_blank_values=True)
            body = request.post_data or ""
            posted = json.loads(body) if body.lstrip().startswith("{") else parse_qs(body, keep_blank_values=True)
            if not isinstance(posted, dict):
                raise ValueError("invalid body")
            for key, value in posted.items():
                values = value if isinstance(value, list) else [value]
                params.setdefault(key, []).extend(values)
            expected = {"roomId": self.values["room_id"], "day": self.day, "seatNum": self.armed_seat,
                        "startTime": self.values["start_time"], "endTime": self.values["end_time"]}
            for key, value in expected.items():
                actual = params.get(key, [])
                if len(actual) != 1:
                    raise ValueError("ambiguous intent")
                actual = normalize_seat(str(actual[0])) if key == "seatNum" else str(actual[0])
                if actual != value:
                    raise ValueError("intent changed")
            # A same-seat retry is allowed only after the official response has
            # explicitly moved this request into the human-challenge state.
            # It is the user's continuation of that challenge, not another
            # automatic candidate attempt.
            if continuation:
                self.challenge_pending = False
                self.challenge_message = ""
                self.refresh_pending = False
                self.refresh_message = ""
            # The callback checks account enablement again and commits before IO.
            if not self.checkpoint("VERIFYING", "预约请求即将发送，正在核对结果", True, self.armed_seat):
                route.abort()
                return
            self.pending = True
            self.sent_seat = self.armed_seat
            self.attempted.add(self.armed_seat)
            route.fallback()
            released = True
        except Exception:
            self.blocked_reason = "提交参数与计划不一致，或运行状态无法保存；已阻止请求"
            route.abort()
        finally:
            self.routing = False
            if released:
                route_finished_ns = perf_counter_ns()
                timing = {
                    "seat": self.sent_seat,
                    "gate_handler_ms": (route_finished_ns - route_started_ns) / 1_000_000,
                    "_route_finished_ns": route_finished_ns,
                    "_request": request,
                }
                if click_started_ns is not None:
                    timing["click_to_request_ms"] = (route_started_ns - click_started_ns) / 1_000_000
                self.attempt_timings.append(timing)

    def response(self, response):
        parsed = urlparse(response.url)
        if parsed.hostname == "passport2.chaoxing.com" and parsed.path == "/fanyalogin":
            try:
                self.login_ok = self.login_matches and response.json().get("status") is True
            except Exception:
                self.login_ok = False
            return
        if parsed.hostname != "office.chaoxing.com" or parsed.path.rstrip("/") != SUBMIT_PATH or not self.pending:
            return
        self._mark_response(response)
        try:
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("success") not in (False, "false", 0):
                return
            message = str(payload.get("msg") or payload.get("message") or "")
            if any(word in message for word in ("操作频繁", "过于频繁", "请勿频繁", "请求频繁", "稍后再试")):
                safe_message = "官方提示操作频繁；已退避并继续检查下一候选"
                if self.checkpoint("RUNNING", safe_message, False, self.sent_seat, "RATE_LIMITED"):
                    self.pending = False
                    self.rejected = True
                    self.rejection_code = "RATE_LIMITED"
                    self.rejection_message = safe_message
                else:
                    self.control.cancel.set()
                return
            if any(word in message for word in ("非法操作", "异常操作", "风险控制", "风控拦截", "访问被拒绝")):
                safe_message = "官方拒绝本次操作；已停止本次任务，不再换座或追加请求"
                if self.checkpoint("BLOCKED_BY_RISK", safe_message, False, self.sent_seat, "BLOCKED_BY_RISK"):
                    self.pending = False
                    self.terminal_code = "BLOCKED_BY_RISK"
                    self.terminal_message = safe_message
                else:
                    self.control.cancel.set()
                return
            # The platform's explicit 303 remedy: it refused the request and
            # asks for a page refresh plus a resubmit. Mirror the HTTP path's
            # TOKEN_STALE handling — refresh the page for a fresh token and
            # re-send the SAME seat once — instead of degrading to an unknown
            # outcome or a human challenge. Must be checked before the
            # human-challenge words, which this wording also contains.
            if any(word in message for word in REFRESH_REJECTION_WORDS):
                safe_message = "官方要求刷新页面后重新提交；正在重新加载页面并再提交一次"
                if self.checkpoint("RUNNING", safe_message, False, self.sent_seat, "TOKEN_STALE"):
                    self.pending = False
                    self.refresh_pending = True
                    self.refresh_message = safe_message
                    self.refresh_serial += 1
                else:
                    self.control.cancel.set()
                return
            # A challenge response proves that this request was rejected before
            # booking. Clear the durable unknown-submission flag and let the
            # user finish the official flow in the same window. Only that flow
            # may send the same seat once more.
            if any(word in message for word in ("验证码", "安全验证", "请登录", "登录后")):
                safe_message = "官方要求完成安全验证或重新确认登录，请在原窗口继续"
                if self.checkpoint("WAITING_USER", safe_message, False, self.sent_seat):
                    self.pending = False
                    self.challenge_pending = True
                    self.challenge_message = safe_message
                else:
                    self.control.cancel.set()
                return
            # Only an explicit seat rejection permits moving to another seat.
            if any(word in message for word in ("已被预约", "已被别人预约", "座位不可预约", "座位已被", "座位被占用")):
                safe_message = "官方明确返回座位不可预约，准备检查下一候选"
                if self.checkpoint("RUNNING", safe_message, False, self.sent_seat, "SEAT_UNAVAILABLE"):
                    self.pending = False
                    self.rejected = True
                    self.rejection_code = "SEAT_UNAVAILABLE"
                    self.rejection_message = safe_message
                else:
                    self.control.cancel.set()
        except Exception:
            pass  # lost/invalid response must leave pending=True


# Observed on the school's official seat_select_third.js (2026090218).
# Read selected UI state only; never set Vue state or invoke its submit/captcha methods.
PAGE_STATE = """() => {
 const v = document.querySelector('.order')?.__vue__;
 if (!v) return null;
 return {day:v.chosedDay, room:String(v.seatRoom?.id || ''), seat:String(v.chosedSeatNum || ''),
   start:v.dynamicChosedTimeInfo?.startTime, end:v.dynamicChosedTimeInfo?.endTime,
   timesShow:!!v.timesShow, ready:!!v.seatRoom?.id,
   fid:typeof fidEnc === 'string' ? fidEnc : '',
   slots:(v.dynamicTimes || []).map(t=>({time:t.time,disabled:t.cls==='noSelect'}))};
}"""


class PageAdapter:
    def __init__(self, page):
        self.page = page

    def state(self):
        if urlparse(self.page.url).hostname != "office.chaoxing.com":
            return None
        return self.page.evaluate(PAGE_STATE)

    def needs_user(self):
        # Observe a visible challenge only; never inspect/solve its contents.
        markers = self.page.get_by_text("请完成安全验证", exact=True)
        frames = self.page.locator('iframe[src*="captcha"]:visible')
        return any(markers.nth(i).is_visible() for i in range(markers.count())) or frames.count() > 0

    def prepare(self, values, day, seat):
        state = self.state()
        if not state or not state["ready"] or state["day"] != day or state["room"] != values["room_id"]:
            raise ValueError("页面未确认计划日期和阅览室")
        if normalize_seat(state["seat"]) != seat or not state["timesShow"]:
            raise ValueError("页面未显示该座位的时间选择窗口")
        slots = state["slots"]
        starts = [i for i, s in enumerate(slots) if s["time"].split("-")[0] == values["start_time"]]
        ends = [i for i, s in enumerate(slots) if s["time"].split("-")[-1] == values["end_time"]]
        if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
            raise ValueError("页面没有完整对应的起止时段")
        first, last = starts[0], ends[0]
        if any(s["disabled"] for s in slots[first:last + 1]):
            raise ValueError("目标时段包含不可选时间；请在官方页面核实")
        for left, right in zip(slots[first:last], slots[first + 1:last + 1]):
            if left["time"].split("-")[-1] != right["time"].split("-")[0]:
                raise ValueError("目标时段不连续")
        # The initial seat URL opens an unselected time picker. Do not reset
        # selections a human may already have made; only accept an exact match.
        if state["start"] or state["end"]:
            if (state["start"], state["end"]) != (values["start_time"], values["end_time"]):
                raise ValueError("页面已有不同时间选择，请重新演练")
        else:
            cells = self.page.locator(".time_pop:visible .time_cell")
            if cells.count() != len(slots):
                raise ValueError("时间控件结构改变")
            cells.nth(first).click(timeout=2000)
            if last != first:
                cells.nth(last).click(timeout=2000)
        state = self.state()
        if (state["day"], state["room"], normalize_seat(state["seat"]), state["start"], state["end"]) != (
                day, values["room_id"], seat, values["start_time"], values["end_time"]):
            raise ValueError("选择后的页面信息与计划不一致")
        button = self.page.locator(".time_pop:visible .time_sure")
        if button.count() != 1 or button.inner_text().strip() != "提交" or not button.is_enabled():
            raise ValueError("无法明确识别预约提交按钮")
        return button, state["fid"]


def run_browser(run_id, account_id, values, day, fire_epoch, checkpoint, *, preview=False, check_only=False, login_only=False) -> BrowserResult:
    timing_sink: list[dict] = []
    result = _run_browser_impl(
        run_id, account_id, values, day, fire_epoch, checkpoint,
        preview=preview, check_only=check_only, login_only=login_only,
        timing_sink=timing_sink,
    )
    result.attempt_timings = [
        {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in timing.items()
            if not key.startswith("_")
        }
        for timing in timing_sink
    ]
    return result


def _run_browser_impl(run_id, account_id, values, day, fire_epoch, checkpoint, *,
                      preview=False, check_only=False, login_only=False,
                      timing_sink=None) -> BrowserResult:
    ready = readiness()
    if not ready["ready"]:
        return BrowserResult("FAILED", "BROWSER_NOT_READY", ready["message"])
    from playwright.sync_api import sync_playwright

    control = Control()
    with _control_lock:
        _controls[run_id] = control
    remaining = fire_epoch + BROWSER_RUN_SECONDS - clock.server_now()
    if remaining <= 0:
        with _control_lock:
            _controls.pop(run_id, None)
        return BrowserResult("SKIPPED", "DEADLINE_EXCEEDED", "已超过开抢后的 5 分钟等待上限，未启动预约")
    deadline = time.monotonic() + remaining
    gate = RequestGate(
        values, day, preview or check_only or login_only,
        checkpoint, control, deadline, timing_sink,
    )
    context = page = None
    fid = ""
    bound_identity = None
    binding_path = None
    from .browser_sessions import current_session
    managed = current_session()
    last_state = None
    checking_login = False
    last_lookup_items = None
    submission_baseline = None
    last_rejection_code = None
    last_rejection_message = ""

    def identity():
        # Persist only a hash binding, never an authentication token. This also
        # catches manual account switching inside an existing browser profile.
        return account_identity(context.cookies(ORIGIN), values.get("username", ""))

    def bind_identity():
        nonlocal bound_identity
        current = identity()
        try:
            saved = json.loads(binding_path.read_text(encoding="utf-8")).get("identity")
        except (OSError, ValueError):
            saved = None
        human_confirmed = current and control.confirmed_identity == current
        if current and (saved == current or gate.login_ok or human_confirmed):
            bound_identity = current
            if saved != current:
                binding_path.write_text(json.dumps({"identity": current,
                    "verification": "user_confirmed" if human_confirmed else "password_login"}), encoding="utf-8")
            save_browser_session(context, binding_path.parent, values.get("username", ""), current)
            return True
        return False

    def confirm_browser_account(return_url):
        # QR/SMS login does not expose the plan's login name to our request gate.
        # Let the user check the authenticated official account page, then bind
        # precisely the identity they saw. Never treat a cached UID alone as proof.
        expected_identity = identity()
        if not expected_identity:
            return False
        response = page.goto("https://passport2.chaoxing.com/mooc/accountManage",
                             wait_until="domcontentloaded", timeout=15000)
        parsed = urlparse(page.url)
        if (not response or response.status != 200 or
                (parsed.hostname, parsed.path) != ("passport2.chaoxing.com", "/mooc/accountManage") or
                page.locator("#phone[name=user_mobile]").count() != 1 or identity() != expected_identity):
            return False
        with _control_lock:
            control.confirmed_identity = None
            control.awaiting_identity = expected_identity
        try:
            while True:
                tick("WAITING_USER", "请确认登录账号：在原窗口查看官方账号资料，确认是本计划对应账号后，点击管理页“确认是本计划账号”；确认前不会预约")
                if identity() != expected_identity:
                    raise InterruptedError("确认期间登录账号发生变化，请重新演练")
                if control.confirmed_identity == expected_identity and bind_identity():
                    page.goto(return_url, wait_until="domcontentloaded", timeout=15000)
                    return True
                page.wait_for_timeout(250)
        finally:
            with _control_lock:
                control.awaiting_identity = None
                control.confirmed_identity = None

    def tick(state, message, *, checking=False):
        nonlocal last_state
        if not checkpoint(state, message, None):
            control.cancel.set()
        if control.focus.is_set() and page and not page.is_closed():
            control.focus.clear()
            window_state(page, True)
        if state != last_state:
            if state in {'WAITING_LOGIN', 'WAITING_USER'}:
                window_state(page, True)
            elif last_state in {'WAITING_LOGIN', 'WAITING_USER'}:
                window_state(page, False)
            last_state = state
        if not checking and (control.cancel.is_set() or time.monotonic() >= deadline):
            raise InterruptedError("已停止接管或等待超时")
        if not checking and page.is_closed():
            raise InterruptedError("预约窗口已关闭")
        if bound_identity and not checking and identity() != bound_identity:
            raise InterruptedError("浏览器登录账号发生变化，已停止接管")

    def lookup(timeout=5000, previous_items=None):
        nonlocal last_lookup_items
        if not bound_identity or identity() != bound_identity:
            return "unavailable", None, "浏览器账号尚未核实或已变化，不能核对另一账号的预约"
        if not fid:
            return "unavailable", None, "未能取得学校预约列表入口"
        try:
            response = context.request.get(ORIGIN + "/data/apps/seat/index", params={"fidEnc": fid}, timeout=timeout, max_redirects=0)
            if response.status != 200:
                raise ValueError("unavailable")
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise ValueError("invalid list")
            items = (payload.get("data") or {}).get("curReserves")
            if not isinstance(items, list):
                raise ValueError("invalid list")
            last_lookup_items = [dict(item) if isinstance(item, dict) else item for item in items]
            return reservation_match(items, values, day, previous_items)
        except Exception:
            last_lookup_items = None
            return "unavailable", None, "无法读取同一浏览器会话的官方预约记录"

    def reconcile(*, allow_challenge=False):
        # During the normal flow a slow submit response may announce a human
        # challenge after read-only reconciliation has already started. Keep
        # the same seat armed until that possibility is resolved.
        until = time.monotonic() + VERIFY_SECONDS
        detail = ""
        while time.monotonic() < until:
            if gate.terminal_code:
                return BrowserResult("BLOCKED_BY_RISK", gate.terminal_code, gate.terminal_message)
            if allow_challenge and gate.challenge_pending:
                return None
            tick("VERIFYING", "正在核对官方预约记录，不会重复提交", checking=True)
            state, seat, detail = lookup(
                timeout=max(1, min(5000, int((until - time.monotonic()) * 1000))),
                previous_items=submission_baseline,
            )
            if allow_challenge and gate.challenge_pending:
                return None
            if state == "exact":
                gate.armed_seat = None
                checkpoint("VERIFYING", "已核对实际预约", False)
                return BrowserResult("SUCCESS", None, "官方记录已确认：" + detail, seat)
            if state == "conflict":
                gate.armed_seat = None
                return BrowserResult("NEEDS_VERIFICATION", "BROWSER_RESERVATION_CONFLICT", detail)
            if gate.rejected:
                gate.armed_seat = None
                return BrowserResult("FAILED", "SEAT_UNAVAILABLE", "官方明确返回当前座位不可预约")
            if page and not page.is_closed():
                page.wait_for_timeout(min(2000, max(0, (until - time.monotonic()) * 1000)))
            else:
                time.sleep(min(2, max(0, until - time.monotonic())))
        gate.armed_seat = None
        return BrowserResult("NEEDS_VERIFICATION", "SUBMIT_OUTCOME_UNKNOWN", detail + "；请先到超星核实，再处理后续预约")

    try:
        with (managed.runtime() if managed else sync_playwright()) as pw:
            profile = DATA_DIR / "browser-profiles" / f"account-{int(account_id)}"
            profile.mkdir(parents=True, exist_ok=True)
            binding_path = profile / "account-binding.json"
            context = managed.open(pw, profile) if managed else launch_context(pw, profile)
            context.set_default_timeout(3000)
            install_request_gate(context, gate)
            context.on("response", gate.response)
            gate.identity_check = lambda: bool(bound_identity and identity() == bound_identity)
            try:
                previous_binding = json.loads(binding_path.read_text(encoding="utf-8")).get("identity")
            except (OSError, ValueError):
                previous_binding = None
            restored_session = restore_browser_session(context, profile, values.get("username", ""), previous_binding)
            current_identity = identity()
            logger.info("browser run %s session: binding=%s identity=%s restored=%s matched=%s", run_id,
                        bool(previous_binding), bool(current_identity), restored_session,
                        bool(previous_binding and current_identity == previous_binding))
            needs_login = not current_identity or bool(previous_binding and current_identity != previous_binding)
            if needs_login:
                # A different account must log in again. A first QR/SMS session
                # is preserved, but cannot proceed without human confirmation.
                context.clear_cookies()
            page = context.pages[0] if context.pages else context.new_page()
            for extra in context.pages[1:]:
                extra.close()
            # A fresh or expired login check must start visibly. Previously the
            # new window was minimized before the redirect could be identified;
            # a slow/failed redirect then left the user with no login window.
            window_state(page, bool(login_only and needs_login))
            # Popup submissions remain gated at context scope; only the owned
            # page is automatically operated.
            try:
                # A live official response, not a UID cookie, proves that login
                # is usable. The same session continues into preparation.
                if managed or login_only:
                    checking_login = True
                    if login_only and needs_login:
                        login_url = 'https://passport2.chaoxing.com/login?' + urlencode({
                            'newversion': 'true', 'refer': ACCOUNT_URL})
                        response = page.goto(login_url, wait_until='domcontentloaded', timeout=15000)
                    else:
                        response = page.goto(ACCOUNT_URL, wait_until='domcontentloaded', timeout=15000)
                    ready_until = time.monotonic() + 15
                    while True:
                        if response and response.status == 200 and official_account_page(page) and identity():
                            if not bind_identity() and not confirm_browser_account(ACCOUNT_URL):
                                return BrowserResult('NEEDS_VERIFICATION', 'BROWSER_ACCOUNT_UNVERIFIED', '请核实此账号的官方资料')
                            checkpoint('RUNNING', '官方已确认登录有效', None)
                            needs_login = False
                            window_state(page, False)
                            if login_only:
                                return BrowserResult('SKIPPED', 'LOGIN_READY', '官方已确认登录有效，已保存会话，可等待自动预约')
                            break
                        path = urlparse(page.url).path.lower()
                        if urlparse(page.url).hostname == 'passport2.chaoxing.com' and ('login' in path or page.locator('input[type=password]').count() > 0):
                            (profile / 'session.dpapi').unlink(missing_ok=True)
                            tick('WAITING_LOGIN', '登录未完成或已失效，请在原窗口登录；完成后自动继续')
                            ready_until = time.monotonic() + 15
                        elif time.monotonic() > ready_until or (response and response.status >= 400):
                            return BrowserResult('NEEDS_VERIFICATION', 'LOGIN_CHECK_FAILED', '官方登录状态检测失败，请检查网络后重试；未清除登录资料')
                        else:
                            tick('RUNNING', '正在读取官方账号登录状态')
                        page.wait_for_timeout(250)
                        if urlparse(page.url).hostname == 'office.chaoxing.com':
                            response = page.goto(ACCOUNT_URL, wait_until='domcontentloaded', timeout=15000)
                    checking_login = False
                    if fire_epoch - clock.server_now() > 30:
                        while fire_epoch - clock.server_now() > 30:
                            tick('WAITING_OPEN', '登录已就绪，等待开抢前 30 秒准备预约页面')
                            page.wait_for_timeout(250)
                        response = page.goto(ACCOUNT_URL, wait_until='domcontentloaded', timeout=15000)
                        if not response or response.status >= 400:
                            return BrowserResult('NEEDS_VERIFICATION', 'LOGIN_CHECK_FAILED', '开抢前登录检测失败，请检查网络')
                        if not official_account_page(page):
                            bound_identity = None
                            needs_login = True
                        else:
                            checkpoint('RUNNING', '官方已确认登录有效', None)
                pending_seats = deque(values["seats"][:min(6, values.get("max_attempts", 6))])
                refresh_retries: dict[str, int] = {}
                while pending_seats:
                    seat = pending_seats.popleft()
                    gate.armed_seat = None
                    gate.rejected = False
                    gate.rejection_code = None
                    gate.rejection_message = ""
                    # Unlocks already armed for this pass are not "new" rejections.
                    refresh_seen = gate.refresh_serial
                    url = select_url(values, day, seat)
                    if check_only:
                        url = ORIGIN + "/front/third/apps/seat/code?" + urlencode({"id": values["room_id"], "seatNum": seat})
                    tick("RUNNING", f"正在打开座位 {seat} 的官方预约页")
                    # Some office pages expose the time picker even without an
                    # authenticated session. Do not use its presence as login proof.
                    entry_url = ("https://passport2.chaoxing.com/login?" + urlencode({
                        "newversion": "true", "refer": url}) if needs_login else url)
                    page.goto(entry_url, wait_until="domcontentloaded", timeout=15000)
                    adapter = PageAdapter(page)
                    login_seen = False
                    ready_until = time.monotonic() + 15
                    refreshed_at_open = False
                    while True:
                        host = urlparse(page.url).hostname or ""
                        if host == "passport2.chaoxing.com":
                            # The official site requires login again. Do not
                            # resurrect an expired/logged-out snapshot next run.
                            (profile / "session.dpapi").unlink(missing_ok=True)
                            login_seen = True
                            tick("WAITING_LOGIN", "请在原浏览器窗口登录此计划的超星账号；登录后会继续准备")
                            page.wait_for_timeout(250)
                            ready_until = time.monotonic() + 15
                            continue
                        if login_seen:
                            login_seen = False
                            page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        if adapter.needs_user():
                            tick("WAITING_USER", "官方页面出现安全验证；请在原窗口完成，期间不会自动点击或刷新")
                            ready_until = time.monotonic() + 15
                            page.wait_for_timeout(250)
                            continue
                        if host == "office.chaoxing.com" and not bound_identity:
                            # Remember a verified login even if the selected
                            # future date is not yet open or has no time picker.
                            if bind_identity():
                                needs_login = False
                            elif identity():
                                if not confirm_browser_account(url):
                                    return BrowserResult("NEEDS_VERIFICATION", "BROWSER_ACCOUNT_UNVERIFIED",
                                        "无法打开已登录的官方账号资料，请重新登录后演练；未提交预约")
                                needs_login = False
                                # Human confirmation may take minutes. Give the
                                # returning seat page its own full loading period.
                                ready_until = time.monotonic() + 15
                        state = adapter.state()
                        if not preview and not check_only and not refreshed_at_open and clock.server_now() >= fire_epoch and not (state and state.get("timesShow")):
                            # The pre-opening response is stale at the opening
                            # boundary. Fetch once before classifying it as closed.
                            refreshed_at_open = True
                            page.goto(url, wait_until="domcontentloaded", timeout=15000)
                            ready_until = time.monotonic() + 15
                            continue
                        if check_only and host == "office.chaoxing.com":
                            entry = page.evaluate("() => typeof fidEnc === 'string' ? fidEnc : ''")
                            if entry:
                                state = {"ready": True, "fid": entry}
                        target_ready = bool(state and state["ready"] and (
                            check_only or (
                                state.get("timesShow")
                                and state.get("day") == day
                                and state.get("room") == values["room_id"]
                            )
                        ))
                        if target_ready:
                            fid = state["fid"]
                            break
                        if state is None and opening_notice(page):
                            if preview:
                                return BrowserResult("PROBE_DONE", "PROBE_WAITING_OPEN",
                                    f"官方提示当前区域未到开放预约时间；目标日期 {day}，计划执行时间 {values.get('run_time', '未配置')}。未提交预约；需开放后再完成选座演练。")
                            if clock.server_now() >= fire_epoch and refreshed_at_open:
                                return BrowserResult("FAILED", "TARGET_DAY_NOT_OPEN",
                                    f"官方提示目标日期 {day} 尚未开放预约；未提交预约")
                        if not preview and clock.server_now() < fire_epoch:
                            tick("WAITING_OPEN", "已打开页面，等待预约窗口开放")
                            page.wait_for_timeout(250)
                            continue
                        if not preview and not refreshed_at_open:
                            refreshed_at_open = True
                            page.goto(url, wait_until="domcontentloaded", timeout=15000)
                            ready_until = time.monotonic() + 15
                        tick("RUNNING", "等待官方页面提供目标日期和可选时段")
                        if time.monotonic() > ready_until:
                            logger.warning("browser run %s page not ready: state=%s picker=%s", run_id,
                                           bool(state and state.get("ready")), bool(state and state.get("timesShow")))
                            return BrowserResult("NEEDS_VERIFICATION", "BROWSER_PAGE_UNSUPPORTED", "目标页面未提供可识别的选座控件；可能未开放、需要客户端或页面结构已变更")
                        page.wait_for_timeout(250)
                    if not bind_identity():
                        return BrowserResult("NEEDS_VERIFICATION", "BROWSER_ACCOUNT_UNVERIFIED",
                            "无法确认浏览器账号与计划一致；请重新演练并完成官方账号确认")
                    if check_only:
                        found, held_seat, detail = lookup()
                        if found in {"exact", "absent"}:
                            return BrowserResult("SKIPPED", "BROWSER_CHECK_" + found.upper(), detail + "；只读核对完成，未提交预约", held_seat)
                        return BrowserResult("NEEDS_VERIFICATION", "BROWSER_PRECHECK_FAILED", detail)
                    if not preview:
                        # A previously booked interval can be disabled in the
                        # picker. Check the official list before choosing slots.
                        existing, held_seat, detail = lookup()
                        if existing == "exact":
                            return BrowserResult("SKIPPED", "ALREADY_BOOKED_ON_SERVER", detail, held_seat)
                        if existing != "absent":
                            return BrowserResult("NEEDS_VERIFICATION", "BROWSER_PRECHECK_FAILED", detail)
                    try:
                        button, fid = adapter.prepare(values, day, seat)
                    except ValueError as exc:
                        return BrowserResult("NEEDS_VERIFICATION", "BROWSER_PAGE_UNSUPPORTED", str(exc))
                    if preview:
                        continue  # never click submit, even with the gate installed
                    while clock.server_now() < fire_epoch:
                        tick("WAITING_OPEN", f"座位 {seat} 和时段已准备，等待开抢")
                        page.wait_for_timeout(250)
                    # Deliberately NO reservation-list lookup here. The official
                    # seat page never calls that endpoint, so a call at this
                    # instant is an off-script request sitting directly on the
                    # critical path — roughly one round trip between the
                    # platform's opening moment and our click.
                    #
                    # The pre-fire check above already answered the question
                    # that matters ("do we already hold this interval?"), and at
                    # the opening moment this account's own list is empty by
                    # definition. A booking made elsewhere in the gap is still
                    # caught by the platform's own refusal (seat taken) and by
                    # the post-submit reconciliation.
                    submission_baseline = last_lookup_items
                    # Re-read all selected values after waiting/user interaction.
                    button, fid = adapter.prepare(values, day, seat)
                    tick("RUNNING", f"准备预约座位 {seat}")
                    gate.armed_seat = seat
                    gate.mark_click(seat)
                    button.click(timeout=3000)
                    # Let the page's immediate submit callback reach the gate
                    # before announcing a human challenge. An ordinary network
                    # event must not flash the window or sound an alert.
                    automatic_until = min(deadline, time.monotonic() + 1)
                    while (not gate.pending and not gate.rejected
                           and gate.refresh_serial == refresh_seen
                           and not gate.terminal_code and time.monotonic() < automatic_until):
                        if adapter.needs_user():
                            break
                        tick('RUNNING', '等待官方提交或验证提示')
                        page.wait_for_timeout(50)
                    # A challenge can be returned by the submit response itself.
                    # In that case the same official flow is allowed to continue
                    # after human verification; it must not fall through to an
                    # unknown result or rotate to another seat. An explicit
                    # "refresh and resubmit" refusal is handled the same way —
                    # the seat loop reloads the page and re-sends this seat.
                    result = None
                    while True:
                        while (not gate.pending and not gate.rejected
                               and gate.refresh_serial == refresh_seen and not gate.terminal_code):
                            if gate.blocked_reason:
                                return BrowserResult("NEEDS_VERIFICATION", "BROWSER_INTENT_CHANGED", gate.blocked_reason)
                            message = gate.challenge_message or f"座位 {seat}：请在原窗口完成验证码或官方确认；系统等待提交结果"
                            tick("WAITING_USER", message)
                            page.wait_for_timeout(250)
                        if gate.refresh_serial != refresh_seen:
                            break
                        if gate.rejected:
                            break
                        if gate.terminal_code:
                            result = BrowserResult("BLOCKED_BY_RISK", gate.terminal_code, gate.terminal_message)
                            break
                        # Give the direct response a short chance to prove
                        # rejection or announce a human challenge; every other
                        # answer enters read-only reconciliation.
                        until = time.monotonic() + 2
                        while (time.monotonic() < until and not gate.rejected
                               and not gate.challenge_pending
                               and gate.refresh_serial == refresh_seen
                               and not gate.terminal_code):
                            tick("VERIFYING", "已发送预约，等待官方响应", checking=True)
                            if page.is_closed():
                                break
                            page.wait_for_timeout(100)
                        if gate.refresh_serial != refresh_seen:
                            # Leave the wait loop so the seat loop can reload the
                            # page and re-send this seat with a fresh token.
                            result = None
                            break
                        if gate.challenge_pending:
                            continue
                        if gate.terminal_code:
                            result = BrowserResult("BLOCKED_BY_RISK", gate.terminal_code, gate.terminal_message)
                            break
                        result = reconcile(allow_challenge=True)
                        if result is None:
                            continue
                        break
                    if gate.refresh_serial != refresh_seen:
                        # The platform refused this request and explicitly asked
                        # for a refresh plus a resubmit. Re-enter the SAME seat
                        # immediately so the whole per-seat flow (fresh page
                        # load, fresh token, duplicate re-check before clicking)
                        # runs again; ``refresh_pending`` is the explicit unlock
                        # the gate consumes on the resubmit.
                        retries = refresh_retries.get(seat, 0)
                        if (retries < REFRESH_RETRY_LIMIT and time.monotonic() < deadline
                                and not control.cancel.is_set()):
                            refresh_retries[seat] = retries + 1
                            tick("RUNNING", "官方要求刷新后重提：重新加载页面并再提交一次")
                            pending_seats.appendleft(seat)
                        else:
                            last_rejection_code = "TOKEN_STALE"
                            last_rejection_message = gate.refresh_message or "官方要求刷新后重提，已达重试上限"
                            gate.refresh_pending = False
                        continue
                    if gate.rejected:
                        last_rejection_code = gate.rejection_code or "SEAT_UNAVAILABLE"
                        last_rejection_message = gate.rejection_message
                        if last_rejection_code == "RATE_LIMITED" and time.monotonic() < deadline:
                            tick("RUNNING", "官方提示操作频繁，正在退避后检查下一候选")
                            page.wait_for_timeout(min(RATE_LIMIT_BACKOFF_MS, max(0, (deadline - time.monotonic()) * 1000)))
                        continue
                    return result
                if preview:
                    return BrowserResult("PROBE_DONE", "BROWSER_PREVIEW_READY", "浏览器演练通过：候选座位、日期、时段及提交控件均已检查；未提交预约")
                return BrowserResult(
                    "FAILED",
                    last_rejection_code or "SEAT_UNAVAILABLE",
                    last_rejection_message or "官方明确拒绝了所有候选座位",
                )
            except InterruptedError as exc:
                if gate.pending or gate.terminal_code:
                    return reconcile()
                return BrowserResult("SKIPPED", "BROWSER_CANCELLED", str(exc))
            except Exception as exc:
                import traceback
                frames = traceback.extract_tb(exc.__traceback__)
                logger.warning('browser run %s page failure %s at %s', run_id, type(exc).__name__,
                               ' > '.join(f'{Path(f.filename).name}:{f.lineno}' for f in frames[:5]))
                if gate.pending or gate.terminal_code:
                    try:
                        return reconcile()
                    except Exception:
                        return BrowserResult("NEEDS_VERIFICATION", "SUBMIT_OUTCOME_UNKNOWN", "浏览器连接中断，提交结果未知，请到超星核实")
                if _browser_target_closed(page, exc):
                    return BrowserResult("SKIPPED", "BROWSER_WINDOW_CLOSED", "官方登录或预约窗口意外关闭")
                return BrowserResult("NEEDS_VERIFICATION", "LOGIN_CHECK_FAILED" if checking_login else "BROWSER_PAGE_UNSUPPORTED",
                                     "官方登录状态检测失败，请检查网络；登录资料已保留" if checking_login else "浏览器页面或连接异常；请重新检查官方页面")
            finally:
                gate.armed_seat = None
                try:
                    if bound_identity and (urlparse(page.url).hostname == "office.chaoxing.com" or official_account_page(page)):
                        save_browser_session(context, profile, values.get("username", ""), bound_identity)
                except Exception:
                    pass  # A manually closed browser already has the login checkpoint.
                try:
                    if managed and not control.cancel.is_set() and not page.is_closed():
                        remove_request_gate(context, gate)
                        context.remove_listener('response', gate.response)
                        # Keep a deny gate while idle; delayed official
                        # callbacks may not submit after the task finishes.
                        gate.preview = True
                        install_request_gate(context, gate)
                        page.goto('about:blank', wait_until='domcontentloaded')
                        window_state(page, False)
                        managed.idle_gate = gate
                    elif managed:
                        managed.close_context()
                    else:
                        context.close()
                except Exception:
                    pass
    except Exception as exc:
        # Keep tracebacks useful without recording Playwright messages, which
        # can contain navigation URLs, credentials, or a browser profile path.
        logger.error("browser run %s failed during setup (%s)", run_id, type(exc).__name__)
        return BrowserResult("NEEDS_VERIFICATION" if gate.pending else "FAILED",
                             "SUBMIT_OUTCOME_UNKNOWN" if gate.pending else "BROWSER_START_FAILED",
                             "浏览器意外中断，请核实预约结果" if gate.pending else "浏览器无法启动或账号窗口正在使用；请检查浏览器组件与桌面状态")
    finally:
        with _control_lock:
            _controls.pop(run_id, None)
