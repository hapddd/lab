"""Credential storage and access-token refresh.

Two kinds of secrets matter:

* ``access_token`` – short lived JWT sent as ``Authorization: Bearer``.
* ``__Secure-next-auth.session-token`` – long lived cookie that can be
  exchanged for a fresh access token via ``/api/auth/session``.

Storing the session cookie means unattended backups keep working for weeks
instead of hours. ``cf_clearance`` is kept too because Cloudflare ties it to a
specific User-Agent, which we therefore persist as well.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from .config import ENV_ACCESS_TOKEN, ENV_SESSION_TOKEN, auth_file, log
from .http import DEFAULT_USER_AGENT, HttpClient, HttpError

SESSION_COOKIE = "__Secure-next-auth.session-token"
DEFAULT_BASE_URL = "https://chatgpt.com"
SESSION_PATH = "/api/auth/session"


def decode_jwt_payload(token: Optional[str]) -> Optional[Dict]:
    if not token or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, json.JSONDecodeError):
        return None


def token_expiry(token: Optional[str]) -> Optional[dt.datetime]:
    payload = decode_jwt_payload(token)
    exp = (payload or {}).get("exp")
    if not isinstance(exp, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(float(exp)).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def token_is_valid(token: Optional[str], skew_seconds: int = 600) -> bool:
    if not token:
        return False
    expiry = token_expiry(token)
    if expiry is None:
        # Opaque token: assume the caller knows what they are doing.
        return True
    return expiry > dt.datetime.now().astimezone() + dt.timedelta(seconds=skew_seconds)


def token_account_email(token: Optional[str]) -> Optional[str]:
    payload = decode_jwt_payload(token) or {}
    for key in ("https://api.openai.com/profile", "profile"):
        profile = payload.get(key)
        if isinstance(profile, dict) and profile.get("email"):
            return str(profile["email"])
    return payload.get("email") if isinstance(payload.get("email"), str) else None


@dataclass
class Credentials:
    access_token: Optional[str] = None
    session_token: Optional[str] = None
    cf_clearance: Optional[str] = None
    user_agent: str = DEFAULT_USER_AGENT
    device_id: Optional[str] = None
    cookies: Dict[str, str] = field(default_factory=dict)
    source: Optional[str] = None
    path: Optional[Path] = None

    def __post_init__(self) -> None:
        if not self.device_id:
            self.device_id = str(uuid.uuid4())

    # -- persistence -------------------------------------------------------- #
    @classmethod
    def load(cls, path: Optional[Path] = None, use_env: bool = True) -> "Credentials":
        path = path or auth_file()
        data: Dict = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("凭证文件损坏，将忽略: %s (%s)", path, exc)
                data = {}
        creds = cls(
            access_token=data.get("access_token"),
            session_token=data.get("session_token"),
            cf_clearance=data.get("cf_clearance"),
            user_agent=data.get("user_agent") or DEFAULT_USER_AGENT,
            device_id=data.get("device_id"),
            cookies={k: v for k, v in (data.get("cookies") or {}).items() if isinstance(v, str)},
            source=data.get("source"),
            path=path,
        )
        if use_env:
            env_access = os.environ.get(ENV_ACCESS_TOKEN)
            env_session = os.environ.get(ENV_SESSION_TOKEN)
            if env_access:
                creds.access_token = env_access.strip()
                creds.source = "env"
            if env_session:
                creds.session_token = env_session.strip()
                creds.source = creds.source or "env"
        return creds

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or self.path or auth_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": self.access_token,
            "session_token": self.session_token,
            "cf_clearance": self.cf_clearance,
            "user_agent": self.user_agent,
            "device_id": self.device_id,
            "cookies": self.cookies,
            "source": self.source,
            "saved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self.path = path
        return path

    # -- helpers ------------------------------------------------------------ #
    @property
    def has_any(self) -> bool:
        return bool(self.access_token or self.session_token)

    def cookie_jar(self) -> Dict[str, str]:
        jar: Dict[str, str] = dict(self.cookies)
        if self.session_token:
            jar[SESSION_COOKIE] = self.session_token
        if self.cf_clearance:
            jar["cf_clearance"] = self.cf_clearance
        if self.device_id:
            jar.setdefault("oai-did", self.device_id)
        return jar

    def describe(self) -> str:
        bits = []
        if self.access_token:
            expiry = token_expiry(self.access_token)
            state = "有效" if token_is_valid(self.access_token) else "已过期"
            when = expiry.strftime("%Y-%m-%d %H:%M") if expiry else "未知有效期"
            bits.append(f"access_token: {state} (到期 {when})")
        else:
            bits.append("access_token: 缺失")
        bits.append(f"session_token: {'有' if self.session_token else '无'}")
        bits.append(f"cf_clearance: {'有' if self.cf_clearance else '无'}")
        email = token_account_email(self.access_token)
        if email:
            bits.append(f"账号: {email}")
        if self.source:
            bits.append(f"来源: {self.source}")
        return " | ".join(bits)


def make_client(creds: Credentials, timeout: float = 45.0, retries: int = 3, proxy: Optional[str] = None) -> HttpClient:
    return HttpClient(user_agent=creds.user_agent, cookies=creds.cookie_jar(), timeout=timeout, retries=retries, proxy=proxy)


def refresh_access_token(
    client: HttpClient,
    creds: Credentials,
    save: bool = True,
    base_url: str = DEFAULT_BASE_URL,
) -> Optional[str]:
    """Swap the session cookie for a fresh access token."""
    if not creds.session_token:
        return None
    base_url = base_url.rstrip("/")
    try:
        payload = client.get_json(
            f"{base_url}{SESSION_PATH}",
            headers={
                "Accept": "application/json",
                "Referer": f"{base_url}/",
                "Oai-Device-Id": creds.device_id or "",
            },
        )
    except HttpError as exc:
        if exc.status in (401, 403):
            log.warning("会话 Cookie 已失效或被 Cloudflare 拦截 (HTTP %s)，需要重新登录。", exc.status)
        else:
            log.warning("刷新 access_token 失败: %s", exc)
        return None
    except OSError as exc:
        log.warning("刷新 access_token 时网络错误: %s", exc)
        return None

    if not isinstance(payload, dict):
        return None
    token = payload.get("accessToken")
    if not token:
        log.warning("接口未返回 accessToken，登录状态可能已过期。")
        return None

    creds.access_token = token
    refreshed = client.cookies.get(SESSION_COOKIE)
    if refreshed:
        creds.session_token = refreshed
    cf = client.cookies.get("cf_clearance")
    if cf:
        creds.cf_clearance = cf
    if save:
        creds.save()
    log.debug("已刷新 access_token。")
    return token
