"""Cookie extraction from the four store formats the desktop clients use."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from chatgpt_backup.sources import desktop
from chatgpt_backup.sources.desktop import (
    BINARYCOOKIES,
    CHROMIUM,
    FIREFOX,
    WEBKIT,
    CookieSource,
    DiscoveredAuth,
    discover_auth,
    read_cookies,
)

SESSION_VALUE = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..session-cookie-value"
CF_VALUE = "cf-clearance-value-123"


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    padded = data + bytes([pad]) * pad
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(padded) + encryptor.finalize()
    except ImportError:
        openssl = shutil.which("openssl")
        if not openssl:
            raise unittest.SkipTest("需要 cryptography 或 openssl 才能构造测试数据")
        result = subprocess.run(
            [openssl, "enc", "-aes-128-cbc", "-nopad", "-K", key.hex(), "-iv", iv.hex()],
            input=padded,
            capture_output=True,
            check=True,
        )
        return result.stdout


def _chromium_v10(value: str) -> bytes:
    iterations = 1003 if sys.platform == "darwin" else 1
    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", iterations, 16)
    return b"v10" + _aes_cbc_encrypt(key, b" " * 16, value.encode("utf-8"))


def write_chromium_store(path: Path, encrypted: bool = True, with_hash_prefix: bool = False) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT)"
    )
    rows = []
    if encrypted:
        payload = SESSION_VALUE
        if with_hash_prefix:
            # Chrome 130+ prepends a 32-byte domain hash to the plaintext.
            iterations = 1003 if sys.platform == "darwin" else 1
            key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", iterations, 16)
            blob = b"v10" + _aes_cbc_encrypt(
                key, b" " * 16, hashlib.sha256(b"chatgpt.com").digest() + SESSION_VALUE.encode()
            )
            rows.append((".chatgpt.com", "__Secure-next-auth.session-token", "", blob, "/"))
        else:
            rows.append((".chatgpt.com", "__Secure-next-auth.session-token", "", _chromium_v10(payload), "/"))
        rows.append((".chatgpt.com", "cf_clearance", "", _chromium_v10(CF_VALUE), "/"))
    else:
        rows.append((".chatgpt.com", "__Secure-next-auth.session-token", SESSION_VALUE, b"", "/"))
        rows.append((".chatgpt.com", "cf_clearance", CF_VALUE, b"", "/"))
    # Noise that must be filtered out.
    rows.append((".example.com", "__Secure-next-auth.session-token", "other-site", b"", "/"))
    rows.append((".chatgpt.com", "_ga", "irrelevant", b"", "/"))
    connection.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()
    return path


def write_moz_store(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT)")
    connection.executemany(
        "INSERT INTO moz_cookies (host, name, value) VALUES (?, ?, ?)",
        [
            (".chatgpt.com", "__Secure-next-auth.session-token", SESSION_VALUE),
            (".chatgpt.com", "cf_clearance", CF_VALUE),
            (".unrelated.com", "cf_clearance", "nope"),
        ],
    )
    connection.commit()
    connection.close()
    return path


def write_binarycookies(path: Path) -> Path:
    """Build a minimal but structurally valid Apple binarycookies file."""

    def cookie(url: str, name: str, value: str) -> bytes:
        path_str = "/"
        header_len = 56
        url_off = header_len
        name_off = url_off + len(url) + 1
        path_off = name_off + len(name) + 1
        value_off = path_off + len(path_str) + 1
        strings = b"".join(item.encode("utf-8") + b"\x00" for item in (url, name, path_str, value))
        size = header_len + len(strings)
        return (
            struct.pack("<i", size)
            + b"\x00" * 4
            + struct.pack("<i", 0)
            + b"\x00" * 4
            + struct.pack("<iiii", url_off, name_off, path_off, value_off)
            + b"\x00" * 8
            + struct.pack("<d", 0.0)
            + struct.pack("<d", 0.0)
            + strings
        )

    cookies = [
        cookie(".chatgpt.com", "__Secure-next-auth.session-token", SESSION_VALUE),
        cookie(".chatgpt.com", "cf_clearance", CF_VALUE),
        cookie(".other.com", "cf_clearance", "nope"),
    ]
    offsets = []
    cursor = 8 + 4 * len(cookies) + 4
    for blob in cookies:
        offsets.append(cursor)
        cursor += len(blob)
    page = (
        b"\x00\x00\x01\x00"
        + struct.pack("<i", len(cookies))
        + b"".join(struct.pack("<i", offset) for offset in offsets)
        + b"\x00\x00\x00\x00"
        + b"".join(cookies)
    )
    blob = b"cook" + struct.pack(">i", 1) + struct.pack(">i", len(page)) + page
    path.write_bytes(blob)
    return path


class CookieStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt-cookies-test-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_chromium_plaintext_values(self):
        path = write_chromium_store(self.root / "Cookies", encrypted=False)
        cookies = read_cookies(CookieSource("test", path, CHROMIUM))
        self.assertEqual(cookies["__Secure-next-auth.session-token"], SESSION_VALUE)
        self.assertEqual(cookies["cf_clearance"], CF_VALUE)

    def test_chromium_encrypted_values(self):
        path = write_chromium_store(self.root / "Cookies", encrypted=True)
        cookies = read_cookies(CookieSource("test", path, CHROMIUM))
        self.assertEqual(cookies.get("__Secure-next-auth.session-token"), SESSION_VALUE)
        self.assertEqual(cookies.get("cf_clearance"), CF_VALUE)

    def test_chromium_new_hash_prefixed_plaintext(self):
        path = write_chromium_store(self.root / "Cookies", encrypted=True, with_hash_prefix=True)
        cookies = read_cookies(CookieSource("test", path, CHROMIUM))
        self.assertEqual(cookies.get("__Secure-next-auth.session-token"), SESSION_VALUE)

    def test_other_domains_and_boring_names_filtered(self):
        path = write_chromium_store(self.root / "Cookies", encrypted=False)
        cookies = read_cookies(CookieSource("test", path, CHROMIUM))
        self.assertNotIn("_ga", cookies)
        self.assertNotEqual(cookies["__Secure-next-auth.session-token"], "other-site")

    def test_webkit_and_firefox_plain_sqlite(self):
        path = write_moz_store(self.root / "cookies.sqlite")
        for kind in (WEBKIT, FIREFOX):
            cookies = read_cookies(CookieSource("test", path, kind))
            self.assertEqual(cookies["__Secure-next-auth.session-token"], SESSION_VALUE)
            self.assertNotIn("nope", cookies.values())

    def test_binarycookies(self):
        path = write_binarycookies(self.root / "Cookies.binarycookies")
        cookies = read_cookies(CookieSource("test", path, BINARYCOOKIES))
        self.assertEqual(cookies.get("__Secure-next-auth.session-token"), SESSION_VALUE)
        self.assertEqual(cookies.get("cf_clearance"), CF_VALUE)

    def test_garbage_files_return_nothing_without_raising(self):
        junk = self.root / "junk"
        junk.write_bytes(b"not a database at all")
        for kind in (CHROMIUM, WEBKIT, FIREFOX, BINARYCOOKIES):
            self.assertEqual(read_cookies(CookieSource("test", junk, kind)), {})

    def test_locked_store_is_copied_before_reading(self):
        path = write_chromium_store(self.root / "Cookies", encrypted=False)
        connection = sqlite3.connect(path)
        connection.execute("BEGIN EXCLUSIVE")
        try:
            cookies = read_cookies(CookieSource("test", path, CHROMIUM))
            self.assertIn("__Secure-next-auth.session-token", cookies)
        finally:
            connection.rollback()
            connection.close()


class DiscoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt-discovery-test-")
        self.root = Path(self.tmp.name)
        self._original = desktop.desktop_cookie_sources

    def tearDown(self):
        desktop.desktop_cookie_sources = self._original
        self.tmp.cleanup()

    def test_prefers_store_that_has_a_session(self):
        empty = self.root / "empty-Cookies"
        sqlite3.connect(empty).close()
        good = write_moz_store(self.root / "cookies.sqlite")
        partial = self.root / "partial.sqlite"
        connection = sqlite3.connect(partial)
        connection.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        connection.execute("INSERT INTO moz_cookies VALUES ('.chatgpt.com', 'cf_clearance', 'only-cf')")
        connection.commit()
        connection.close()

        desktop.desktop_cookie_sources = lambda: [
            CookieSource("空存储", empty, WEBKIT, priority=1),
            CookieSource("只有 cf", partial, WEBKIT, priority=2),
            CookieSource("桌面应用 ChatGPT", good, WEBKIT, priority=3),
        ]
        results = discover_auth()
        self.assertTrue(results)
        self.assertTrue(results[0].usable)
        self.assertEqual(results[0].session_token, SESSION_VALUE)

    def test_browsers_can_be_excluded(self):
        good = write_moz_store(self.root / "cookies.sqlite")
        desktop.desktop_cookie_sources = lambda: [CookieSource("浏览器 Chrome", good, WEBKIT)]
        self.assertEqual(discover_auth(include_browsers=False), [])
        self.assertTrue(discover_auth(include_browsers=True))

    def test_real_scan_does_not_crash(self):
        # Whatever this machine looks like, discovery must not raise.
        desktop.desktop_cookie_sources = self._original
        self.assertIsInstance(desktop.desktop_cookie_sources(), list)
        self.assertIsInstance(discover_auth(), list)
        self.assertIsInstance(desktop.app_data_dirs(), list)


class DiscoveredAuthTestCase(unittest.TestCase):
    def test_properties(self):
        found = DiscoveredAuth(
            label="x",
            path=Path("/tmp/x"),
            cookies={"__Secure-next-auth.session-token": "abc", "cf_clearance": "def"},
        )
        self.assertEqual(found.session_token, "abc")
        self.assertEqual(found.cf_clearance, "def")
        self.assertTrue(found.usable)

    def test_unusable_without_session(self):
        found = DiscoveredAuth(label="x", path=Path("/tmp/x"), cookies={"cf_clearance": "def"})
        self.assertFalse(found.usable)
        self.assertIsNone(found.session_token)

    def test_legacy_cookie_name_accepted(self):
        found = DiscoveredAuth(label="x", path=Path("/tmp/x"), cookies={"next-auth.session-token": "abc"})
        self.assertEqual(found.session_token, "abc")


if __name__ == "__main__":
    unittest.main()
