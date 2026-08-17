"""Backup directory layout, incremental state and the root index."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ASSETS_DIRNAME, CONVERSATIONS_DIRNAME, INDEX_FILE, STATE_FILE, log
from .model import Conversation, ConversationRef
from .render import render_index
from .util import atomic_write_text, date_prefix, fmt_iso, short_id, slugify

STATE_VERSION = 1


@dataclass
class Target:
    """Where a single conversation is written."""

    directory: Path
    markdown_path: Path
    assets_dir: Path
    assets_rel_prefix: str
    rel_path: str


class BackupStore:
    def __init__(self, root: Path, layout: str = "folder") -> None:
        self.root = Path(root).expanduser()
        self.layout = layout if layout in ("folder", "flat") else "folder"
        self.conversations_dir = self.root / CONVERSATIONS_DIRNAME
        self.state_path = self.root / STATE_FILE
        self.state: Dict[str, Any] = {"version": STATE_VERSION, "conversations": {}}
        self._loaded = False

    # -- state -------------------------------------------------------------- #
    def load(self) -> "BackupStore":
        if self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("conversations"), dict):
                    self.state = data
                    self.state.setdefault("version", STATE_VERSION)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("备份状态文件损坏，本次将全量重建: %s (%s)", self.state_path, exc)
        self._loaded = True
        return self

    def save_state(self) -> None:
        self.state["version"] = STATE_VERSION
        self.state["updated_at"] = fmt_iso(dt.datetime.now().astimezone())
        self.state["output_dir"] = str(self.root)
        atomic_write_text(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")

    def entry(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        return (self.state.get("conversations") or {}).get(conversation_id)

    def needs_update(self, ref: ConversationRef, force: bool = False) -> bool:
        """Skip conversations whose ``update_time`` has not moved since last run."""
        if force:
            return True
        entry = self.entry(ref.id)
        if not entry:
            return True
        stored_path = entry.get("path")
        if not stored_path or not (self.root / stored_path).exists():
            return True
        known = entry.get("update_time") or ""
        current = fmt_iso(ref.update_time)
        if not current:
            return True
        return current != known

    # -- layout ------------------------------------------------------------- #
    def _dir_name(self, conversation: Conversation) -> str:
        stamp = date_prefix(conversation.update_time or conversation.create_time)
        slug = slugify(conversation.title, max_len=42, fallback="对话")
        return f"{stamp}-{slug}-{short_id(conversation.id)}"

    def target_for(self, conversation: Conversation) -> Target:
        name = self._dir_name(conversation)
        if self.layout == "flat":
            markdown_path = self.conversations_dir / f"{name}.md"
            assets_dir = self.root / ASSETS_DIRNAME / name
            prefix = f"../{ASSETS_DIRNAME}/{name}"
            directory = self.conversations_dir
        else:
            directory = self.conversations_dir / name
            markdown_path = directory / f"{INDEX_FILE}"
            assets_dir = directory / ASSETS_DIRNAME
            prefix = ASSETS_DIRNAME
        return Target(
            directory=directory,
            markdown_path=markdown_path,
            assets_dir=assets_dir,
            assets_rel_prefix=prefix,
            rel_path=markdown_path.relative_to(self.root).as_posix(),
        )

    def relocate_if_renamed(self, conversation: Conversation, target: Target) -> None:
        """Move a previously written conversation when its title changed."""
        entry = self.entry(conversation.id)
        if not entry:
            return
        old_rel = entry.get("path")
        if not old_rel or old_rel == target.rel_path:
            return
        old_md = self.root / old_rel
        if not old_md.exists():
            return
        try:
            if self.layout == "folder":
                old_dir = old_md.parent
                if old_dir.is_dir() and old_dir != target.directory:
                    target.directory.parent.mkdir(parents=True, exist_ok=True)
                    if target.directory.exists():
                        shutil.rmtree(target.directory, ignore_errors=True)
                    shutil.move(str(old_dir), str(target.directory))
                    log.debug("对话目录已改名: %s -> %s", old_dir.name, target.directory.name)
            else:
                target.markdown_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_md), str(target.markdown_path))
                old_assets = self.root / ASSETS_DIRNAME / old_md.stem
                new_assets = self.root / ASSETS_DIRNAME / target.markdown_path.stem
                if old_assets.is_dir() and old_assets != new_assets:
                    if new_assets.exists():
                        shutil.rmtree(new_assets, ignore_errors=True)
                    shutil.move(str(old_assets), str(new_assets))
        except OSError as exc:
            log.debug("重命名旧备份失败（将直接写入新位置）: %s", exc)

    # -- writing ------------------------------------------------------------ #
    def write_markdown(self, conversation: Conversation, markdown: str, target: Target) -> Path:
        atomic_write_text(target.markdown_path, markdown)
        return target.markdown_path

    def record(self, conversation: Conversation, target: Target, asset_count: int, asset_failures: int = 0) -> None:
        self.state.setdefault("conversations", {})[conversation.id] = {
            "title": conversation.title,
            "path": target.rel_path,
            "create_time": fmt_iso(conversation.create_time),
            "update_time": fmt_iso(conversation.update_time),
            "message_count": len(conversation.messages),
            "asset_count": asset_count,
            "asset_failures": asset_failures,
            "source": conversation.source,
            "model": conversation.model or "",
            "backed_up_at": fmt_iso(dt.datetime.now().astimezone()),
        }

    def write_index(self) -> Path:
        entries: List[Dict[str, Any]] = []
        for conversation_id, entry in (self.state.get("conversations") or {}).items():
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    "id": conversation_id,
                    "title": entry.get("title"),
                    "rel_path": entry.get("path"),
                    "update_time": entry.get("update_time") or entry.get("create_time") or "",
                    "message_count": entry.get("message_count", 0),
                    "asset_count": entry.get("asset_count", 0),
                }
            )
        entries.sort(key=lambda item: str(item.get("update_time") or ""), reverse=True)
        path = self.root / INDEX_FILE
        atomic_write_text(path, render_index(entries, str(self.root)))
        return path

    def ensure_dirs(self) -> None:
        self.conversations_dir.mkdir(parents=True, exist_ok=True)

    @property
    def conversation_count(self) -> int:
        return len(self.state.get("conversations") or {})
