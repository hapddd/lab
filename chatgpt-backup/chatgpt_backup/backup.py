"""Backup orchestration: fetch → parse → save images → render → write."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .api import ApiAssetProvider, ChatGPTClient
from .assets import AssetProvider, AssetSaver, LocalFileProvider
from .config import log
from .http import HttpError
from .model import Conversation, ConversationRef
from .parse import ParseOptions, parse_conversation
from .render import RenderOptions, render_conversation
from .sources.official_export import ExportArchive
from .store import BackupStore
from .util import atomic_write_text, fmt_iso


@dataclass
class BackupOptions:
    output_dir: Path
    limit: int = 20
    force: bool = False
    include_system: bool = False
    include_tools: bool = False
    include_thoughts: bool = True
    all_branches: bool = False
    download_assets: bool = True
    include_archived: bool = False
    layout: str = "folder"
    dry_run: bool = False
    since: Optional[dt.datetime] = None
    save_raw: bool = False

    def parse_options(self) -> ParseOptions:
        return ParseOptions(
            include_system=self.include_system,
            include_tools=self.include_tools,
            include_thoughts=self.include_thoughts,
            all_branches=self.all_branches,
        )

    def render_options(self) -> RenderOptions:
        return RenderOptions()


@dataclass
class BackupResult:
    seen: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    assets_saved: int = 0
    assets_reused: int = 0
    assets_failed: int = 0
    output_dir: Optional[Path] = None
    index_path: Optional[Path] = None
    errors: List[str] = field(default_factory=list)
    written_paths: List[Path] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"检查 {self.seen} 个对话",
            f"写入 {self.written} 个",
            f"跳过 {self.skipped} 个（无更新）",
        ]
        if self.failed:
            bits.append(f"失败 {self.failed} 个")
        bits.append(f"图片/附件: 新增 {self.assets_saved}，复用 {self.assets_reused}")
        if self.assets_failed:
            bits.append(f"附件失败 {self.assets_failed}")
        return "，".join(bits) + "。"


def _process_conversation(
    store: BackupStore,
    conversation: Conversation,
    provider: Optional[AssetProvider],
    options: BackupOptions,
    result: BackupResult,
) -> Optional[Path]:
    target = store.target_for(conversation)

    if options.dry_run:
        log.info("[试运行] 将写入: %s (%d 条消息, %d 个附件)",
                 target.rel_path, len(conversation.messages), len(conversation.assets))
        result.written += 1
        return None

    store.relocate_if_renamed(conversation, target)
    target.markdown_path.parent.mkdir(parents=True, exist_ok=True)

    saver = AssetSaver(
        assets_dir=target.assets_dir,
        provider=provider,
        rel_prefix=target.assets_rel_prefix,
        enabled=options.download_assets and provider is not None,
    )
    assets = conversation.assets
    if assets and saver.enabled:
        log.debug("下载 %d 个附件: %s", len(assets), conversation.title)
        saver.save_all(assets)
    result.assets_saved += saver.saved
    result.assets_reused += saver.reused
    result.assets_failed += saver.failed

    markdown = render_conversation(conversation, options.render_options())
    path = store.write_markdown(conversation, markdown, target)
    if options.save_raw and conversation.raw is not None:
        import json

        atomic_write_text(
            target.markdown_path.with_name(target.markdown_path.stem + ".raw.json"),
            json.dumps(conversation.raw, ensure_ascii=False, indent=1),
        )
    store.record(conversation, target, asset_count=len(assets), asset_failures=saver.failed)
    result.written += 1
    result.written_paths.append(path)
    log.info(
        "已备份 %s (%d 条消息%s)",
        target.rel_path,
        len(conversation.messages),
        f", {len(assets)} 个附件" if assets else "",
    )
    return path


def _finish(store: BackupStore, options: BackupOptions, result: BackupResult) -> BackupResult:
    result.output_dir = store.root
    if not options.dry_run:
        store.save_state()
        result.index_path = store.write_index()
    return result


def backup_from_api(client: ChatGPTClient, options: BackupOptions) -> BackupResult:
    """Back up the most recent conversations straight from the backend API."""
    result = BackupResult()
    store = BackupStore(options.output_dir, options.layout).load()
    store.ensure_dirs()
    provider: Optional[AssetProvider] = ApiAssetProvider(client) if options.download_assets else None
    parse_options = options.parse_options()

    try:
        refs: Sequence[ConversationRef] = client.list_conversation_refs(
            limit=options.limit, include_archived=options.include_archived
        )
    except HttpError as exc:
        result.errors.append(f"获取对话列表失败: {exc}")
        log.error("获取对话列表失败: %s", exc)
        return _finish(store, options, result)

    log.info("服务器返回 %d 个最近对话。", len(refs))

    for index, ref in enumerate(refs, 1):
        result.seen += 1
        if options.since and ref.update_time and ref.update_time < options.since:
            log.debug("早于 --since，跳过: %s", ref.title)
            result.skipped += 1
            continue
        if not store.needs_update(ref, options.force):
            log.info("[%d/%d] 无更新，跳过: %s", index, len(refs), ref.title)
            result.skipped += 1
            continue

        log.info("[%d/%d] 拉取: %s", index, len(refs), ref.title)
        try:
            payload = client.get_conversation(ref.id)
        except (HttpError, OSError) as exc:
            result.failed += 1
            message = f"拉取对话失败 {ref.title} ({ref.id}): {exc}"
            result.errors.append(message)
            log.warning(message)
            continue

        conversation = parse_conversation(payload, parse_options, source="api")
        if not conversation.id:
            conversation.id = ref.id
        if not conversation.update_time:
            conversation.update_time = ref.update_time
        if not conversation.create_time:
            conversation.create_time = ref.create_time

        try:
            _process_conversation(store, conversation, provider, options, result)
        except KeyboardInterrupt:
            log.warning("已中断，正在保存已完成的进度…")
            _finish(store, options, result)
            raise
        except Exception as exc:
            result.failed += 1
            message = f"写入对话失败 {conversation.title}: {exc}"
            result.errors.append(message)
            log.warning(message)
            continue

        if result.written % 5 == 0 and not options.dry_run:
            store.save_state()

    return _finish(store, options, result)


def backup_from_export(export_path: Path, options: BackupOptions) -> BackupResult:
    """Back up from an official data export (no login required)."""
    result = BackupResult()
    store = BackupStore(options.output_dir, options.layout).load()
    store.ensure_dirs()
    parse_options = options.parse_options()

    with ExportArchive(Path(export_path)) as archive:
        payloads = archive.load_conversations()
        provider: Optional[AssetProvider] = archive.asset_provider() if options.download_assets else None
        conversations = [parse_conversation(payload, parse_options, source="export") for payload in payloads]
        conversations.sort(key=lambda item: item.sort_time, reverse=True)

        if options.since:
            conversations = [item for item in conversations if item.sort_time >= options.since]
        if options.limit and options.limit > 0:
            conversations = conversations[: options.limit]
        if not options.include_archived:
            conversations = [item for item in conversations if not item.is_archived]

        log.info("导出包内共 %d 个对话，本次处理 %d 个。", len(payloads), len(conversations))

        for index, conversation in enumerate(conversations, 1):
            result.seen += 1
            ref = ConversationRef(
                id=conversation.id,
                title=conversation.title,
                create_time=conversation.create_time,
                update_time=conversation.update_time,
            )
            if not store.needs_update(ref, options.force):
                log.info("[%d/%d] 无更新，跳过: %s", index, len(conversations), conversation.title)
                result.skipped += 1
                continue
            log.info("[%d/%d] 处理: %s", index, len(conversations), conversation.title)
            try:
                _process_conversation(store, conversation, provider, options, result)
            except KeyboardInterrupt:
                _finish(store, options, result)
                raise
            except Exception as exc:
                result.failed += 1
                message = f"写入对话失败 {conversation.title}: {exc}"
                result.errors.append(message)
                log.warning(message)

    return _finish(store, options, result)


def backup_from_json_files(paths: Sequence[Path], options: BackupOptions) -> BackupResult:
    """Back up from raw conversation JSON files (e.g. saved by `fetch --raw`)."""
    import json

    result = BackupResult()
    store = BackupStore(options.output_dir, options.layout).load()
    store.ensure_dirs()
    parse_options = options.parse_options()
    provider = LocalFileProvider()

    for path in paths:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result.failed += 1
            result.errors.append(f"读取 {path} 失败: {exc}")
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            result.seen += 1
            conversation = parse_conversation(item, parse_options, source="json")
            try:
                _process_conversation(store, conversation, provider, options, result)
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"写入 {conversation.title} 失败: {exc}")

    return _finish(store, options, result)


def state_report(output_dir: Path) -> List[dict]:
    store = BackupStore(output_dir).load()
    entries = []
    for conversation_id, entry in (store.state.get("conversations") or {}).items():
        if isinstance(entry, dict):
            item = dict(entry)
            item["id"] = conversation_id
            entries.append(item)
    entries.sort(key=lambda item: str(item.get("update_time") or ""), reverse=True)
    return entries


def now_iso() -> str:
    return fmt_iso(dt.datetime.now().astimezone())
