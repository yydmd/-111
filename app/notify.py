"""Push notifications for reservation outcomes.

Supported channels (single choice, stored in the local settings table):
- serverchan: Server酱 SendKey, pushes to WeChat via sctapi.ftqq.com
- bark: iOS Bark app key (api.day.app)
- wecom_webhook: 企业微信群机器人 webhook URL

Delivery is best-effort: notification problems are logged, never raised into
the reservation flow. Message bodies contain run metadata only — statuses,
seats and already-redacted platform messages; never credentials or tokens.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from sqlalchemy import select

from .db import AppSetting, SessionLocal
from .security import protect_secret, unprotect_secret

logger = logging.getLogger(__name__)
NOTIFY_TYPES = ("none", "serverchan", "bark", "wecom_webhook")
TIMEOUT = (3, 8)
_session = requests.Session()
_session.trust_env = False
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")


def _settings_rows() -> dict[str, str]:
    db = SessionLocal()
    try:
        return {row.key: (row.value or "") for row in db.scalars(select(AppSetting))}
    finally:
        db.close()


def load_settings() -> dict:
    """Return safe UI state only; never send notification secrets to a browser."""
    rows = _settings_rows()
    return {
        "notify_type": rows.get("notify_type", "none"),
        "notify_key_configured": bool(rows.get("notify_key", "")),
    }


def save_settings(notify_type: str, notify_key: str) -> None:
    if notify_type not in NOTIFY_TYPES:
        raise ValueError("不支持的通知类型")
    db = SessionLocal()
    try:
        existing_type = (db.get(AppSetting, "notify_type").value if db.get(AppSetting, "notify_type") else "none")
        existing_key = (db.get(AppSetting, "notify_key").value if db.get(AppSetting, "notify_key") else "")
        provided_key = notify_key.strip()
        if notify_type == "none":
            stored_key = ""
        elif provided_key:
            stored_key = protect_secret(provided_key)
        elif existing_type == notify_type and existing_key:
            # Preserve an intentionally masked existing key, and rewrite a
            # legacy plaintext value into DPAPI protection on the next save.
            stored_key = protect_secret(unprotect_secret(existing_key))
        else:
            raise ValueError("已选择通知渠道，请填写对应的 Key 或 Webhook")
        for key, value in (("notify_type", notify_type), ("notify_key", stored_key)):
            row = db.get(AppSetting, key)
            if row is None:
                db.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        db.commit()
    finally:
        db.close()


def _dispatch(cfg: dict, title: str, body: str) -> str | None:
    """Send one notification; returns None on success or an error string."""
    kind = cfg.get("notify_type") or "none"
    key = (cfg.get("notify_key") or "").strip()
    if kind == "none" or not key:
        return "未配置通知渠道"
    try:
        if kind == "serverchan":
            response = _session.post(f"https://sctapi.ftqq.com/{key}.send", data={"title": title, "desp": body}, timeout=TIMEOUT)
        elif kind == "bark":
            response = _session.post(f"https://api.day.app/{key}", json={"title": title, "body": body}, timeout=TIMEOUT)
        elif kind == "wecom_webhook":
            response = _session.post(key, json={"msgtype": "text", "text": {"content": f"{title}\n{body}"}}, timeout=TIMEOUT)
        else:
            return f"未知通知类型 {kind}"
    except requests.RequestException as exc:
        return exc.__class__.__name__
    if response.status_code >= 400:
        return f"HTTP {response.status_code}"
    return None


def send(title: str, body: str) -> str | None:
    rows = _settings_rows()
    try:
        key = unprotect_secret(rows.get("notify_key", ""))
    except Exception:
        logger.warning("notification key could not be decrypted")
        return "通知密钥无法解密；请重新保存通知设置"
    return _dispatch({"notify_type": rows.get("notify_type", "none"), "notify_key": key}, title, body)


def submit_async(title: str, body: str) -> None:
    def job() -> None:
        error = send(title, body)
        if error:
            logger.info("notification not delivered: %s", error)

    _executor.submit(job)
