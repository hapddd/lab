"""Normalised conversation model.

Every source (backend API, official data export) is converted into these
objects, so the markdown renderer and the asset downloader never need to know
where the data came from.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Block kinds understood by the renderer.
TEXT = "text"
CODE = "code"
IMAGE = "image"
FILE = "file"
AUDIO = "audio"
VIDEO = "video"
THOUGHT = "thought"
QUOTE = "quote"
TOOL_OUTPUT = "tool_output"
ERROR = "error"
CONTEXT = "context"

ASSET_KINDS = {IMAGE, FILE, AUDIO, VIDEO}


@dataclass
class Asset:
    """An image / file / audio attachment referenced by a message."""

    file_id: str
    pointer: Optional[str] = None
    name: Optional[str] = None
    mime_type: Optional[str] = None
    kind: str = FILE
    size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    prompt: Optional[str] = None  # DALL·E / image-gen prompt when present
    source_path: Optional[Path] = None  # already on disk (data export)
    local_path: Optional[Path] = None  # filled in after download
    rel_path: Optional[str] = None  # path used inside the markdown file
    failed: bool = False

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.local_path:
            return self.local_path.name
        return self.file_id or "附件"


@dataclass
class Block:
    """One renderable chunk inside a message."""

    kind: str = TEXT
    text: str = ""
    language: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    asset: Optional[Asset] = None

    @property
    def is_empty(self) -> bool:
        if self.asset is not None:
            return False
        return not (self.text or "").strip()


@dataclass
class Message:
    id: str
    role: str = "assistant"
    author_name: Optional[str] = None
    create_time: Optional[dt.datetime] = None
    model: Optional[str] = None
    blocks: List[Block] = field(default_factory=list)
    sources: List[Dict[str, str]] = field(default_factory=list)
    hidden: bool = False

    @property
    def assets(self) -> List[Asset]:
        return [block.asset for block in self.blocks if block.asset is not None]

    @property
    def is_empty(self) -> bool:
        return all(block.is_empty for block in self.blocks)


@dataclass
class Conversation:
    id: str
    title: str = "未命名对话"
    create_time: Optional[dt.datetime] = None
    update_time: Optional[dt.datetime] = None
    model: Optional[str] = None
    messages: List[Message] = field(default_factory=list)
    source: str = "api"
    is_archived: bool = False
    raw: Optional[Dict[str, Any]] = None

    @property
    def url(self) -> str:
        return f"https://chatgpt.com/c/{self.id}" if self.id else ""

    @property
    def assets(self) -> List[Asset]:
        found: List[Asset] = []
        seen = set()
        for message in self.messages:
            for asset in message.assets:
                key = asset.pointer or asset.file_id
                if key in seen:
                    continue
                seen.add(key)
                found.append(asset)
        return found

    @property
    def sort_time(self) -> dt.datetime:
        value = self.update_time or self.create_time
        return value or dt.datetime.fromtimestamp(0).astimezone()


@dataclass
class ConversationRef:
    """Lightweight listing entry, used to decide what needs downloading."""

    id: str
    title: str = "未命名对话"
    create_time: Optional[dt.datetime] = None
    update_time: Optional[dt.datetime] = None
    is_archived: bool = False
    raw: Optional[Dict[str, Any]] = None
