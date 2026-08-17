"""Find the ChatGPT desktop app's login state on this machine.

The desktop clients are web views, so the session lives in a cookie store. Four
store formats cover every client we have seen:

* Chromium/Electron ``Cookies`` SQLite (values encrypted with an OS key)
* WebKitGTK / libsoup ``cookies.sqlite`` (plain text) – Tauri builds on Linux
* Firefox-style ``cookies.sqlite`` (plain text)
* Apple ``*.binarycookies`` – the native macOS app

Everything here is best-effort: when a store cannot be read we say so and let
the user paste the token manually instead.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..config import log

WANTED_DOMAINS = ("chatgpt.com", "chat.openai.com", "openai.com", "auth.openai.com", "auth0.openai.com")
SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
    "__Secure-next-auth.session-token.0",
)
INTERESTING_COOKIES = set(SESSION_COOKIE_NAMES) | {
    "cf_clearance",
    "oai-did",
    "oai-hlib",
    "__cf_bm",
    "_puid",
    "__Host-next-auth.csrf-token",
    "__Secure-next-auth.callback-url",
}

CHROMIUM = "chromium"
WEBKIT = "webkit"
FIREFOX = "firefox"
BINARYCOOKIES = "binarycookies"


@dataclass
class CookieSource:
    label: str
    path: Path
    kind: str
    keyring_service: Optional[str] = None
    priority: int = 50


@dataclass
class DiscoveredAuth:
    label: str
    path: Path
    cookies: Dict[str, str] = field(default_factory=dict)

    @property
    def session_token(self) -> Optional[str]:
        for name in SESSION_COOKIE_NAMES:
            if self.cookies.get(name):
                return self.cookies[name]
        return None

    @property
    def cf_clearance(self) -> Optional[str]:
        return self.cookies.get("cf_clearance")

    @property
    def usable(self) -> bool:
        return bool(self.session_token)


# --------------------------------------------------------------------------- #
# locating stores
# --------------------------------------------------------------------------- #
def _linux_app_roots() -> List[Path]:
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    share = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    roots = [config, share, home / ".var" / "app"]
    return [root for root in roots if root.is_dir()]


def _looks_like_chatgpt(name: str) -> bool:
    lowered = name.lower()
    return "chatgpt" in lowered or "openai" in lowered or lowered in ("chatbox", "chatall", "opencat")


def _chromium_cookie_files(app_dir: Path) -> Iterable[Path]:
    for relative in ("Cookies", "Network/Cookies", "Default/Cookies", "Default/Network/Cookies"):
        candidate = app_dir / relative
        if candidate.is_file():
            yield candidate


def desktop_cookie_sources() -> List[CookieSource]:
    """All plausible cookie stores, desktop apps first, browsers as fallback."""
    sources: List[CookieSource] = []
    home = Path.home()

    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        containers = home / "Library" / "Containers"
        for base in (containers / "com.openai.chat" / "Data" / "Library", home / "Library"):
            if base.is_dir():
                for candidate in list(base.glob("**/*.binarycookies"))[:40]:
                    if _looks_like_chatgpt(candidate.name) or "com.openai" in str(candidate):
                        sources.append(
                            CookieSource("ChatGPT for macOS", candidate, BINARYCOOKIES, priority=5)
                        )
        if support.is_dir():
            for app_dir in support.iterdir():
                if not app_dir.is_dir() or not _looks_like_chatgpt(app_dir.name):
                    continue
                for cookie_file in _chromium_cookie_files(app_dir):
                    sources.append(
                        CookieSource(f"桌面应用 {app_dir.name}", cookie_file, CHROMIUM, app_dir.name, 10)
                    )
        browsers = {
            "Google/Chrome": "Chrome",
            "Microsoft Edge": "Microsoft Edge",
            "BraveSoftware/Brave-Browser": "Brave",
            "Vivaldi": "Vivaldi",
            "Arc": "Arc",
        }
        for relative, keyring in browsers.items():
            base = support / relative
            for cookie_file in _chromium_cookie_files(base):
                sources.append(CookieSource(f"浏览器 {keyring}", cookie_file, CHROMIUM, keyring, 60))
        firefox = support / "Firefox" / "Profiles"
        if firefox.is_dir():
            for profile in firefox.glob("*/cookies.sqlite"):
                sources.append(CookieSource("浏览器 Firefox", profile, FIREFOX, priority=70))

    elif os.name == "nt":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        localapp = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        for base in (appdata, localapp, localapp / "Packages"):
            if not base.is_dir():
                continue
            try:
                children = list(base.iterdir())
            except OSError:
                continue
            for app_dir in children:
                if not app_dir.is_dir() or not _looks_like_chatgpt(app_dir.name):
                    continue
                for cookie_file in _chromium_cookie_files(app_dir):
                    sources.append(CookieSource(f"桌面应用 {app_dir.name}", cookie_file, CHROMIUM, app_dir.name, 10))
                for cookie_file in list(app_dir.glob("**/Network/Cookies"))[:10]:
                    sources.append(CookieSource(f"桌面应用 {app_dir.name}", cookie_file, CHROMIUM, app_dir.name, 15))
        for relative, label in (
            ("Google/Chrome/User Data", "Chrome"),
            ("Microsoft/Edge/User Data", "Microsoft Edge"),
            ("BraveSoftware/Brave-Browser/User Data", "Brave"),
        ):
            base = localapp / relative
            for profile in ("Default", "Profile 1"):
                for cookie_file in _chromium_cookie_files(base / profile):
                    sources.append(CookieSource(f"浏览器 {label}", cookie_file, CHROMIUM, label, 60))
        firefox = appdata / "Mozilla" / "Firefox" / "Profiles"
        if firefox.is_dir():
            for profile in firefox.glob("*/cookies.sqlite"):
                sources.append(CookieSource("浏览器 Firefox", profile, FIREFOX, priority=70))

    else:
        for root in _linux_app_roots():
            try:
                children = list(root.iterdir())
            except OSError:
                continue
            for app_dir in children:
                if not app_dir.is_dir() or not _looks_like_chatgpt(app_dir.name):
                    continue
                for cookie_file in _chromium_cookie_files(app_dir):
                    sources.append(CookieSource(f"桌面应用 {app_dir.name}", cookie_file, CHROMIUM, app_dir.name, 10))
                for name in ("cookies.sqlite", "cookies.db", "soupcookies.sqlite"):
                    candidate = app_dir / name
                    if candidate.is_file():
                        sources.append(CookieSource(f"桌面应用 {app_dir.name}", candidate, WEBKIT, priority=10))
                for candidate in list(app_dir.glob("**/cookies.sqlite"))[:10]:
                    sources.append(CookieSource(f"桌面应用 {app_dir.name}", candidate, WEBKIT, priority=20))

        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        for relative, keyring in (
            ("google-chrome", "Chrome"),
            ("chromium", "Chromium"),
            ("microsoft-edge", "Microsoft Edge"),
            ("BraveSoftware/Brave-Browser", "Brave"),
        ):
            base = config / relative
            for profile in ("Default", "Profile 1", ""):
                for cookie_file in _chromium_cookie_files(base / profile if profile else base):
                    sources.append(CookieSource(f"浏览器 {keyring}", cookie_file, CHROMIUM, keyring, 60))
        for profile in (home / ".mozilla" / "firefox").glob("*/cookies.sqlite"):
            sources.append(CookieSource("浏览器 Firefox", profile, FIREFOX, priority=70))

    unique: Dict[Path, CookieSource] = {}
    for source in sources:
        try:
            resolved = source.path.resolve()
        except OSError:
            resolved = source.path
        if resolved not in unique or source.priority < unique[resolved].priority:
            unique[resolved] = source
    return sorted(unique.values(), key=lambda item: (item.priority, str(item.path)))


# --------------------------------------------------------------------------- #
# decryption helpers
# --------------------------------------------------------------------------- #
def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> Optional[bytes]:
    """AES-128-CBC without padding removal; uses `cryptography` or the openssl CLI."""
    if not data or len(data) % 16:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        return decryptor.update(data) + decryptor.finalize()
    except ImportError:
        pass
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    try:
        result = subprocess.run(
            [openssl, "enc", "-d", "-aes-128-cbc", "-nopad", "-K", key.hex(), "-iv", iv.hex()],
            input=data,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("openssl 解密失败: %s", exc)
        return None
    return result.stdout if result.returncode == 0 else None


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def _keyring_passwords(service_hint: Optional[str]) -> List[bytes]:
    """Candidate os_crypt passwords, most specific first."""
    passwords: List[bytes] = []
    if sys.platform == "darwin":
        services = [name for name in (service_hint, "Chrome", "Chromium", "Microsoft Edge", "Brave") if name]
        security = shutil.which("security")
        if security:
            for service in services:
                for account in (service, f"{service} Safe Storage"):
                    try:
                        result = subprocess.run(
                            [security, "find-generic-password", "-w", "-s", f"{service} Safe Storage", "-a", account],
                            capture_output=True,
                            text=True,
                            timeout=20,
                            check=False,
                        )
                    except (OSError, subprocess.SubprocessError):
                        continue
                    if result.returncode == 0 and result.stdout.strip():
                        passwords.append(result.stdout.strip().encode("utf-8"))
    else:
        secret_tool = shutil.which("secret-tool")
        if secret_tool:
            attempts = []
            if service_hint:
                attempts.append(["application", service_hint.lower()])
            attempts += [["application", "chrome"], ["application", "chromium"], ["application", "brave"]]
            for attributes in attempts:
                try:
                    result = subprocess.run(
                        [secret_tool, "lookup", *attributes],
                        capture_output=True,
                        timeout=20,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                if result.returncode == 0 and result.stdout.strip():
                    passwords.append(result.stdout.strip())
    passwords.append(b"peanuts")  # v10 fallback used when no keyring is available
    seen = set()
    unique: List[bytes] = []
    for password in passwords:
        if password not in seen:
            seen.add(password)
            unique.append(password)
    return unique


def _decrypt_chromium_value(
    encrypted: bytes,
    service_hint: Optional[str],
    store_path: Optional[Path] = None,
) -> Optional[str]:
    if not encrypted:
        return None
    prefix = encrypted[:3]

    if os.name == "nt" and prefix in (b"v10", b"v11", b"v20"):
        return _decrypt_windows(encrypted, store_path)

    if prefix in (b"v10", b"v11"):
        body = encrypted[3:]
        iterations = 1003 if sys.platform == "darwin" else 1
        for password in _keyring_passwords(service_hint):
            key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, 16)
            plain = _aes_cbc_decrypt(key, b" " * 16, body)
            if plain is None:
                continue
            plain = _strip_pkcs7(plain)
            text = _plaintext_to_str(plain)
            if text:
                return text
        return None

    # Unencrypted store (older Linux builds without a keyring).
    return _plaintext_to_str(encrypted)


def _plaintext_to_str(plain: bytes) -> Optional[str]:
    """Decode a decrypted cookie value, rejecting garbage from a wrong key."""
    for candidate in (plain, plain[32:]):  # Chrome 130+ prefixes a 32-byte domain hash
        try:
            text = candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not text:
            continue
        if text.isprintable() or ("\n" not in text and "\x00" not in text):
            return text
    return None


def _dpapi_unprotect(data: bytes) -> Optional[bytes]:  # pragma: no cover - Windows only
    import ctypes
    import ctypes.wintypes as wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _windows_master_key(store_path: Optional[Path]) -> Optional[bytes]:  # pragma: no cover - Windows only
    """The AES key from ``Local State``, unwrapped with DPAPI."""
    if store_path is None:
        return None
    for parent in list(store_path.parents)[:5]:
        candidate = parent / "Local State"
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        encoded = (data.get("os_crypt") or {}).get("encrypted_key")
        if not encoded:
            continue
        try:
            blob = base64.b64decode(encoded)
        except (ValueError, TypeError):
            continue
        if blob[:5] == b"DPAPI":
            blob = blob[5:]
        key = _dpapi_unprotect(blob)
        if key:
            return key
    return None


def _decrypt_windows(encrypted: bytes, store_path: Optional[Path]) -> Optional[str]:  # pragma: no cover - Windows only
    """AES-GCM with the profile key, falling back to raw DPAPI for old stores."""
    prefix = encrypted[:3]

    if prefix == b"v20":
        log.debug("这个 Cookie 用了 Chrome 的应用绑定加密 (v20)，无法在普通权限下解密")
        return None

    if prefix in (b"v10", b"v11"):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            log.debug("解密 Windows 上的 Cookie 需要 cryptography 库: pip install cryptography")
            return None
        key = _windows_master_key(store_path)
        if not key:
            log.debug("没能从 Local State 里取出主密钥")
            return None
        nonce, payload = encrypted[3:15], encrypted[15:]
        try:
            plain = AESGCM(key).decrypt(nonce, payload, None)
        except Exception as exc:
            log.debug("AES-GCM 解密失败: %s", exc)
            return None
        return _plaintext_to_str(plain)

    plain = _dpapi_unprotect(encrypted)
    return _plaintext_to_str(plain) if plain else None


# --------------------------------------------------------------------------- #
# reading stores
# --------------------------------------------------------------------------- #
def _copy_for_read(path: Path) -> Tuple[Path, tempfile.TemporaryDirectory]:
    holder = tempfile.TemporaryDirectory(prefix="chatgpt-backup-cookies-")
    target = Path(holder.name) / path.name
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_file():
            try:
                shutil.copy2(sidecar, target.with_name(target.name + suffix))
            except OSError:
                pass
    return target, holder


def _query_sqlite(path: Path, sql: str) -> List[tuple]:
    copied, holder = _copy_for_read(path)
    try:
        connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()
    finally:
        holder.cleanup()


def _wanted(host: str, name: str) -> bool:
    host = (host or "").lstrip(".").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in WANTED_DOMAINS):
        return False
    return name in INTERESTING_COOKIES


def read_chromium_cookies(source: CookieSource) -> Dict[str, str]:
    rows = _query_sqlite(source.path, "SELECT host_key, name, value, encrypted_value FROM cookies")
    cookies: Dict[str, str] = {}
    for host, name, value, encrypted in rows:
        if not _wanted(host or "", name or ""):
            continue
        if value:
            cookies[name] = value
            continue
        if isinstance(encrypted, (bytes, bytearray)) and encrypted:
            decrypted = _decrypt_chromium_value(bytes(encrypted), source.keyring_service, source.path)
            if decrypted:
                cookies[name] = decrypted
    return cookies


def read_plain_sqlite_cookies(source: CookieSource) -> Dict[str, str]:
    for sql in (
        "SELECT host, name, value FROM moz_cookies",
        "SELECT domain, name, value FROM cookies",
    ):
        try:
            rows = _query_sqlite(source.path, sql)
        except sqlite3.Error:
            continue
        cookies = {name: value for host, name, value in rows if _wanted(host or "", name or "") and value}
        if cookies:
            return cookies
    return {}


def read_binarycookies(source: CookieSource) -> Dict[str, str]:
    """Parse Apple's ``*.binarycookies`` container format."""
    try:
        blob = source.path.read_bytes()
    except OSError as exc:
        log.debug("读取 %s 失败: %s", source.path, exc)
        return {}
    if len(blob) < 8 or blob[:4] != b"cook":
        return {}

    cookies: Dict[str, str] = {}
    (page_count,) = struct.unpack(">i", blob[4:8])
    if page_count <= 0 or page_count > 10000:
        return {}
    offset = 8
    try:
        page_sizes = struct.unpack(f">{page_count}i", blob[offset : offset + 4 * page_count])
    except struct.error:
        return {}
    offset += 4 * page_count

    for size in page_sizes:
        page = blob[offset : offset + size]
        offset += size
        if len(page) < 12:
            continue
        try:
            (cookie_count,) = struct.unpack("<i", page[4:8])
            cookie_offsets = struct.unpack(f"<{cookie_count}i", page[8 : 8 + 4 * cookie_count])
        except struct.error:
            continue
        for cookie_offset in cookie_offsets:
            record = page[cookie_offset:]
            if len(record) < 48:
                continue
            try:
                url_off, name_off, path_off, value_off = struct.unpack("<iiii", record[16:32])
            except struct.error:
                continue

            def read_string(start: int) -> str:
                if start <= 0 or start >= len(record):
                    return ""
                end = record.find(b"\x00", start)
                chunk = record[start : end if end != -1 else len(record)]
                return chunk.decode("utf-8", errors="replace")

            host = read_string(url_off)
            name = read_string(name_off)
            value = read_string(value_off)
            if value and _wanted(host, name):
                cookies[name] = value
    return cookies


READERS = {
    CHROMIUM: read_chromium_cookies,
    WEBKIT: read_plain_sqlite_cookies,
    FIREFOX: read_plain_sqlite_cookies,
    BINARYCOOKIES: read_binarycookies,
}


def read_cookies(source: CookieSource) -> Dict[str, str]:
    reader = READERS.get(source.kind)
    if reader is None:
        return {}
    try:
        return reader(source)
    except (sqlite3.Error, OSError, struct.error, ValueError) as exc:
        log.debug("解析 Cookie 存储 %s 失败: %s", source.path, exc)
        return {}


def discover_auth(include_browsers: bool = True, limit: int = 25) -> List[DiscoveredAuth]:
    """Return every cookie store that yielded a usable ChatGPT session."""
    results: List[DiscoveredAuth] = []
    for source in desktop_cookie_sources()[:limit]:
        if not include_browsers and source.label.startswith("浏览器"):
            continue
        cookies = read_cookies(source)
        if not cookies:
            continue
        found = DiscoveredAuth(label=source.label, path=source.path, cookies=cookies)
        log.debug("在 %s 找到 %d 个相关 Cookie", source.path, len(cookies))
        if found.usable:
            results.insert(0, found)
        else:
            results.append(found)
    return results


def app_data_dirs() -> List[Path]:
    """Directories that look like a ChatGPT desktop app profile (for `doctor`)."""
    found: List[Path] = []
    if sys.platform == "darwin":
        roots = [
            Path.home() / "Library" / "Application Support",
            Path.home() / "Library" / "Containers",
        ]
    elif os.name == "nt":
        roots = [
            Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"),
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"),
        ]
    else:
        roots = _linux_app_roots()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                if child.is_dir() and _looks_like_chatgpt(child.name):
                    found.append(child)
        except OSError:
            continue
    return found
