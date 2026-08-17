"""CLI wiring and credential handling."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from chatgpt_backup import auth as auth_module
from chatgpt_backup.auth import Credentials, token_account_email, token_expiry, token_is_valid
from chatgpt_backup.cli import _parse_pasted, _parse_since, main
from chatgpt_backup.config import ENV_CONFIG_DIR, ENV_OUT_DIR, Settings, documents_dir

from .fake_server import ACCESS_TOKEN, SESSION_TOKEN, FakeChatGPT
from .test_export_import import build_export


def make_jwt(expires_in: int = 3600, email: str = "someone@example.com") -> str:
    def encode(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = encode({"alg": "RS256", "typ": "JWT"})
    body = encode(
        {
            "exp": int((dt.datetime.now() + dt.timedelta(seconds=expires_in)).timestamp()),
            "https://api.openai.com/profile": {"email": email},
        }
    )
    return f"{header}.{body}.signature"


class EnvSandbox(unittest.TestCase):
    """Keeps every test out of the real ~/.config and ~/文档."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt-cli-test-")
        self.root = Path(self.tmp.name)
        self.out = self.root / "chat_bak"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (ENV_CONFIG_DIR, ENV_OUT_DIR, "CHATGPT_ACCESS_TOKEN", "CHATGPT_SESSION_TOKEN", "CHATGPT_API_BASE")
        }
        os.environ[ENV_CONFIG_DIR] = str(self.root / "config")
        os.environ[ENV_OUT_DIR] = str(self.out)
        for key in ("CHATGPT_ACCESS_TOKEN", "CHATGPT_SESSION_TOKEN", "CHATGPT_API_BASE"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class TokenHelpersTestCase(unittest.TestCase):
    def test_expiry_parsed_from_jwt(self):
        expiry = token_expiry(make_jwt(3600))
        self.assertIsNotNone(expiry)
        self.assertGreater(expiry, dt.datetime.now().astimezone())

    def test_valid_and_expired(self):
        self.assertTrue(token_is_valid(make_jwt(7200)))
        self.assertFalse(token_is_valid(make_jwt(60)), "快过期的 token 应视为无效以触发续期")
        self.assertFalse(token_is_valid(None))

    def test_opaque_token_assumed_valid(self):
        self.assertTrue(token_is_valid("not-a-jwt"))

    def test_email_extracted(self):
        self.assertEqual(token_account_email(make_jwt(600, "me@example.com")), "me@example.com")
        self.assertIsNone(token_account_email("garbage"))


class CredentialsTestCase(EnvSandbox):
    def test_round_trip_and_permissions(self):
        creds = Credentials(access_token="a", session_token="b", cf_clearance="c", user_agent="UA/1")
        path = creds.save(self.root / "auth.json")
        self.assertTrue(path.is_file())
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode & (stat.S_IRGRP | stat.S_IROTH), 0, "凭证文件不应对其他用户可读")

        loaded = Credentials.load(path)
        self.assertEqual(loaded.access_token, "a")
        self.assertEqual(loaded.session_token, "b")
        self.assertEqual(loaded.cf_clearance, "c")
        self.assertEqual(loaded.user_agent, "UA/1")

    def test_cookie_jar_contains_session_and_cf(self):
        creds = Credentials(session_token="s", cf_clearance="c", cookies={"extra": "1"})
        jar = creds.cookie_jar()
        self.assertEqual(jar[auth_module.SESSION_COOKIE], "s")
        self.assertEqual(jar["cf_clearance"], "c")
        self.assertEqual(jar["extra"], "1")

    def test_env_overrides_file(self):
        Credentials(access_token="from-file").save(self.root / "auth.json")
        os.environ["CHATGPT_ACCESS_TOKEN"] = "from-env"
        loaded = Credentials.load(self.root / "auth.json")
        self.assertEqual(loaded.access_token, "from-env")

    def test_corrupt_file_is_ignored(self):
        path = self.root / "auth.json"
        path.write_text("{oops", encoding="utf-8")
        loaded = Credentials.load(path, use_env=False)
        self.assertFalse(loaded.has_any)

    def test_describe_is_informative(self):
        text = Credentials(access_token=make_jwt(7200), session_token="s").describe()
        self.assertIn("access_token", text)
        self.assertIn("session_token: 有", text)


class ParseHelpersTestCase(unittest.TestCase):
    def test_relative_since(self):
        now = dt.datetime.now().astimezone()
        seven_days = _parse_since("7d")
        self.assertLess(abs((now - seven_days).total_seconds() - 7 * 86400), 120)
        self.assertIsNotNone(_parse_since("3w"))
        self.assertIsNotNone(_parse_since("12h"))

    def test_absolute_since(self):
        parsed = _parse_since("2026-08-01")
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 8, 1))

    def test_bad_since_raises(self):
        import argparse

        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_since("下周二")

    def test_none_since(self):
        self.assertIsNone(_parse_since(None))

    def test_pasted_session_json(self):
        payload = json.dumps({"user": {"email": "a@b.c"}, "accessToken": "eyJa.b.c"})
        self.assertEqual(_parse_pasted(payload)["access_token"], "eyJa.b.c")

    def test_pasted_cookie_header(self):
        header = f"__Secure-next-auth.session-token={SESSION_TOKEN}; cf_clearance=abc; _ga=1"
        found = _parse_pasted(header)
        self.assertEqual(found["session_token"], SESSION_TOKEN)
        self.assertEqual(found["cf_clearance"], "abc")

    def test_pasted_bare_jwt(self):
        token = make_jwt()
        self.assertEqual(_parse_pasted(token)["access_token"], token)
        self.assertEqual(_parse_pasted(f"Bearer {token}")["access_token"], token)

    def test_pasted_long_opaque_string_treated_as_session(self):
        found = _parse_pasted("x" * 200)
        self.assertIn("session_token", found)

    def test_pasted_empty(self):
        self.assertEqual(_parse_pasted("   "), {})


class CliTestCase(EnvSandbox):
    def test_version_exits_zero(self):
        with self.assertRaises(SystemExit) as caught:
            main(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_import_command_end_to_end(self):
        export = build_export(self.root)
        self.assertEqual(main(["import", str(export), "--out", str(self.out), "-q"]), 0)
        self.assertTrue((self.out / "index.md").is_file())
        self.assertEqual(len(list((self.out / "conversations").rglob("index.md"))), 2)

    def test_import_uses_env_output_dir(self):
        export = build_export(self.root)
        self.assertEqual(main(["import", str(export), "-q"]), 0)
        self.assertTrue((self.out / "index.md").is_file())

    def test_import_missing_path_reports_error(self):
        self.assertEqual(main(["import", str(self.root / "nope.zip"), "-q"]), 2)

    def test_list_after_import(self):
        main(["import", str(build_export(self.root)), "-q"])
        self.assertEqual(main(["list", "-q"]), 0)
        self.assertEqual(main(["list", "--json", "-q"]), 0)

    def test_list_on_empty_dir(self):
        self.assertEqual(main(["list", "-q"]), 0)

    def test_backup_without_credentials_fails_clearly(self):
        self.assertEqual(main(["backup", "-q"]), 2)

    def test_config_set_and_show(self):
        self.assertEqual(main(["config", "--set", "limit=42", "--set", "include_tools=true", "-q"]), 0)
        settings = Settings.load()
        self.assertEqual(settings.limit, 42)
        self.assertTrue(settings.include_tools)

    def test_config_rejects_unknown_key(self):
        self.assertEqual(main(["config", "--set", "nonsense=1", "-q"]), 2)

    def test_dry_run_import_writes_nothing(self):
        export = build_export(self.root)
        self.assertEqual(main(["import", str(export), "--dry-run", "-q"]), 0)
        self.assertFalse((self.out / "index.md").exists())

    def test_no_args_defaults_to_backup(self):
        # No credentials configured, so this must fail with the login hint (2),
        # proving the default subcommand is `backup` rather than a usage error.
        self.assertEqual(main([]), 2)

    def test_log_file_is_written(self):
        logfile = self.root / "logs" / "run.log"
        main(["import", str(build_export(self.root)), "--log-file", str(logfile), "-q"])
        self.assertTrue(logfile.is_file())
        self.assertIn("已备份", logfile.read_text(encoding="utf-8"))


class CliAgainstFakeServerTestCase(EnvSandbox):
    def setUp(self):
        super().setUp()
        self.server = FakeChatGPT()
        os.environ["CHATGPT_API_BASE"] = self.server.start()

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def _write_creds(self):
        Credentials(access_token=ACCESS_TOKEN, session_token=SESSION_TOKEN).save()

    def test_backup_command(self):
        self._write_creds()
        self.assertEqual(main(["backup", "-n", "2", "--delay", "0", "-q"]), 0)
        files = list((self.out / "conversations").rglob("index.md"))
        self.assertEqual(len(files), 2)
        images = list((self.out / "conversations").rglob("assets/*"))
        self.assertGreater(len(images), 0)

    def test_whoami(self):
        self._write_creds()
        self.assertEqual(main(["whoami", "-q"]), 0)

    def test_whoami_without_login(self):
        self.assertEqual(main(["whoami", "-q"]), 2)

    def test_login_with_session_token(self):
        self.assertEqual(main(["login", "--session-token", SESSION_TOKEN, "-q"]), 0)
        creds = Credentials.load()
        self.assertEqual(creds.access_token, ACCESS_TOKEN, "登录时应自动换取 access_token")

    def test_login_with_bad_session_token(self):
        self.assertEqual(main(["login", "--session-token", "wrong", "-q"]), 2)

    def test_login_no_auto_without_input(self):
        self.assertEqual(main(["login", "--no-auto", "-q"]), 2)

    def test_doctor_runs(self):
        self._write_creds()
        self.assertEqual(main(["doctor", "-q"]), 0)


class DocumentsDirTestCase(unittest.TestCase):
    def test_documents_dir_is_absolute(self):
        path = documents_dir()
        self.assertTrue(path.is_absolute())


if __name__ == "__main__":
    unittest.main()
