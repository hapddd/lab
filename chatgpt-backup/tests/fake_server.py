"""A stand-in for chatgpt.com's backend API, used by the end-to-end tests."""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from . import fixtures

ACCESS_TOKEN = "test-access-token-abcdef"
SESSION_TOKEN = "test-session-token-" + "x" * 120


class FakeChatGPT:
    """Holds the state a test wants to assert on."""

    def __init__(self, payloads: Optional[List[Dict[str, Any]]] = None) -> None:
        self.payloads = payloads if payloads is not None else [
            fixtures.conversation_payload(),
            fixtures.second_conversation_payload(),
            fixtures.archived_conversation_payload(),
        ]
        self.require_refresh = False  # first backend call answers 401
        self.refresh_calls = 0
        self.download_calls: List[str] = []
        self.conversation_calls: List[str] = []
        self.list_calls = 0
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> str:
        handler = _make_handler(self)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # A small poll interval keeps shutdown() from costing half a second.
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def __enter__(self) -> "FakeChatGPT":
        self.base_url = self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # -- data --------------------------------------------------------------- #
    def find(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        for payload in self.payloads:
            if payload.get("conversation_id") == conversation_id:
                return payload
        return None

    def touch(self, conversation_id: str, new_update_time: float, title: Optional[str] = None) -> None:
        payload = self.find(conversation_id)
        if payload is not None:
            payload["update_time"] = new_update_time
            if title:
                payload["title"] = title


def _make_handler(state: FakeChatGPT):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # silence test output
            pass

        # -- helpers -------------------------------------------------------- #
        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"))

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization") or ""
            return header == f"Bearer {ACCESS_TOKEN}"

        def _cookies(self) -> Dict[str, str]:
            raw = self.headers.get("Cookie") or ""
            jar: Dict[str, str] = {}
            for chunk in raw.split(";"):
                if "=" in chunk:
                    name, value = chunk.split("=", 1)
                    jar[name.strip()] = value.strip()
            return jar

        # -- routing -------------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if path == "/api/auth/session":
                state.refresh_calls += 1
                jar = self._cookies()
                if jar.get("__Secure-next-auth.session-token") != SESSION_TOKEN:
                    self._send_json(401, {"error": "no session"})
                    return
                state.require_refresh = False
                self._send_json(
                    200,
                    {
                        "user": {"email": "someone@example.com", "name": "Ethan"},
                        "expires": "2026-12-01T00:00:00.000Z",
                        "accessToken": ACCESS_TOKEN,
                    },
                )
                return

            if path == "/robots.txt":
                self._send(200, b"User-agent: *\n", "text/plain")
                return

            if path.startswith("/blob/"):
                file_id = path[len("/blob/"):]
                blob = fixtures.FILE_BLOBS.get(file_id)
                if not blob:
                    self._send_json(404, {"error": "not found"})
                    return
                data, mime, _name = blob
                self._send(200, data, mime)
                return

            if path.startswith("/backend-api/"):
                if state.require_refresh or not self._authorized():
                    self._send_json(401, {"detail": "Could not validate credentials"})
                    return

            if path == "/backend-api/conversations":
                state.list_calls += 1
                offset = int((query.get("offset") or ["0"])[0])
                limit = int((query.get("limit") or ["28"])[0])
                ordered = sorted(state.payloads, key=lambda item: item["update_time"], reverse=True)
                items = fixtures.conversation_list_items(ordered)
                window = items[offset : offset + limit]
                self._send_json(200, {"items": window, "total": len(items), "limit": limit, "offset": offset})
                return

            match = re.fullmatch(r"/backend-api/conversation/([^/]+)", path)
            if match:
                conversation_id = urllib.parse.unquote(match.group(1))
                state.conversation_calls.append(conversation_id)
                payload = state.find(conversation_id)
                if payload is None:
                    self._send_json(404, {"error": "conversation not found"})
                    return
                self._send_json(200, payload)
                return

            match = re.fullmatch(r"/backend-api/files/([^/]+)/download", path)
            if match:
                file_id = urllib.parse.unquote(match.group(1))
                state.download_calls.append(file_id)
                if file_id not in fixtures.FILE_BLOBS:
                    self._send_json(404, {"error": "file not found"})
                    return
                host = self.headers.get("Host") or "127.0.0.1"
                self._send_json(
                    200,
                    {
                        "status": "success",
                        "download_url": f"http://{host}/blob/{file_id}",
                        "metadata": {"file_name": fixtures.FILE_BLOBS[file_id][2]},
                    },
                )
                return

            self._send_json(404, {"error": f"unhandled path {path}"})

    return Handler
