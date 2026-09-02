from __future__ import annotations

import datetime as dt
import html
import logging
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from utils import AES_Encrypt, verify_param

logger = logging.getLogger(__name__)


class ReservationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ProbeResult:
    ok: bool
    token: str = ""
    algorithm: str = ""
    captcha_type: str = "none"
    page_state: str = "UNKNOWN_PAGE"
    message: str = ""
    # Where the parameters came from: "target_day" (seat-select page for the
    # day we submit) or "current_page" (legacy seat/code status page).
    source: str = "current_page"
    detail: dict = field(default_factory=dict)


def normalize_seat(value: str | int) -> str:
    raw = str(value).strip()
    if not raw.isdigit():
        raise ValueError("seat number must contain digits only")
    return raw.zfill(3)


# Login headers mirrored from the proven upstream implementation (GitHub
# rebuild branch, utils/reserve.py get_login_status/login). Login traffic keeps
# this mobile-XHR shape so cookies and risk-control behaviour stay identical to
# the original script instead of looking like a desktop browser.
LOGIN_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Referer": "https://passport2.chaoxing.com/",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X) "
        "AppleWebKit/603.1.3 (KHTML, like Gecko) Version/10.0 Mobile/14E304 "
        "Safari/602.1 wechatdevtools/1.05.2109131 MicroMessenger/8.0.5 "
        "Language/zh_CN webview/16364215743155638"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

# Desktop browser headers for office.chaoxing.com pages. Referer is set per
# request and no static Host header is used: requests derives Host from the
# URL, which keeps passport2 and office traffic on the right hosts.
OFFICE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}

LOGIN_REDIRECT_MARKERS = ("passport2.chaoxing.com", "/wlogin", "fanyalogin")
SECURITY_REDIRECT_MARKERS = ("captcha", "verify", "security", "risk")
EPHEMERAL_SELECT_KEYS = {"day", "pagetoken", "token", "captcha", "enc", "submit_enc", "algorithm", "verifydata"}
SELECT_PATH_RE = re.compile(r"/front/(?:third/)?apps/seat/select", re.I)
CAPTCHA_TYPES = {"slider", "point_click"}
_FALLBACK_CONTEXT_ERRORS = {"NETWORK_ERROR", "HTTP_ERROR", "SERVER_ERROR", "TARGET_CONTEXT_UNAVAILABLE"}


class _InputParser(HTMLParser):
    """Small, tolerant extractor for hidden inputs on seat pages.

    HTML attribute order is not significant. Regexes that assume ``id`` appears
    before ``value`` make a valid page look like it has no token.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "input":
            self.inputs.append({str(key).lower(): str(value or "") for key, value in attrs if key})


def _input_fields(text: str) -> tuple[str, str]:
    parser = _InputParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # A malformed page should be reported as missing parameters, never make
        # the reservation worker crash.
        return "", ""
    token = ""
    algorithm = ""
    values: list[str] = []
    for attrs in parser.inputs:
        value = attrs.get("value", "").strip()
        if value:
            values.append(value)
        field_id = attrs.get("id", "").strip().lower()
        field_name = attrs.get("name", "").strip().lower()
        if field_id == "submit_enc" or field_name == "submit_enc":
            token = value
        if field_id == "algorithm" or field_name == "algorithm":
            algorithm = value
    if not algorithm:
        # Older pages either expose a second unnamed signing value or only the
        # canonical ``submit_enc`` field.  The upstream ChaoXing client uses
        # that lone field both as the request token and as the signature salt;
        # a live read-only probe of this school's target-day page has exactly
        # that one-field shape.  Do not apply this fallback to arbitrary form
        # inputs: it is limited to a page whose only non-empty input value is
        # the explicitly identified submit_enc token.
        algorithm = next((value for value in values if value and value != token), "")
        if not algorithm and token and values and all(value == token for value in values):
            algorithm = token
    return token, algorithm


def _select_path(parsed) -> str:
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "office.chaoxing.com":
        raise ValueError("请粘贴 office.chaoxing.com 的选座页链接")
    if not SELECT_PATH_RE.fullmatch(parsed.path):
        raise ValueError("请粘贴选座页链接（路径需包含 /front/.../apps/seat/select）")
    return parsed.path


def normalize_select_context_path(value: str) -> str:
    """Return the canonical relative path stored with a plan.

    Old automatic-discovery code saved ``select_page_url`` (a complete URL),
    while the API only accepted a relative path.  Accept that legacy shape at
    the boundary, but never persist it again.
    """
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return _select_path(parsed)
    if parsed.query or parsed.fragment:
        raise ValueError("选座页路径无效")
    if not SELECT_PATH_RE.fullmatch(raw):
        raise ValueError("选座页路径无效")
    return raw


def _parse_select_url(url: str, *, include_ephemeral: bool = False) -> tuple[dict[str, str], str]:
    """Parse a pasted ChaoXing seat-select page link into reusable query params.

    The ``day`` value of the pasted link is deliberately dropped: the target
    day is always computed from the plan at run time, never trusted from a
    sample link.
    """
    parsed = urlparse(str(url).strip())
    select_path = _select_path(parsed)
    pairs = parse_qs(parsed.query, keep_blank_values=True)
    params: dict[str, str] = {}
    for key, values in pairs.items():
        clean_key = key.strip()
        value = values[-1].strip() if values else ""
        # Param names are case-sensitive on the server (deptIdEnc, backLevel),
        # so only "day" is matched case-insensitively; original casing is kept.
        if not clean_key or (not include_ephemeral and clean_key.lower() in EPHEMERAL_SELECT_KEYS):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]+", clean_key):
            raise ValueError(f"选座页链接参数名异常：{key}")
        if len(value) > 512:
            raise ValueError(f"选座页链接参数值过长：{key}")
        params[clean_key] = value
    if not params:
        raise ValueError("选座页链接缺少学校或房间参数（只有日期是不够的）")
    if len(params) > 12:
        raise ValueError("选座页链接参数过多，请确认链接完整")
    return params, select_path


def parse_select_url(url: str) -> dict[str, str]:
    """Parse manually supplied, reusable seat-select parameters only."""
    return _parse_select_url(url, include_ephemeral=False)[0]


def parse_select_context_url(url: str) -> tuple[dict[str, str], str]:
    """Parse reusable query params and the exact first-party select path."""
    return _parse_select_url(url, include_ephemeral=False)


def _stable_select_params(params: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in params.items() if key.lower() not in EPHEMERAL_SELECT_KEYS}


class ChaoxingClient:
    login_page = "https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid="
    login_url = "https://passport2.chaoxing.com/fanyalogin"
    page_url = "https://office.chaoxing.com/front/third/apps/seat/code?id={room}&seatNum={seat}"
    select_page_url = "https://office.chaoxing.com/front/third/apps/seat/select"
    submit_url = "https://office.chaoxing.com/data/apps/seat/submit"
    reservations_url = "https://office.chaoxing.com/data/apps/seat/index"
    fid_enc_re = re.compile(r"fidEnc\s*=\s*['\"]([A-Za-z0-9]+)['\"]")

    def __init__(self, username: str, password: str, *, slider_enabled: bool = False, deadline: float | None = None):
        self.username = username
        self.password = password
        self.slider_enabled = slider_enabled
        self.deadline = deadline
        self.logged_in = False
        self.session = requests.Session()
        self.timeout = (8, 20)
        # Safe run metadata used by the local audit trail. These never contain a
        # token, cookie, password, captcha result or signature.
        self.last_parameter_source = ""
        self.last_discovered_select_params: dict[str, str] | None = None
        self.last_discovered_select_path: str | None = None
        self.last_submitted = False
        # School app entry key discovered from seat pages; required by the
        # read-only reservation-list endpoint.
        self.last_fid_enc = ""

    def _remaining_seconds(self) -> float | None:
        if self.deadline is None:
            return None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReservationError("DEADLINE_EXCEEDED", "任务超过 60 秒运行上限")
        return remaining

    @staticmethod
    def _classify_redirect(status_code: int, location: str) -> ReservationError:
        """Tell re-login redirects apart from security challenges and the rest."""
        loc = (location or "").lower()
        if any(marker in loc for marker in LOGIN_REDIRECT_MARKERS):
            return ReservationError("LOGIN_REQUIRED", f"登录会话失效（HTTP {status_code}）")
        if any(marker in loc for marker in SECURITY_REDIRECT_MARKERS):
            return ReservationError("SECURITY_CHALLENGE", f"平台跳转到安全验证（HTTP {status_code}）")
        return ReservationError("BLOCKED_BY_RISK", f"平台重定向到未知地址（HTTP {status_code}）")

    def _request(
        self,
        method: str,
        url: str,
        *,
        follow_redirects: bool = False,
        headers: dict | None = None,
        allow_login_landing: bool = False,
        **kwargs,
    ) -> requests.Response:
        remaining = self._remaining_seconds()
        if remaining is None:
            timeout = self.timeout
        else:
            timeout = (max(1, min(self.timeout[0], remaining)), max(1, min(self.timeout[1], remaining)))
        kwargs.setdefault("timeout", timeout)
        kwargs.setdefault("verify", True)
        kwargs.setdefault("allow_redirects", follow_redirects)
        try:
            response = self.session.request(method, url, headers=headers, **kwargs)
        except requests.RequestException as exc:
            raise ReservationError("NETWORK_ERROR", f"网络请求失败：{exc.__class__.__name__}") from exc
        if not follow_redirects and response.status_code in {301, 302, 303, 307, 308}:
            raise self._classify_redirect(response.status_code, response.headers.get("Location", ""))
        if follow_redirects and not allow_login_landing:
            # Only office page fetches use this: landing on the login host after
            # redirects means the session expired. The login page itself lives on
            # that host and must pass with allow_login_landing=True.
            final_url = str(getattr(response, "url", "")).lower()
            if any(marker in final_url for marker in LOGIN_REDIRECT_MARKERS):
                raise ReservationError("LOGIN_REQUIRED", "登录会话失效（被重定向到登录页）")
        if response.status_code == 429:
            raise ReservationError("BLOCKED_BY_RISK", "平台限流（HTTP 429）")
        if response.status_code >= 500:
            raise ReservationError("SERVER_ERROR", f"平台服务异常（HTTP {response.status_code}）")
        if response.status_code >= 400:
            raise ReservationError("HTTP_ERROR", f"平台拒绝请求（HTTP {response.status_code}）")
        return response

    def login(self) -> None:
        if self.logged_in:
            return
        self._request("GET", self.login_page, headers=LOGIN_HEADERS, follow_redirects=True, allow_login_landing=True)
        params = {
            "fid": -1,
            "uname": AES_Encrypt(self.username),
            "password": AES_Encrypt(self.password),
            "refer": "http%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fthird%2Fapps%2Fseat%2Fcode%3Fid%3D4219%26seatNum%3D380",
            "t": True,
        }
        try:
            payload = self._request("POST", self.login_url, params=params, headers=LOGIN_HEADERS).json()
        except ValueError as exc:
            raise ReservationError("LOGIN_RESPONSE", "登录响应不是 JSON") from exc
        if not payload.get("status"):
            raise ReservationError("LOGIN_FAILED", str(payload.get("msg2") or payload.get("msg") or "登录失败"))
        self.logged_in = True

    def clone_authenticated(self) -> "ChaoxingClient":
        """Copy this client's login state into an independent instance.

        The opening-moment race submits several seats at once; one
        requests.Session must not be shared across threads, so every racer
        gets a clone carrying the same cookie jar (no re-login traffic).
        """
        clone = ChaoxingClient(self.username, self.password, slider_enabled=self.slider_enabled, deadline=self.deadline)
        clone.session.cookies.update(self.session.cookies)
        clone.logged_in = self.logged_in
        clone.last_fid_enc = self.last_fid_enc
        return clone

    def _parse_page(self, response: requests.Response) -> ProbeResult:
        # ChaoXing seat pages are UTF-8. requests may otherwise select a legacy
        # fallback encoding, which makes Chinese state markers impossible to match.
        try:
            text = response.content.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            text = response.text
        match = self.fid_enc_re.search(text)
        if match:
            self.last_fid_enc = match.group(1)
        text = html.unescape(text)
        lowered = text.lower()
        if "passport2.chaoxing.com" in lowered or "fanyalogin" in lowered:
            return ProbeResult(False, page_state="LOGIN_REQUIRED", message="页面要求重新登录")
        token, algorithm = _input_fields(text)
        if not token:
            token_match = re.search(r"token\s*=\s*['\"]([^'\"]+)", text, re.I)
            token = token_match.group(1) if token_match else ""
        captcha_context = any(marker in lowered for marker in ("captcha.chaoxing.com", "captchaid", "imageverification", "verification/image"))
        point_click_markers = ("textclickarr", "点击图中", "点选验证码")
        slider_markers = ("type=\"slide\"", "type='slide'", "type: \"slide\"", "type: 'slide'", "滑块验证码")
        if any(value in lowered for value in point_click_markers):
            return ProbeResult(False, algorithm=algorithm, captcha_type="point_click", page_state="POINT_CLICK_CAPTCHA", message="检测到点选式验证码")
        if captcha_context and any(value in lowered for value in slider_markers):
            if token and algorithm:
                return ProbeResult(True, token, algorithm, "slider", "SLIDER_CAPTCHA", "已取得预约参数，检测到滑块验证码")
            return ProbeResult(False, algorithm=algorithm, captcha_type="slider", page_state="SLIDER_CAPTCHA", message="检测到滑块验证码，但未取得完整预约参数")

        if token and algorithm:
            occupied_markers = ("该座位已被别人预约", "等待用户签到", "当前有人使用", "当前预约时段")
            if any(marker in text for marker in occupied_markers):
                return ProbeResult(True, token, algorithm, "none", "TOKEN_READY", "当前状态页显示占用，但已取得预约参数；目标日期由提交接口判断")
            return ProbeResult(True, token, algorithm, "none", "TOKEN_READY", "已取得页面的预约参数")
        occupied_markers = ("该座位已被别人预约", "等待用户签到", "当前有人使用", "当前预约时段")
        if any(marker in text for marker in occupied_markers):
            return ProbeResult(False, algorithm=algorithm, page_state="CURRENTLY_OCCUPIED", message="该座位当前正在使用或等待签到；这不代表目标日期时段不可预约")
        if token:
            return ProbeResult(False, token, algorithm, page_state="ALGORITHM_MISSING", message="取得 token，但缺少签名算法参数")
        return ProbeResult(False, algorithm=algorithm, page_state="TOKEN_MISSING", message="当前页面未提供预约参数；未检测到验证码")

    def _current_page(self, room_id: str, seat_num: str) -> requests.Response:
        self.login()
        headers = {**OFFICE_HEADERS, "Referer": self.select_page_url}
        return self._request(
            "GET", self.page_url.format(room=room_id, seat=normalize_seat(seat_num)), headers=headers, follow_redirects=True
        )

    def prepare(self, room_id: str, seat_num: str) -> ProbeResult:
        """Legacy current-status page (/seat/code). Reference only: it shows the
        seat's state right now, not the state of the target day."""
        response = self._current_page(room_id, seat_num)
        result = self._parse_page(response)
        result.source = "current_page"
        if not result.ok and result.captcha_type == "point_click":
            raise ReservationError("NEEDS_VERIFICATION", f"检测到验证码：{result.captcha_type}")
        return result

    def fetch_target_day_page(self, target_day: dt.date, select_params: dict[str, str], select_path: str | None = None) -> ProbeResult:
        """Fetch the seat-select page bound to the target day.

        Codex's read-only verification confirmed this page returns a fresh
        ``submit_enc`` for the requested day, so the submit token must come
        from here instead of the current-status page.
        """
        self.login()
        params = {key: value for key, value in select_params.items()}
        params["day"] = target_day.isoformat()
        headers = {**OFFICE_HEADERS, "Referer": self.select_page_url}
        select_url = urljoin("https://office.chaoxing.com/", select_path or self.select_page_url)
        parsed = urlparse(select_url)
        _select_path(parsed)
        response = self._request("GET", select_url, params=params, headers=headers, follow_redirects=True)
        final_path = urlparse(str(getattr(response, "url", ""))).path.rstrip("/")
        # Before this school's 19:00 opening window, ChaoXing answers a valid
        # select-page request with HTTP 200 after redirecting to this internal
        # error route.  Treating that HTML as a generic TOKEN_MISSING page makes
        # a correctly preconfigured future plan look broken.
        if re.fullmatch(r"/front/apps/reserve/error/code/500", final_path, re.I):
            return ProbeResult(
                False,
                page_state="TARGET_DAY_NOT_OPEN",
                message=f"目标日期 {target_day.isoformat()} 的预约页面尚未开放或平台暂不可用（平台错误页 500）",
                source="target_day",
            )
        result = self._parse_page(response)
        result.source = "target_day"
        return result

    @staticmethod
    def _select_context_from_response(response: requests.Response, room_id: str) -> tuple[dict[str, str], str] | None:
        """Find stable select-page params exposed by a logged-in seat page.

        Schools expose different navigation shapes. We only accept first-party
        seat-select URLs and remove short-lived query values before persistence.
        """
        try:
            text = html.unescape(response.content.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError):
            text = html.unescape(response.text)
        text = text.replace("\\/", "/")
        candidates = [str(getattr(response, "url", ""))]
        candidates.extend(re.findall(r"(?:https?:)?//[^\"'<>\\s]+/front/(?:third/)?apps/seat/select[^\"'<>\\s]*", text, re.I))
        candidates.extend(re.findall(r"(?:href|location)\s*[=:]\s*[\"']([^\"']*?/front/(?:third/)?apps/seat/select[^\"']*)", text, re.I))
        for candidate in candidates:
            absolute = urljoin("https://office.chaoxing.com/", candidate)
            try:
                params, select_path = _parse_select_url(absolute, include_ephemeral=True)
            except ValueError:
                continue
            bound_room = str(params.get("id", "")).strip()
            if bound_room and bound_room != str(room_id).strip():
                continue
            params.setdefault("id", str(room_id).strip())
            return params, select_path
        return None

    @staticmethod
    def _usable_page(result: ProbeResult) -> bool:
        return result.ok or result.captcha_type in CAPTCHA_TYPES

    def resolve_submission_page(
        self,
        room_id: str,
        seat_num: str,
        day: dt.date,
        select_params: dict[str, str] | None = None,
        select_path: str | None = None,
        select_source: str | None = None,
        require_target_day: bool = False,
    ) -> ProbeResult:
        """Get fresh parameters without treating present occupancy as future availability.

        A manually stored context is an override. Without one, first inspect the
        authenticated current page for a select-page URL, then try the canonical
        room-only route used by schools that do not require extra query params.
        The legacy token path remains valid when the current page itself supplies
        complete parameters.
        """
        self.last_parameter_source = ""
        self.last_discovered_select_params = None
        self.last_discovered_select_path = None
        contexts: list[tuple[str, dict[str, str], str | None]] = []
        if select_params:
            source = "stored_auto_context" if select_source in {"auto_context", "room_context", "stored_auto_context"} else "manual_context"
            contexts.append((source, dict(select_params), select_path))

        context_errors: list[str] = []
        for source, context, path in contexts:
            try:
                result = self.fetch_target_day_page(day, context, path)
            except ReservationError as exc:
                if exc.code not in _FALLBACK_CONTEXT_ERRORS:
                    raise
                context_errors.append(exc.code)
                continue
            if self._usable_page(result):
                self.last_parameter_source = source
                if source == "stored_auto_context":
                    self.last_discovered_select_params = _stable_select_params(context)
                    self.last_discovered_select_path = normalize_select_context_path(path or self.select_page_url)
                return result
            context_errors.append(result.page_state)

        current_response = self._current_page(room_id, seat_num)
        current_result = self._parse_page(current_response)
        current_result.source = "current_page"
        discovered = self._select_context_from_response(current_response, room_id)
        automatic_contexts: list[tuple[str, dict[str, str], str | None]] = []
        if discovered and discovered[0] != (select_params or {}):
            automatic_contexts.append(("auto_context", discovered[0], discovered[1]))
        room_context = {"id": str(room_id).strip()}
        if room_context not in [params for _, params, _ in contexts] and room_context not in [params for _, params, _ in automatic_contexts]:
            automatic_contexts.append(("room_context", room_context, normalize_select_context_path(self.select_page_url)))
        for source, context, path in automatic_contexts:
            try:
                result = self.fetch_target_day_page(day, context, path)
            except ReservationError as exc:
                if exc.code not in _FALLBACK_CONTEXT_ERRORS:
                    raise
                context_errors.append(exc.code)
                continue
            if self._usable_page(result):
                self.last_parameter_source = source
                self.last_discovered_select_params = _stable_select_params(context)
                self.last_discovered_select_path = normalize_select_context_path(path or self.select_page_url)
                return result
            context_errors.append(result.page_state)
        if require_target_day:
            detail = "、".join(dict.fromkeys(context_errors)) or current_result.page_state
            if "TARGET_DAY_NOT_OPEN" in context_errors:
                raise ReservationError(
                    "TARGET_DAY_NOT_OPEN",
                    f"目标日期 {day.isoformat()} 的预约窗口尚未开放或平台暂不可用",
                )
            raise ReservationError("TARGET_CONTEXT_UNAVAILABLE", f"未取得目标日期预约参数（检查结果：{detail}）")
        if current_result.captcha_type in CAPTCHA_TYPES:
            self.last_parameter_source = "legacy_current"
            return current_result
        if current_result.ok:
            self.last_parameter_source = "legacy_current"
            return current_result
        detail = "、".join(dict.fromkeys(context_errors)) or current_result.page_state
        raise ReservationError("TARGET_CONTEXT_UNAVAILABLE", f"未取得目标日期预约参数（当前状态仅供参考；检查结果：{detail}）")

    def fetch_reservations(self, select_params: dict[str, str] | None = None, select_path: str | None = None) -> list[dict]:
        """Read-only: list the account's current/upcoming seat reservations.

        Backed by the same JSON endpoint the official seat-app home page uses
        (``data/apps/seat/index`` → ``data.curReserves``). ``nearReserves`` is
        the UI's recent-history list and includes cancelled/invalid records, so
        it must never be used as evidence that an active reservation exists.
        Any shape surprise raises ``VERIFY_UNAVAILABLE`` so callers can fall
        back to a safe stop instead of guessing.
        """
        self.login()
        if not self.last_fid_enc:
            # Discover the school entry key from any authenticated seat page.
            # One extra GET, read-only, no day binding required.
            params = dict(select_params or {"id": ""})
            params.pop("day", None)
            response = self._request(
                "GET",
                urljoin("https://office.chaoxing.com/", select_path or self.select_page_url),
                params=params,
                headers={**OFFICE_HEADERS, "Referer": "https://office.chaoxing.com/"},
                follow_redirects=True,
            )
            text = response.content.decode("utf-8", errors="ignore")
            match = self.fid_enc_re.search(text)
            if match:
                self.last_fid_enc = match.group(1)
        if not self.last_fid_enc:
            raise ReservationError("VERIFY_UNAVAILABLE", "未能取得学校入口参数（fidEnc），无法核对超星端预约")
        response = self._request(
            "GET",
            self.reservations_url,
            params={"fidEnc": self.last_fid_enc, "r": f"{time.time() * 1000:.0f}"},
            headers={**OFFICE_HEADERS, "Referer": urljoin("https://office.chaoxing.com/", select_path or self.select_page_url)},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ReservationError("VERIFY_UNAVAILABLE", "预约列表响应不是 JSON") from exc
        if not payload.get("success"):
            raise ReservationError("VERIFY_UNAVAILABLE", str(payload.get("msg") or "预约列表接口返回失败"))
        items = (payload.get("data") or {}).get("curReserves")
        if not isinstance(items, list):
            raise ReservationError("VERIFY_UNAVAILABLE", "预约列表结构异常（缺少 curReserves）")
        return items

    @staticmethod
    def find_reservation(
        reservations: list[dict],
        target_day: dt.date,
        room_id: str,
        start_time: str,
        end_time: str,
    ) -> dict | None:
        """Return the server-side reservation that overlaps this request, if any.

        Epoch-millisecond times are converted in Asia/Shanghai; the item's own
        ``today`` field must equal the target day so midnight-adjacent records
        cannot produce a false match.
        """
        tz = dt.timezone(dt.timedelta(hours=8))

        def minutes(value) -> int | None:
            try:
                moment = dt.datetime.fromtimestamp(int(value) / 1000, tz)
            except (TypeError, ValueError, OverflowError, OSError):
                return None
            return moment.hour * 60 + moment.minute

        try:
            wanted_start = int(start_time[:2]) * 60 + int(start_time[3:5])
            wanted_end = int(end_time[:2]) * 60 + int(end_time[3:5])
        except (TypeError, ValueError):
            return None
        room = str(room_id).strip()
        for item in reservations:
            if not isinstance(item, dict):
                continue
            if str(item.get("roomId", "")).strip() != room:
                continue
            if str(item.get("today", "")).strip() != target_day.isoformat():
                continue
            item_start = minutes(item.get("startTime"))
            item_end = minutes(item.get("endTime"))
            if item_start is None or item_end is None:
                continue
            if item_start < wanted_end and item_end > wanted_start:
                return item
        return None

    def _solve_slider(self) -> str:
        try:
            from utils.reserve import reserve as LegacyReserve
            legacy = LegacyReserve(sleep_time=1, max_attempt=1, enable_slider=True)
            legacy.requests = self.session
            return legacy.resolve_captcha()
        except ImportError as exc:
            raise ReservationError("SLIDER_DEPENDENCY", "请安装 NumPy 和 OpenCV 后再开启滑块功能") from exc
        except Exception as exc:
            raise ReservationError("SLIDER_FAILED", "滑块识别失败") from exc

    def probe(self, room_id: str, seat_num: str) -> ProbeResult:
        return self.prepare(room_id, seat_num)

    def browse(self, room_id: str, seat_num: str) -> None:
        """Warm-up only: mimic the page traffic of a waiting human.

        Fetches the same read-only pages the seat app loads while someone sits
        on the select screen. Every failure is swallowed: warm-up must never
        break a run, and the submit path re-fetches what it needs anyway.
        """
        try:
            self.login()
        except ReservationError:
            return
        headers = {**OFFICE_HEADERS, "Referer": self.select_page_url}
        for url in (
            f"https://office.chaoxing.com/data/apps/seat/room/info?roomId={str(room_id).strip()}",
            self.page_url.format(room=str(room_id).strip(), seat=normalize_seat(seat_num)),
        ):
            try:
                self._request("GET", url, headers=headers, follow_redirects=True)
            except ReservationError:
                continue

    def submit_once(
        self,
        room_id: str,
        seat_num: str,
        start_time: str,
        end_time: str,
        day: dt.date,
        select_params: dict[str, str] | None = None,
        select_path: str | None = None,
        select_source: str | None = None,
        pre_resolved: ProbeResult | None = None,
    ) -> str:
        self.last_submitted = False
        if pre_resolved is not None:
            # Parameters were fetched ~2s before the opening moment by the
            # pre-warm gate; reuse them so only the final POST remains.
            probe = pre_resolved
        else:
            probe = self.resolve_submission_page(room_id, seat_num, day, select_params, select_path, select_source)
        captcha = ""
        if probe.captcha_type in CAPTCHA_TYPES:
            # Automatic captcha solving is intentionally disabled in the local
            # service. It is both brittle and likely to trigger risk controls.
            raise ReservationError("CAPTCHA_REQUIRED", f"检测到{probe.captcha_type}验证码，需要人工处理")
        if not probe.ok:
            raise ReservationError("TARGET_CONTEXT_UNAVAILABLE", probe.message)
        params = {
            "roomId": room_id,
            "startTime": start_time,
            "endTime": end_time,
            "day": str(day),
            "seatNum": normalize_seat(seat_num),
            "captcha": captcha,
            "token": probe.token,
            "type": "1",
            "verifyData": "1",
        }
        # Generate enc and submit immediately so the token cannot go stale.
        params["enc"] = verify_param(params, probe.algorithm)
        headers = {**OFFICE_HEADERS, "Referer": self.select_page_url if probe.source == "target_day" else self.page_url.format(room=room_id, seat=normalize_seat(seat_num))}
        # From this point the request may have reached the platform even if the
        # response times out or cannot be decoded. Callers must not retry it.
        self.last_submitted = True
        try:
            payload = self._request("POST", self.submit_url, params=params, headers=headers).json()
        except ReservationError as exc:
            if exc.code in {"NETWORK_ERROR", "SERVER_ERROR"}:
                raise ReservationError("SUBMIT_OUTCOME_UNKNOWN", "提交请求已发出，但未能确认超星端结果；请先在超星端核实") from exc
            raise
        except ValueError as exc:
            raise ReservationError("SUBMIT_OUTCOME_UNKNOWN", "提交请求已发出，但响应无法解析；请先在超星端核实") from exc
        message = str(payload.get("msg") or payload.get("message") or "")
        success = payload.get("success")
        if success is True or (isinstance(success, str) and success.strip().lower() in {"true", "1"}):
            return "预约成功"
        risk_text = message.lower()
        if any(value in risk_text for value in ("登录", "login", "重新登录")):
            raise ReservationError("LOGIN_REQUIRED", message or "登录会话失效")
        if any(value in risk_text for value in ("验证码", "captcha", "点选", "滑块")):
            raise ReservationError("CAPTCHA_REQUIRED", message or "平台要求验证码")
        if any(value in risk_text for value in ("非法", "验证超时", "302", "303", "人数过多", "安全验证", "频繁")):
            raise ReservationError("BLOCKED_BY_RISK", message or "平台拒绝本次请求")
        if any(value in risk_text for value in ("已被", "不可预约", "没有空", "已满", "冲突", "占用")):
            raise ReservationError("SEAT_UNAVAILABLE", message or "座位不可预约")
        raise ReservationError("SUBMIT_REJECTED", message or "平台拒绝本次请求，原因未能确认")
