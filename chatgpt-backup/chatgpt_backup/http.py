"""Tiny HTTP client built on urllib, with cookies, retries and gzip support."""

from __future__ import annotations

import gzip
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .config import log

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504, 520, 522, 524)


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: bytes = b"", headers: Optional[Mapping[str, str]] = None):
        self.status = status
        self.url = url
        self.body = body
        self.headers = dict(headers or {})
        snippet = body[:400].decode("utf-8", errors="replace") if body else ""
        super().__init__(f"HTTP {status} {url} {snippet}".strip())

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)


@dataclass
class Response:
    status: int
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def content_type(self) -> Optional[str]:
        return self.headers.get("content-type")

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8", errors="replace"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _decompress(body: bytes, encoding: Optional[str]) -> bytes:
    if not body or not encoding:
        return body
    encoding = encoding.lower()
    try:
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error) as exc:
        log.debug("响应解压失败 (%s): %s", encoding, exc)
    return body


class HttpClient:
    def __init__(
        self,
        user_agent: Optional[str] = None,
        cookies: Optional[Mapping[str, str]] = None,
        timeout: float = 45.0,
        retries: int = 3,
        proxy: Optional[str] = None,
    ) -> None:
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.cookies: Dict[str, str] = dict(cookies or {})
        self.timeout = timeout
        self.retries = max(0, retries)
        handlers = [urllib.request.HTTPSHandler()]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)

    def cookie_header(self) -> Optional[str]:
        if not self.cookies:
            return None
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items() if value)

    def update_cookies_from(self, headers: Mapping[str, str]) -> None:
        raw = headers.get("set-cookie")
        if not raw:
            return
        for chunk in raw.split("\n"):
            pair = chunk.split(";", 1)[0].strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                if value and value not in ("deleted", '""'):
                    self.cookies[name.strip()] = value.strip()

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[bytes] = None,
        json_body: Optional[Any] = None,
        send_cookies: bool = True,
        retry_status: Sequence[int] = RETRY_STATUS,
    ) -> Response:
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}{'&' if '?' in url else '?'}{query}"

        final_headers: Dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            final_headers["Content-Type"] = "application/json"
        if send_cookies:
            cookie = self.cookie_header()
            if cookie:
                final_headers["Cookie"] = cookie
        for key, value in (headers or {}).items():
            if value is None:
                final_headers.pop(key, None)
            else:
                final_headers[key] = value

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, data=data, method=method.upper(), headers=final_headers)
            try:
                with self._opener.open(request, timeout=self.timeout) as raw:
                    response = self._read(raw, url)
                self.update_cookies_from(response.headers)
                return response
            except urllib.error.HTTPError as exc:
                body = _decompress(exc.read() or b"", (exc.headers.get("Content-Encoding") if exc.headers else None))
                headers_map = {k.lower(): v for k, v in (exc.headers or {}).items()}
                self.update_cookies_from(headers_map)
                if exc.code in retry_status and attempt < self.retries:
                    delay = self._retry_delay(attempt, headers_map.get("retry-after"))
                    log.debug("HTTP %s，%.1fs 后重试: %s", exc.code, delay, url)
                    time.sleep(delay)
                    last_error = HttpError(exc.code, url, body, headers_map)
                    continue
                raise HttpError(exc.code, url, body, headers_map) from None
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
                last_error = exc
                if attempt < self.retries:
                    delay = self._retry_delay(attempt, None)
                    log.debug("网络错误 (%s)，%.1fs 后重试: %s", exc, delay, url)
                    time.sleep(delay)
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError("unreachable")

    @staticmethod
    def _read(raw: Any, url: str) -> Response:
        headers = {key.lower(): value for key, value in raw.headers.items()}
        body = _decompress(raw.read(), headers.get("content-encoding"))
        return Response(status=getattr(raw, "status", 200) or 200, url=url, headers=headers, body=body)

    def _retry_delay(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return min(60.0, max(1.0, float(retry_after)))
            except ValueError:
                pass
        return min(30.0, (2 ** attempt) * 1.5) + random.uniform(0, 0.75)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.request("GET", url, **kwargs)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise HttpError(response.status, url, response.body, response.headers) from exc

    def get_bytes(self, url: str, **kwargs: Any) -> Tuple[bytes, Optional[str]]:
        response = self.request("GET", url, **kwargs)
        return response.body, response.content_type
