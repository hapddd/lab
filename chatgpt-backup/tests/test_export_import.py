"""Backup from an official data export (zip / unpacked directory)."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from chatgpt_backup.backup import BackupOptions, backup_from_export
from chatgpt_backup.sources.official_export import ExportArchive, find_latest_export

from . import fixtures


def build_export(directory: Path, as_zip: bool = True) -> Path:
    payloads = [
        fixtures.conversation_payload(),
        fixtures.second_conversation_payload(),
        fixtures.archived_conversation_payload(),
    ]
    members = {
        "conversations.json": json.dumps(payloads, ensure_ascii=False).encode("utf-8"),
        "user.json": b'{"id": "user-1", "email": "someone@example.com"}',
        "chat.html": b"<html></html>",
        # Uploads keep their original name; note the `file_` spelling variant.
        f"{fixtures.IMAGE_FILE_ID}-截图.png": fixtures.FILE_BLOBS[fixtures.IMAGE_FILE_ID][0],
        f"{fixtures.PDF_FILE_ID}-账号说明.pdf": fixtures.FILE_BLOBS[fixtures.PDF_FILE_ID][0],
        f"{fixtures.PLOT_FILE_ID}-plot.png": fixtures.FILE_BLOBS[fixtures.PLOT_FILE_ID][0],
        f"dalle-generations/{fixtures.DALLE_FILE_ID.replace('-', '_')}-示意图.webp": fixtures.FILE_BLOBS[
            fixtures.DALLE_FILE_ID
        ][0],
    }

    if as_zip:
        target = directory / "chatgpt-data-export.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return target

    target = directory / "export"
    for name, data in members.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return target


class ExportArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt-export-test-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_conversations_from_zip(self):
        with ExportArchive(build_export(self.root)) as archive:
            payloads = archive.load_conversations()
        self.assertEqual(len(payloads), 3)

    def test_finds_asset_by_pointer_id(self):
        with ExportArchive(build_export(self.root)) as archive:
            member = archive.find_member(fixtures.IMAGE_FILE_ID)
            self.assertIsNotNone(member)
            self.assertIn("截图.png", member)

    def test_matches_across_file_dash_and_underscore(self):
        with ExportArchive(build_export(self.root)) as archive:
            member = archive.find_member(fixtures.DALLE_FILE_ID)
            self.assertIsNotNone(member, "sediment 指针里的 file- 应能匹配磁盘上的 file_ 命名")
            self.assertIn("dalle-generations/", member)

    def test_rejects_unknown_format(self):
        bogus = self.root / "notes.txt"
        bogus.write_text("hi", encoding="utf-8")
        with self.assertRaises(ValueError):
            ExportArchive(bogus)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            ExportArchive(self.root / "nope.zip")

    def test_find_latest_export_prefers_newest(self):
        first = self.root / "chatgpt-export-old.zip"
        second = self.root / "chatgpt-export-new.zip"
        for path in (first, second):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("conversations.json", "[]")
        import os
        import time

        os.utime(first, (time.time() - 5000, time.time() - 5000))
        self.assertEqual(find_latest_export([self.root]), second)


class ExportBackupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt-export-backup-")
        self.root = Path(self.tmp.name)
        self.out = self.root / "chat_bak"

    def tearDown(self):
        self.tmp.cleanup()

    def _options(self, **overrides) -> BackupOptions:
        params = dict(output_dir=self.out, limit=10)
        params.update(overrides)
        return BackupOptions(**params)

    def test_zip_export_produces_markdown_and_local_images(self):
        result = backup_from_export(build_export(self.root), self._options())
        self.assertEqual(result.failed, 0, result.errors)
        self.assertEqual(result.written, 2)
        self.assertGreaterEqual(result.assets_saved, 3)

        target = next((self.out / "conversations").rglob("index.md"))
        self.assertTrue(target.is_file())
        markdown = next(
            path for path in (self.out / "conversations").rglob("index.md") if "退出账号" in str(path)
        )
        text = markdown.read_text(encoding="utf-8")
        self.assertIn("source: \"export\"", text)
        for link in [
            line.split("](", 1)[1].split(")", 1)[0]
            for line in text.splitlines()
            if line.startswith("![") or line.startswith("[附件")
        ]:
            self.assertTrue((markdown.parent / link.replace("%20", " ")).resolve().is_file())

    def test_unpacked_directory_export(self):
        result = backup_from_export(build_export(self.root, as_zip=False), self._options())
        self.assertEqual(result.written, 2)
        self.assertGreater(result.assets_saved, 0)

    def test_conversations_json_alone_is_accepted(self):
        payload_path = self.root / "conversations.json"
        payload_path.write_text(
            json.dumps([fixtures.second_conversation_payload()], ensure_ascii=False), encoding="utf-8"
        )
        result = backup_from_export(payload_path, self._options())
        self.assertEqual(result.written, 1)

    def test_incremental_skip_on_second_import(self):
        export = build_export(self.root)
        backup_from_export(export, self._options())
        second = backup_from_export(export, self._options())
        self.assertEqual(second.written, 0)
        self.assertEqual(second.skipped, 2)

    def test_limit_keeps_most_recent(self):
        result = backup_from_export(build_export(self.root), self._options(limit=1))
        self.assertEqual(result.written, 1)
        names = [path.name for path in (self.out / "conversations").iterdir()]
        self.assertTrue(any("退出账号记录保存" in name for name in names))

    def test_archived_conversations_opt_in(self):
        result = backup_from_export(build_export(self.root), self._options(include_archived=True))
        self.assertEqual(result.written, 3)


if __name__ == "__main__":
    unittest.main()
