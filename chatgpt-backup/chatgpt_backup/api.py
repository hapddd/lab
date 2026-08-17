"""Client for the ChatGPT web/desktop backend API.

Only read-only endpoints are used:

* ``GET /backend-api/conversations``            – recent conversation list
* ``GET /backend-api/conversation/{id}``        – full message tree
* ``GET /backend-api/files/{file_id}/download`` – signed URL for an attachment
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterator, List, Optional

from .assets import AssetProvider, Fetched
from .auth import Credentials, make_client, refresh_access_token, token_is_valid
from .config import log
from .http import HttpClient, HttpError
from .model import Asset, ConversationRef
from .util import to_datetime

DEFAULT_BASE_URL = "https://chatgpt.com"
PAGE_SIZE = 28


def resolve_base_url(explicit: Optional[str] = None) -> str:
    """Base URL, overridable via ``CHATGPT_API_BASE`` (used by the test server)."""
    return (explicit or os.environ.get("CHATGPT_API_BASE") or DEFAULT_BASE_URL).rstrip("/")


class AuthenticationError(RuntimeError):
    pass


class ChatGPTClient:
    def __init__(
        self,
        credentials: Credentials,
        base_url: Optional[str] = None,
        client: Optional[HttpClient] = None,
        request_delay: float = 0.5,
        timeout: float = 45.0,
        retries: int = 3,
        proxy: Optional[str] = None,
    ) -> None:
        self.credentials = credentials
        self.base_url = resolve_base_url(base_url)
        self.http = client or make_client(credentials, timeout=timeout, retries=retries, proxy=proxy)
        self.request_delay = max(0.0, request_delay)
        self._last_request = 0.0
        self._refreshed = False

    # -- plumbing ----------------------------------------------------------- #
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Referer": f"{self.base_url}/",
            "Origin": self.base_url,
            "Oai-Language": "zh-Hans",
        }
        if self.credentials.access_token:
            headers["Authorization"] = f"Bearer {self.credentials.access_token}"
        if self.credentials.device_id:
            headers["Oai-Device-Id"] = self.credentials.device_id
        if extra:
            headers.update(extra)
        return headers

    def _throttle(self) -> None:
        if self.request_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request = time.monotonic()

    def ensure_token(self) -> None:
        if token_is_valid(self.credentials.access_token):
            return
        token = refresh_access_token(self.http, self.credentials, base_url=self.base_url)
        if not token:
            raise AuthenticationError(
                "没有可用的 access_token。请先运行 `chatgpt-backup login`（或设置 CHATGPT_ACCESS_TOKEN）。"
            )

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self.ensure_token()
        url = f"{self.base_url}{path}"
        self._throttle()
        try:
            return self.http.get_json(url, headers=self._headers(), params=params)
        except HttpError as exc:
            if exc.is_auth_error and not self._refreshed:
                self._refreshed = True
                log.info("凭证被拒绝 (HTTP %s)，尝试刷新登录状态…", exc.status)
                self.credentials.access_token = None
                if refresh_access_token(self.http, self.credentials, base_url=self.base_url):
                    self._throttle()
                    return self.http.get_json(url, headers=self._headers(), params=params)
                raise AuthenticationError(
                    f"访问被拒绝 (HTTP {exc.status})。登录状态可能已过期，或被 Cloudflare 拦截；"
                    "请重新执行 `chatgpt-backup login`，必要时同时提供 cf_clearance。"
                ) from exc
            raise

    # -- endpoints ---------------------------------------------------------- #
    def account_email(self) -> Optional[str]:
        from .auth import token_account_email

        return token_account_email(self.credentials.access_token)

    def iter_conversation_refs(
        self,
        limit: int = 20,
        include_archived: bool = False,
        order: str = "updated",
    ) -> Iterator[ConversationRef]:
        fetched = 0
        offset = 0
        while fetched < limit:
            page_size = min(PAGE_SIZE, limit - fetched)
            payload = self._get(
                "/backend-api/conversations",
                {"offset": offset, "limit": page_size, "order": order},
            )
            items = (payload or {}).get("items") or []
            if not items:
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("is_archived") and not include_archived:
                    continue
                yield ConversationRef(
                    id=item.get("id") or item.get("conversation_id") or "",
                    title=item.get("title") or "未命名对话",
                    create_time=to_datetime(item.get("create_time")),
                    update_time=to_datetime(item.get("update_time")),
                    is_archived=bool(item.get("is_archived")),
                    raw=item,
                )
                fetched += 1
                if fetched >= limit:
                    return
            offset += len(items)
            total = (payload or {}).get("total")
            if isinstance(total, int) and offset >= total:
                return

    def list_conversation_refs(self, limit: int = 20, include_archived: bool = False) -> List[ConversationRef]:
        return [ref for ref in self.iter_conversation_refs(limit, include_archived) if ref.id]

    def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        payload = self._get(f"/backend-api/conversation/{conversation_id}")
        if not isinstance(payload, dict):
            raise HttpError(502, f"{self.base_url}/backend-api/conversation/{conversation_id}", b"unexpected payload")
        payload.setdefault("conversation_id", conversation_id)
        return payload

    def file_download_url(self, file_id: str) -> Optional[str]:
        candidates = [f"/backend-api/files/{file_id}/download", f"/backend-api/files/download/{file_id}"]
        # Older exports use `file-abc`, newer sediment pointers use `file_abc`.
        if file_id.startswith("file_"):
            alternate = "file-" + file_id[len("file_"):]
            candidates.append(f"/backend-api/files/{alternate}/download")
        for path in candidates:
            try:
                payload = self._get(path)
            except HttpError as exc:
                if exc.status in (400, 404):
                    continue
                raise
            if isinstance(payload, dict):
                url = payload.get("download_url") or payload.get("url")
                if url:
                    return str(url)
        return None

    def download_file(self, file_id: str) -> Optional[Fetched]:
        url = self.file_download_url(file_id)
        if not url:
            log.debug("拿不到附件下载地址: %s", file_id)
            return None
        self._throttle()
        try:
            # Signed blob URLs reject our auth headers and cookies.
            data, content_type = self.http.get_bytes(url, headers={"Accept": "*/*"}, send_cookies=False)
        except (HttpError, OSError) as exc:
            log.debug("下载附件 %s 失败: %s", file_id, exc)
            return None
        if not data:
            return None
        return Fetched(data=data, mime=content_type)


class ApiAssetProvider(AssetProvider):
    """Fetches attachments through the backend API."""

    def __init__(self, client: ChatGPTClient) -> None:
        self.client = client

    def fetch(self, asset: Asset) -> Optional[Fetched]:
        if not asset.file_id:
            return None
        return self.client.download_file(asset.file_id)
