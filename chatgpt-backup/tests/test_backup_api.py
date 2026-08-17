"""End-to-end: mock backend API -> markdown + images on disk."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chatgpt_backup.api import ChatGPTClient
from chatgpt_backup.auth import Credentials, make_client
from chatgpt_backup.backup import BackupOptions, backup_from_api
from chatgpt_backup.config import STATE_FILE

from . import fixtures
from .fake_server import ACCESS_TOKEN, SESSION_TOKEN, FakeChatGPT


class ApiBackupTestCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeChatGPT()
        self.base_url = self.server.start()
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt-backup-test-")
        self.out = Path(self.tmp.name) / "chat_bak"
        self.creds = Credentials(
            access_token=ACCESS_TOKEN,
            session_token=SESSION_TOKEN,
            path=Path(self.tmp.name) / "auth.json",
        )

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _client(self) -> ChatGPTClient:
        return ChatGPTClient(
            self.creds,
            base_url=self.base_url,
            client=make_client(self.creds),
            request_delay=0.0,
        )

    def _options(self, **overrides) -> BackupOptions:
        params = dict(output_dir=self.out, limit=10, download_assets=True)
        params.update(overrides)
        return BackupOptions(**params)

    def _markdown_files(self):
        return sorted((self.out / "conversations").rglob("index.md"))

    # -- basics ------------------------------------------------------------- #
    def test_backup_writes_markdown_and_images(self):
        result = backup_from_api(self._client(), self._options())

        self.assertEqual(result.failed, 0, result.errors)
        self.assertEqual(result.written, 2, "归档对话默认不备份")
        self.assertGreater(result.assets_saved, 0)

        files = self._markdown_files()
        self.assertEqual(len(files), 2)

        target = next(path for path in files if "退出账号记录保存" in str(path))
        text = target.read_text(encoding="utf-8")
        self.assertIn("# 退出账号记录保存", text)
        self.assertIn("请问 如果我退出了当前账号", text)
        self.assertIn("![", text)

        images = sorted((target.parent / "assets").iterdir())
        self.assertGreaterEqual(len(images), 2)
        for image in images:
            self.assertGreater(image.stat().st_size, 0)
        self.assertTrue(any(path.suffix == ".png" for path in images))
        self.assertTrue(any(path.suffix == ".pdf" for path in images), "PDF 附件也应保存")

    def test_image_links_resolve_relative_to_markdown(self):
        backup_from_api(self._client(), self._options())
        target = next(path for path in self._markdown_files() if "退出账号" in str(path))
        text = target.read_text(encoding="utf-8")
        links = [
            line.split("](", 1)[1].split(")", 1)[0]
            for line in text.splitlines()
            if line.startswith("![") or line.startswith("[附件")
        ]
        self.assertTrue(links)
        for link in links:
            resolved = (target.parent / link.replace("%20", " ")).resolve()
            self.assertTrue(resolved.is_file(), f"链接指向的文件不存在: {link}")

    def test_index_and_state_written(self):
        backup_from_api(self._client(), self._options())
        index = self.out / "index.md"
        self.assertTrue(index.is_file())
        index_text = index.read_text(encoding="utf-8")
        self.assertIn("退出账号记录保存", index_text)
        self.assertIn("配置 Clash Verge 多订阅合并", index_text)

        state = json.loads((self.out / STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(len(state["conversations"]), 2)
        entry = state["conversations"][fixtures.conversation_payload()["conversation_id"]]
        self.assertGreater(entry["message_count"], 0)
        self.assertGreater(entry["asset_count"], 0)

    def test_default_output_layout_is_one_folder_per_conversation(self):
        backup_from_api(self._client(), self._options())
        folders = [path for path in (self.out / "conversations").iterdir() if path.is_dir()]
        self.assertEqual(len(folders), 2)
        for folder in folders:
            self.assertTrue((folder / "index.md").is_file())
            self.assertRegex(folder.name, r"^\d{4}-\d{2}-\d{2}-")

    def test_flat_layout(self):
        backup_from_api(self._client(), self._options(layout="flat"))
        files = sorted((self.out / "conversations").glob("*.md"))
        self.assertEqual(len(files), 2)
        target = next(path for path in files if "退出账号" in path.name)
        text = target.read_text(encoding="utf-8")
        link = next(line for line in text.splitlines() if line.startswith("!["))
        rel = link.split("](", 1)[1].split(")", 1)[0]
        self.assertTrue(rel.startswith("../assets/"))
        self.assertTrue((target.parent / rel.replace("%20", " ")).resolve().is_file())

    # -- incremental behaviour ---------------------------------------------- #
    def test_second_run_skips_unchanged(self):
        backup_from_api(self._client(), self._options())
        downloads_after_first = len(self.server.download_calls)

        second = backup_from_api(self._client(), self._options())
        self.assertEqual(second.written, 0)
        self.assertEqual(second.skipped, 2)
        self.assertEqual(len(self.server.download_calls), downloads_after_first, "不应重复下载图片")

    def test_updated_conversation_is_refetched(self):
        backup_from_api(self._client(), self._options())
        payload = fixtures.conversation_payload()
        self.server.touch(payload["conversation_id"], fixtures.BASE_TIME + 9999)

        second = backup_from_api(self._client(), self._options())
        self.assertEqual(second.written, 1)
        self.assertEqual(second.skipped, 1)

    def test_force_rewrites_everything(self):
        backup_from_api(self._client(), self._options())
        forced = backup_from_api(self._client(), self._options(force=True))
        self.assertEqual(forced.written, 2)

    def test_rename_moves_existing_folder(self):
        backup_from_api(self._client(), self._options())
        payload = fixtures.conversation_payload()
        self.server.touch(payload["conversation_id"], fixtures.BASE_TIME + 5000, title="账号与聊天记录的关系")

        backup_from_api(self._client(), self._options())
        folders = [path.name for path in (self.out / "conversations").iterdir() if path.is_dir()]
        self.assertTrue(any("账号与聊天记录的关系" in name for name in folders))
        self.assertFalse(any("退出账号记录保存" in name for name in folders), "旧目录应被改名而不是留下副本")

    # -- options ------------------------------------------------------------ #
    def test_limit_is_respected(self):
        result = backup_from_api(self._client(), self._options(limit=1))
        self.assertEqual(result.written, 1)

    def test_archived_included_on_request(self):
        result = backup_from_api(self._client(), self._options(include_archived=True))
        self.assertEqual(result.written, 3)

    def test_no_images_skips_downloads(self):
        result = backup_from_api(self._client(), self._options(download_assets=False))
        self.assertEqual(self.server.download_calls, [])
        self.assertEqual(result.assets_saved, 0)
        target = next(path for path in self._markdown_files() if "退出账号" in str(path))
        self.assertIn("未保存的附件", target.read_text(encoding="utf-8"))

    def test_dry_run_writes_nothing(self):
        result = backup_from_api(self._client(), self._options(dry_run=True))
        self.assertEqual(result.written, 2)
        self.assertFalse((self.out / "conversations").exists() and self._markdown_files())
        self.assertFalse((self.out / STATE_FILE).exists())

    def test_save_raw_keeps_original_json(self):
        backup_from_api(self._client(), self._options(save_raw=True))
        raws = list((self.out / "conversations").rglob("*.raw.json"))
        self.assertEqual(len(raws), 2)
        payload = json.loads(raws[0].read_text(encoding="utf-8"))
        self.assertIn("mapping", payload)

    def test_since_filter(self):
        import datetime as dt

        cutoff = dt.datetime.fromtimestamp(fixtures.BASE_TIME - 100).astimezone()
        result = backup_from_api(self._client(), self._options(since=cutoff))
        self.assertEqual(result.written, 1)

    # -- auth resilience ---------------------------------------------------- #
    def test_expired_token_is_refreshed_from_session_cookie(self):
        self.server.require_refresh = True
        self.creds.access_token = "stale-token"
        result = backup_from_api(self._client(), self._options())
        self.assertGreaterEqual(self.server.refresh_calls, 1)
        self.assertEqual(result.written, 2, result.errors)

    def test_missing_credentials_raise_clear_error(self):
        from chatgpt_backup.api import AuthenticationError

        creds = Credentials(path=Path(self.tmp.name) / "empty.json")
        client = ChatGPTClient(creds, base_url=self.base_url, client=make_client(creds), request_delay=0.0)
        with self.assertRaises(AuthenticationError):
            backup_from_api(client, self._options())

    def test_conversation_fetch_failure_is_reported_but_not_fatal(self):
        self.server.payloads.append(
            {
                "conversation_id": "missing-from-detail-endpoint",
                "title": "拉取会失败的对话",
                "create_time": fixtures.BASE_TIME,
                "update_time": fixtures.BASE_TIME + 100000,
            }
        )
        # The detail endpoint only knows the payloads it can find by id; remove it
        # so the list advertises a conversation that cannot be fetched.
        broken = self.server.payloads.pop()
        self.server.payloads.append(dict(broken))
        self.server.payloads[-1]["conversation_id"] = "advertised-only"
        original_find = self.server.find
        self.server.find = lambda cid: None if cid == "advertised-only" else original_find(cid)

        result = backup_from_api(self._client(), self._options())
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.written, 2)
        self.assertTrue(result.errors)


if __name__ == "__main__":
    unittest.main()
