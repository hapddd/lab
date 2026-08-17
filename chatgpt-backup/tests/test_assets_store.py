"""Asset saving, extension sniffing, file naming and incremental state."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from chatgpt_backup import model
from chatgpt_backup.assets import AssetProvider, AssetSaver, Fetched, sniff_extension
from chatgpt_backup.model import Asset, Conversation, ConversationRef, Message
from chatgpt_backup.store import BackupStore
from chatgpt_backup.util import slugify

from . import fixtures


class StubProvider(AssetProvider):
    def __init__(self, blobs: dict, fail: tuple = ()):
        self.blobs = blobs
        self.fail = set(fail)
        self.calls = []

    def fetch(self, asset: Asset) -> Optional[Fetched]:
        self.calls.append(asset.file_id)
        if asset.file_id in self.fail or asset.file_id not in self.blobs:
            return None
        data, mime, name = self.blobs[asset.file_id]
        return Fetched(data=data, mime=mime, name=name)


class SniffExtensionTestCase(unittest.TestCase):
    def test_png(self):
        self.assertEqual(sniff_extension(fixtures.png_bytes()), ".png")

    def test_jpeg(self):
        self.assertEqual(sniff_extension(fixtures.jpeg_bytes()), ".jpg")

    def test_webp(self):
        data = b"RIFF\x00\x00\x00\x00WEBPVP8 "
        self.assertEqual(sniff_extension(data), ".webp")

    def test_wav(self):
        self.assertEqual(sniff_extension(b"RIFF\x00\x00\x00\x00WAVEfmt "), ".wav")

    def test_pdf(self):
        self.assertEqual(sniff_extension(fixtures.pdf_bytes()), ".pdf")

    def test_svg(self):
        self.assertEqual(sniff_extension(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'), ".svg")

    def test_mp4(self):
        self.assertEqual(sniff_extension(b"\x00\x00\x00 ftypisom\x00\x00\x02\x00"), ".mp4")

    def test_docx_keeps_original_suffix(self):
        self.assertEqual(sniff_extension(b"PK\x03\x04rest", name="报告.docx"), ".docx")

    def test_falls_back_to_mime(self):
        self.assertEqual(sniff_extension(b"unknown-bytes", mime="image/png"), ".png")

    def test_falls_back_to_name(self):
        self.assertEqual(sniff_extension(b"unknown-bytes", name="notes.csv"), ".csv")

    def test_last_resort(self):
        self.assertEqual(sniff_extension(b"unknown-bytes"), ".bin")

    def test_content_type_with_charset(self):
        self.assertEqual(sniff_extension(b"unknown", mime="text/plain; charset=utf-8"), ".txt")


class AssetSaverTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt-assets-test-")
        self.dir = Path(self.tmp.name) / "assets"
        self.provider = StubProvider(fixtures.FILE_BLOBS)

    def tearDown(self):
        self.tmp.cleanup()

    def _saver(self, **kwargs) -> AssetSaver:
        return AssetSaver(assets_dir=self.dir, provider=self.provider, **kwargs)

    def test_saves_with_readable_name_and_extension(self):
        saver = self._saver()
        asset = Asset(file_id=fixtures.IMAGE_FILE_ID, name="截图.png", kind=model.IMAGE)
        self.assertTrue(saver.save(asset))
        self.assertTrue(asset.local_path.is_file())
        self.assertTrue(asset.local_path.name.startswith("截图-"))
        self.assertEqual(asset.local_path.suffix, ".png")
        self.assertEqual(asset.rel_path, f"assets/{asset.local_path.name}")

    def test_extension_comes_from_bytes_not_a_wrong_mime(self):
        saver = self._saver()
        # The fixture claims image/webp but the bytes are PNG; bytes win.
        asset = Asset(file_id=fixtures.DALLE_FILE_ID, kind=model.IMAGE)
        saver.save(asset)
        self.assertEqual(asset.local_path.suffix, ".png")

    def test_same_file_id_reused_without_second_fetch(self):
        saver = self._saver()
        first = Asset(file_id=fixtures.IMAGE_FILE_ID, kind=model.IMAGE)
        second = Asset(file_id=fixtures.IMAGE_FILE_ID, kind=model.IMAGE)
        saver.save(first)
        saver.save(second)
        self.assertEqual(self.provider.calls, [fixtures.IMAGE_FILE_ID])
        self.assertEqual(first.rel_path, second.rel_path)
        self.assertEqual(saver.saved, 1)
        self.assertEqual(saver.reused, 1)

    def test_identical_bytes_under_different_ids_are_deduped(self):
        blobs = dict(fixtures.FILE_BLOBS)
        shared = fixtures.png_bytes(3, 3, (1, 2, 3))
        blobs["file-AAA"] = (shared, "image/png", "a.png")
        blobs["file-BBB"] = (shared, "image/png", "b.png")
        provider = StubProvider(blobs)
        saver = AssetSaver(assets_dir=self.dir, provider=provider)
        first = Asset(file_id="file-AAA", kind=model.IMAGE)
        second = Asset(file_id="file-BBB", kind=model.IMAGE)
        saver.save(first)
        saver.save(second)
        self.assertEqual(first.rel_path, second.rel_path)
        self.assertEqual(len(list(self.dir.iterdir())), 1)

    def test_failure_is_flagged_and_counted(self):
        saver = AssetSaver(assets_dir=self.dir, provider=StubProvider({}, fail=("file-X",)))
        asset = Asset(file_id="file-X", kind=model.IMAGE)
        self.assertFalse(saver.save(asset))
        self.assertTrue(asset.failed)
        self.assertEqual(saver.failed, 1)
        self.assertIsNone(asset.rel_path)

    def test_disabled_saver_does_nothing(self):
        saver = self._saver(enabled=False)
        asset = Asset(file_id=fixtures.IMAGE_FILE_ID, kind=model.IMAGE)
        self.assertFalse(saver.save(asset))
        self.assertFalse(self.dir.exists())

    def test_custom_rel_prefix(self):
        saver = self._saver(rel_prefix="../assets/conv-1")
        asset = Asset(file_id=fixtures.IMAGE_FILE_ID, kind=model.IMAGE)
        saver.save(asset)
        self.assertTrue(asset.rel_path.startswith("../assets/conv-1/"))

    def test_provider_exception_does_not_propagate(self):
        class Boom(AssetProvider):
            def fetch(self, asset):
                raise RuntimeError("network exploded")

        saver = AssetSaver(assets_dir=self.dir, provider=Boom())
        asset = Asset(file_id="file-Y", kind=model.IMAGE)
        saver.save_all([asset])
        self.assertTrue(asset.failed)
        self.assertEqual(saver.failed, 1)


class SlugifyTestCase(unittest.TestCase):
    def test_keeps_chinese(self):
        self.assertEqual(slugify("退出账号记录保存"), "退出账号记录保存")

    def test_removes_path_separators(self):
        self.assertNotIn("/", slugify("a/b\\c:d*e?f"))

    def test_collapses_whitespace(self):
        self.assertEqual(slugify("hello   world"), "hello-world")

    def test_falls_back_when_empty(self):
        self.assertEqual(slugify("   ", fallback="对话"), "对话")
        self.assertEqual(slugify(None, fallback="对话"), "对话")

    def test_byte_length_capped_for_cjk(self):
        name = slugify("中" * 200)
        self.assertLessEqual(len(name.encode("utf-8")), 120)

    def test_strips_leading_and_trailing_dots(self):
        self.assertFalse(slugify("...secret...").startswith("."))


class BackupStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt-store-test-")
        self.root = Path(self.tmp.name) / "chat_bak"
        self.store = BackupStore(self.root).load()
        self.store.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def _conversation(self, title="退出账号记录保存", cid="68a1000011112222") -> Conversation:
        return Conversation(
            id=cid,
            title=title,
            create_time=dt.datetime(2026, 8, 15, 10, 0).astimezone(),
            update_time=dt.datetime(2026, 8, 17, 9, 30).astimezone(),
            messages=[Message(id="m1", role="user")],
        )

    def test_folder_target_layout(self):
        target = self.store.target_for(self._conversation())
        self.assertEqual(target.markdown_path.name, "index.md")
        self.assertTrue(target.directory.name.startswith("2026-08-17-退出账号记录保存-"))
        self.assertEqual(target.assets_rel_prefix, "assets")
        self.assertEqual(target.rel_path, f"conversations/{target.directory.name}/index.md")

    def test_flat_target_layout(self):
        store = BackupStore(self.root, layout="flat").load()
        target = store.target_for(self._conversation())
        self.assertEqual(target.markdown_path.suffix, ".md")
        self.assertTrue(target.assets_rel_prefix.startswith("../assets/"))

    def test_needs_update_for_unknown_conversation(self):
        ref = ConversationRef(id="new-id", update_time=dt.datetime.now().astimezone())
        self.assertTrue(self.store.needs_update(ref))

    def test_needs_update_false_when_unchanged(self):
        conversation = self._conversation()
        target = self.store.target_for(conversation)
        self.store.write_markdown(conversation, "# hi\n", target)
        self.store.record(conversation, target, asset_count=0)
        ref = ConversationRef(id=conversation.id, update_time=conversation.update_time)
        self.assertFalse(self.store.needs_update(ref))
        self.assertTrue(self.store.needs_update(ref, force=True))

    def test_needs_update_when_file_deleted(self):
        conversation = self._conversation()
        target = self.store.target_for(conversation)
        self.store.write_markdown(conversation, "# hi\n", target)
        self.store.record(conversation, target, asset_count=0)
        target.markdown_path.unlink()
        ref = ConversationRef(id=conversation.id, update_time=conversation.update_time)
        self.assertTrue(self.store.needs_update(ref))

    def test_state_round_trip(self):
        conversation = self._conversation()
        target = self.store.target_for(conversation)
        self.store.write_markdown(conversation, "# hi\n", target)
        self.store.record(conversation, target, asset_count=2)
        self.store.save_state()

        reloaded = BackupStore(self.root).load()
        entry = reloaded.entry(conversation.id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["asset_count"], 2)

    def test_corrupt_state_is_tolerated(self):
        self.store.state_path.write_text("{not json", encoding="utf-8")
        reloaded = BackupStore(self.root).load()
        self.assertEqual(reloaded.state.get("conversations"), {})

    def test_index_sorted_newest_first(self):
        older = self._conversation(title="旧对话", cid="old-id")
        older.update_time = dt.datetime(2026, 1, 1, 0, 0).astimezone()
        newer = self._conversation(title="新对话", cid="new-id")
        for conversation in (older, newer):
            target = self.store.target_for(conversation)
            self.store.write_markdown(conversation, "# x\n", target)
            self.store.record(conversation, target, asset_count=0)
        index = self.store.write_index().read_text(encoding="utf-8")
        self.assertLess(index.index("新对话"), index.index("旧对话"))

    def test_relocate_moves_folder_on_title_change(self):
        conversation = self._conversation()
        target = self.store.target_for(conversation)
        self.store.write_markdown(conversation, "# old\n", target)
        (target.assets_dir / "keep.png").parent.mkdir(parents=True, exist_ok=True)
        (target.assets_dir / "keep.png").write_bytes(fixtures.png_bytes())
        self.store.record(conversation, target, asset_count=1)

        conversation.title = "账号与聊天记录的关系"
        new_target = self.store.target_for(conversation)
        self.store.relocate_if_renamed(conversation, new_target)
        self.assertTrue((new_target.assets_dir / "keep.png").is_file())
        self.assertFalse(target.directory.exists())


if __name__ == "__main__":
    unittest.main()
