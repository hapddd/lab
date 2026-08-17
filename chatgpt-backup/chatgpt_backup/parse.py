"""Turn raw ChatGPT conversation JSON into :mod:`chatgpt_backup.model` objects.

The wire format is a tree: ``mapping`` holds nodes keyed by id, each with a
``parent``/``children`` link and an optional ``message``. ``current_node``
points at the leaf of the branch the user currently sees, so walking parents
from there reproduces the visible transcript (regenerated/edited replies are
side branches and are skipped unless ``all_branches`` is requested).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from . import model
from .model import Asset, Block, Conversation, Message
from .util import dig, strip_private_use, to_datetime

# Legacy citation markers that are made of ordinary characters and therefore
# survive the private-use cleanup.
_BRACKET_CITATION = re.compile(r"【[^【】]{0,120}?†[^【】]{0,120}?】")
_OAICITE = re.compile(r"\s*\[?oaicite:[^\]\s]*\]?")
_TRAILING_SPACES = re.compile(r"[ \t]+(\n|$)")
_MANY_BLANKS = re.compile(r"\n{3,}")

IMAGE_MIMES = ("image/",)
AUDIO_MIMES = ("audio/",)
VIDEO_MIMES = ("video/",)

HIDDEN_CONTENT_TYPES = {
    "model_editable_context",
    "sonic_webpage",
}


@dataclass
class ParseOptions:
    include_system: bool = False
    include_tools: bool = False
    include_thoughts: bool = True
    all_branches: bool = False


def clean_text(text: Any) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = strip_private_use(text)
    text = _BRACKET_CITATION.sub("", text)
    text = _OAICITE.sub("", text)
    text = _TRAILING_SPACES.sub(r"\1", text)
    return _MANY_BLANKS.sub("\n\n", text).strip()


def pointer_file_id(pointer: Optional[str]) -> Optional[str]:
    """``file-service://file-Abc123`` / ``sediment://file_0000`` -> bare id."""
    if not pointer or not isinstance(pointer, str):
        return None
    value = pointer.split("://", 1)[-1]
    value = value.split("?", 1)[0].strip("/")
    return value or None


def _kind_for(mime: Optional[str], default: str = model.FILE) -> str:
    if not mime:
        return default
    lowered = mime.lower()
    if lowered.startswith(IMAGE_MIMES):
        return model.IMAGE
    if lowered.startswith(AUDIO_MIMES):
        return model.AUDIO
    if lowered.startswith(VIDEO_MIMES):
        return model.VIDEO
    return default


# --------------------------------------------------------------------------- #
# tree walking
# --------------------------------------------------------------------------- #
def linearize(mapping: Dict[str, Any], current_node: Optional[str], all_branches: bool = False) -> List[Dict[str, Any]]:
    """Return message-bearing nodes in reading order."""
    if not isinstance(mapping, dict):
        return []

    if not all_branches and current_node and current_node in mapping:
        chain: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        node_id: Optional[str] = current_node
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            node = mapping[node_id] or {}
            if isinstance(node, dict) and node.get("message"):
                chain.append(node)
            node_id = node.get("parent") if isinstance(node, dict) else None
        chain.reverse()
        if chain:
            return chain

    # Fall back to (or explicitly ask for) every node, ordered by depth so that
    # replies still follow their prompts even when timestamps are missing.
    depths = _node_depths(mapping)
    nodes = [node for node in mapping.values() if isinstance(node, dict) and node.get("message")]
    nodes.sort(
        key=lambda node: (
            dig(node, "message", "create_time") or 0.0,
            depths.get(node.get("id") or "", 0),
        )
    )
    return nodes


def _node_depths(mapping: Dict[str, Any]) -> Dict[str, int]:
    depths: Dict[str, int] = {}

    def depth_of(node_id: str, guard: int = 0) -> int:
        if node_id in depths:
            return depths[node_id]
        if guard > 10000:
            return 0
        node = mapping.get(node_id) or {}
        parent = node.get("parent") if isinstance(node, dict) else None
        value = 0 if not parent or parent not in mapping else depth_of(parent, guard + 1) + 1
        depths[node_id] = value
        return value

    for node_id in list(mapping.keys()):
        depth_of(node_id)
    return depths


# --------------------------------------------------------------------------- #
# message -> blocks
# --------------------------------------------------------------------------- #
def _sources_from_metadata(metadata: Dict[str, Any]) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    seen: Set[str] = set()

    def add(title: Any, url: Any) -> None:
        if not url or not isinstance(url, str) or not url.startswith("http"):
            return
        if url in seen:
            return
        seen.add(url)
        found.append({"title": clean_text(title) or url, "url": url})

    for citation in metadata.get("citations") or []:
        if isinstance(citation, dict):
            meta = citation.get("metadata") or {}
            add(meta.get("title"), meta.get("url"))

    for ref in metadata.get("content_references") or []:
        if not isinstance(ref, dict):
            continue
        add(ref.get("title"), ref.get("url"))
        for nested_key in ("items", "sources", "refs", "attributions"):
            for nested in ref.get(nested_key) or []:
                if isinstance(nested, dict):
                    add(nested.get("title") or nested.get("attribution"), nested.get("url"))
    return found


def _asset_from_pointer_part(part: Dict[str, Any]) -> Optional[Asset]:
    pointer = part.get("asset_pointer") or part.get("image_url") or part.get("audio_asset_pointer")
    if isinstance(pointer, dict):
        pointer = pointer.get("asset_pointer")
    file_id = pointer_file_id(pointer)
    if not file_id:
        return None

    content_type = (part.get("content_type") or "").lower()
    if "image" in content_type:
        kind = model.IMAGE
    elif "audio" in content_type:
        kind = model.AUDIO
    elif "video" in content_type:
        kind = model.VIDEO
    else:
        kind = _kind_for(part.get("mime_type"), model.IMAGE)

    prompt = dig(part, "metadata", "dalle", "prompt") or dig(part, "metadata", "generation", "gen_title")
    return Asset(
        file_id=file_id,
        pointer=pointer if isinstance(pointer, str) else None,
        name=part.get("name"),
        mime_type=part.get("mime_type"),
        kind=kind,
        size_bytes=part.get("size_bytes") or part.get("size"),
        width=part.get("width"),
        height=part.get("height"),
        prompt=clean_text(prompt) or None,
    )


def _blocks_from_content(content: Dict[str, Any], metadata: Dict[str, Any], options: ParseOptions) -> List[Block]:
    content_type = (content.get("content_type") or "text").lower()
    blocks: List[Block] = []

    if content_type in ("text", "system_message"):
        for part in content.get("parts") or []:
            text = clean_text(part if isinstance(part, str) else dig(part, "text"))
            if text:
                blocks.append(Block(kind=model.TEXT, text=text))
        return blocks

    if content_type == "multimodal_text":
        for part in content.get("parts") or []:
            if isinstance(part, str):
                text = clean_text(part)
                if text:
                    blocks.append(Block(kind=model.TEXT, text=text))
                continue
            if not isinstance(part, dict):
                continue
            part_type = (part.get("content_type") or "").lower()
            if part_type == "audio_transcription":
                text = clean_text(part.get("text"))
                if text:
                    blocks.append(Block(kind=model.QUOTE, title="语音转写", text=text))
                continue
            asset = _asset_from_pointer_part(part)
            if asset is not None:
                blocks.append(Block(kind=asset.kind, asset=asset, text=asset.prompt or ""))
                continue
            text = clean_text(part.get("text"))
            if text:
                blocks.append(Block(kind=model.TEXT, text=text))
        return blocks

    if content_type == "code":
        language = (content.get("language") or "").strip()
        if language.lower() in ("", "unknown"):
            language = None
        text = content.get("text") or ""
        if text.strip():
            blocks.append(Block(kind=model.CODE, text=text.rstrip(), language=language))
        return blocks

    if content_type == "execution_output":
        text = (content.get("text") or "").rstrip()
        if text.strip():
            blocks.append(Block(kind=model.TOOL_OUTPUT, text=text, title="运行结果"))
        for item in dig(metadata, "aggregate_result", "messages", default=[]) or []:
            if not isinstance(item, dict):
                continue
            if (item.get("message_type") or "") == "image" or item.get("image_url"):
                asset = _asset_from_pointer_part(
                    {"content_type": "image_asset_pointer", "asset_pointer": item.get("image_url")}
                )
                if asset is not None:
                    blocks.append(Block(kind=model.IMAGE, asset=asset))
        return blocks

    if content_type == "thoughts":
        if not options.include_thoughts:
            return blocks
        chunks = []
        for thought in content.get("thoughts") or []:
            if not isinstance(thought, dict):
                continue
            summary = clean_text(thought.get("summary"))
            body = clean_text(thought.get("content"))
            if summary and body:
                chunks.append(f"**{summary}**\n\n{body}")
            elif summary or body:
                chunks.append(summary or body)
        if chunks:
            blocks.append(Block(kind=model.THOUGHT, title="推理过程", text="\n\n".join(chunks)))
        return blocks

    if content_type == "reasoning_recap":
        if options.include_thoughts:
            text = clean_text(content.get("content") or content.get("text"))
            if text:
                blocks.append(Block(kind=model.THOUGHT, title="推理小结", text=text))
        return blocks

    if content_type in ("tether_browsing_display", "tether_browsing_code"):
        text = clean_text(content.get("result") or content.get("summary") or content.get("text"))
        if text:
            blocks.append(Block(kind=model.QUOTE, title="联网浏览", text=text))
        return blocks

    if content_type == "tether_quote":
        text = clean_text(content.get("text"))
        if text:
            blocks.append(
                Block(
                    kind=model.QUOTE,
                    title=clean_text(content.get("title")) or "引用",
                    text=text,
                    url=content.get("url"),
                )
            )
        return blocks

    if content_type == "system_error":
        text = clean_text(content.get("text"))
        name = clean_text(content.get("name"))
        if text or name:
            blocks.append(Block(kind=model.ERROR, title=name or "系统错误", text=text))
        return blocks

    if content_type == "user_editable_context":
        profile = clean_text(content.get("user_profile"))
        instructions = clean_text(content.get("user_instructions"))
        merged = "\n\n".join(chunk for chunk in (profile, instructions) if chunk)
        if merged:
            blocks.append(Block(kind=model.CONTEXT, title="自定义指令", text=merged))
        return blocks

    if content_type in HIDDEN_CONTENT_TYPES:
        return blocks

    # Unknown content type: keep whatever text-ish payload we can find so the
    # backup never silently drops content.
    fallback = content.get("text") or content.get("result") or content.get("content")
    if isinstance(fallback, str) and fallback.strip():
        blocks.append(Block(kind=model.TEXT, text=clean_text(fallback)))
    else:
        for part in content.get("parts") or []:
            if isinstance(part, str) and part.strip():
                blocks.append(Block(kind=model.TEXT, text=clean_text(part)))
    return blocks


def _attachment_blocks(metadata: Dict[str, Any], known: Dict[str, Asset]) -> List[Block]:
    """Blocks for attachments that have no inline pointer.

    Uploaded images appear twice: as an ``image_asset_pointer`` part and in
    ``metadata.attachments``. Only the attachment record carries the original
    file name, so for those we enrich the existing asset instead of adding a
    duplicate block.
    """
    blocks: List[Block] = []
    for attachment in metadata.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        file_id = attachment.get("id") or pointer_file_id(attachment.get("asset_pointer"))
        if not file_id:
            continue
        mime = attachment.get("mime_type") or attachment.get("mimeType")
        existing = known.get(file_id)
        if existing is not None:
            existing.name = existing.name or attachment.get("name")
            existing.mime_type = existing.mime_type or mime
            existing.size_bytes = existing.size_bytes or attachment.get("size")
            existing.width = existing.width or attachment.get("width")
            existing.height = existing.height or attachment.get("height")
            continue
        asset = Asset(
            file_id=file_id,
            pointer=attachment.get("asset_pointer"),
            name=attachment.get("name"),
            mime_type=mime,
            kind=_kind_for(mime, model.FILE),
            size_bytes=attachment.get("size") or attachment.get("file_size_tokens"),
            width=attachment.get("width"),
            height=attachment.get("height"),
        )
        known[file_id] = asset
        blocks.append(Block(kind=asset.kind, asset=asset))
    return blocks


def parse_message(node: Dict[str, Any], options: ParseOptions) -> Optional[Message]:
    raw = node.get("message") or {}
    if not isinstance(raw, dict):
        return None

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    role = (dig(raw, "author", "role") or "assistant").lower()
    author_name = dig(raw, "author", "name")
    recipient = raw.get("recipient") or "all"

    if role == "system" and not options.include_system:
        return None
    if role == "tool" and not options.include_tools:
        return None
    if metadata.get("is_visually_hidden_from_conversation") and not options.include_system:
        return None
    # Assistant messages addressed at a tool are the model's tool call payload.
    if role == "assistant" and recipient not in ("all", None, "") and not options.include_tools:
        return None

    content = raw.get("content") or {}
    if not isinstance(content, dict):
        content = {"content_type": "text", "parts": [str(content)]}

    blocks = _blocks_from_content(content, metadata, options)
    known_assets = {block.asset.file_id: block.asset for block in blocks if block.asset is not None}
    blocks.extend(_attachment_blocks(metadata, known_assets))

    message = Message(
        id=raw.get("id") or node.get("id") or "",
        role=role,
        author_name=author_name,
        create_time=to_datetime(raw.get("create_time")),
        model=metadata.get("model_slug") or metadata.get("default_model_slug"),
        blocks=[block for block in blocks if not block.is_empty],
        sources=_sources_from_metadata(metadata),
    )
    if message.is_empty and not message.blocks:
        return None
    return message


def parse_conversation(
    payload: Dict[str, Any],
    options: Optional[ParseOptions] = None,
    source: str = "api",
) -> Conversation:
    options = options or ParseOptions()
    mapping = payload.get("mapping") or {}
    nodes = linearize(mapping, payload.get("current_node"), options.all_branches)

    messages: List[Message] = []
    for node in nodes:
        message = parse_message(node, options)
        if message is not None:
            messages.append(message)

    title = clean_text(payload.get("title")) or "未命名对话"
    model_slug = payload.get("default_model_slug")
    if not model_slug:
        for message in reversed(messages):
            if message.model:
                model_slug = message.model
                break

    return Conversation(
        id=payload.get("conversation_id") or payload.get("id") or "",
        title=title,
        create_time=to_datetime(payload.get("create_time")),
        update_time=to_datetime(payload.get("update_time")),
        model=model_slug,
        messages=messages,
        source=source,
        is_archived=bool(payload.get("is_archived")),
        raw=payload,
    )


def iter_conversations(
    payloads: Iterable[Dict[str, Any]],
    options: Optional[ParseOptions] = None,
    source: str = "export",
) -> Iterable[Conversation]:
    for payload in payloads:
        if isinstance(payload, dict):
            yield parse_conversation(payload, options, source)
