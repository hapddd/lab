"""Render a :class:`~chatgpt_backup.model.Conversation` as Markdown."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import model
from .model import Asset, Block, Conversation, Message
from .util import fmt_datetime, fmt_iso, human_size

ROLE_LABELS: Dict[str, str] = {
    "user": "我",
    "assistant": "ChatGPT",
    "system": "系统",
    "tool": "工具",
}

_BACKTICK_RUN = re.compile(r"`+")


@dataclass
class RenderOptions:
    frontmatter: bool = True
    show_timestamps: bool = True
    show_sources: bool = True
    time_format: str = "%Y-%m-%d %H:%M:%S"
    heading_level: int = 2


def _yaml_scalar(value: object) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def _fence_for(text: str) -> str:
    longest = max((len(match.group(0)) for match in _BACKTICK_RUN.finditer(text)), default=0)
    return "`" * max(3, longest + 1)


def _blockquote(text: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _asset_link(asset: Asset) -> str:
    target = asset.rel_path
    if not target:
        return ""
    # Spaces and parentheses break plain markdown link targets.
    return target.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _render_asset_block(block: Block, options: RenderOptions) -> str:
    asset = block.asset
    if asset is None:
        return ""

    caption_bits: List[str] = []
    if asset.width and asset.height:
        caption_bits.append(f"{asset.width}×{asset.height}")
    if asset.size_bytes:
        caption_bits.append(human_size(asset.size_bytes))

    if not asset.rel_path:
        note = f"未保存的附件: `{asset.display_name}` (id: `{asset.file_id}`)"
        if asset.failed:
            note = f"附件下载失败: `{asset.display_name}` (id: `{asset.file_id}`)"
        return _blockquote(note)

    link = _asset_link(asset)
    parts: List[str] = []
    if asset.kind == model.IMAGE:
        alt = asset.name or "图片"
        parts.append(f"![{alt}]({link})")
        caption = " · ".join(caption_bits)
        prompt = (asset.prompt or block.text or "").strip()
        if prompt:
            parts.append(f"*生成提示: {prompt}*")
        if caption:
            parts.append(f"<sub>{caption}</sub>")
    elif asset.kind in (model.AUDIO, model.VIDEO):
        label = "音频" if asset.kind == model.AUDIO else "视频"
        parts.append(f"[{label}: {asset.display_name}]({link})")
    else:
        caption = f" ({' · '.join(caption_bits)})" if caption_bits else ""
        parts.append(f"[附件: {asset.display_name}{caption}]({link})")
    return "\n\n".join(parts)


def render_block(block: Block, options: RenderOptions) -> str:
    if block.asset is not None:
        return _render_asset_block(block, options)

    if block.kind == model.CODE:
        fence = _fence_for(block.text)
        language = block.language or ""
        return f"{fence}{language}\n{block.text}\n{fence}"

    if block.kind == model.TOOL_OUTPUT:
        fence = _fence_for(block.text)
        title = f"**{block.title}**\n\n" if block.title else ""
        return f"{title}{fence}text\n{block.text}\n{fence}"

    if block.kind in (model.THOUGHT, model.CONTEXT):
        title = block.title or ("推理过程" if block.kind == model.THOUGHT else "上下文")
        return f"<details>\n<summary>{title}</summary>\n\n{block.text}\n\n</details>"

    if block.kind == model.QUOTE:
        header = block.title or "引用"
        if block.url:
            header = f"[{header}]({block.url})"
        return _blockquote(f"**{header}**\n\n{block.text}")

    if block.kind == model.ERROR:
        return _blockquote(f"**{block.title or '错误'}**\n\n{block.text}")

    return block.text


def _message_heading(message: Message, options: RenderOptions) -> str:
    label = ROLE_LABELS.get(message.role, message.role or "未知")
    if message.role == "tool" and message.author_name:
        label = f"工具 · {message.author_name}"
    elif message.role == "assistant" and message.model:
        label = f"{label} · {message.model}"
    hashes = "#" * max(2, options.heading_level)
    return f"{hashes} {label}"


def render_message(message: Message, options: RenderOptions) -> str:
    chunks: List[str] = [_message_heading(message, options)]
    if options.show_timestamps and message.create_time:
        chunks.append(f"<sub>{fmt_datetime(message.create_time, options.time_format)}</sub>")

    for block in message.blocks:
        rendered = render_block(block, options).strip()
        if rendered:
            chunks.append(rendered)

    if options.show_sources and message.sources:
        lines = ["**参考来源**", ""]
        lines += [f"{index}. [{item['title']}]({item['url']})" for index, item in enumerate(message.sources, 1)]
        chunks.append("\n".join(lines))

    return "\n\n".join(chunks)


def render_frontmatter(conversation: Conversation, extra: Optional[Dict[str, object]] = None) -> str:
    fields: Dict[str, object] = {
        "title": conversation.title,
        "conversation_id": conversation.id,
        "source": conversation.source,
        "model": conversation.model or "",
        "create_time": fmt_iso(conversation.create_time),
        "update_time": fmt_iso(conversation.update_time),
        "message_count": len(conversation.messages),
        "asset_count": len(conversation.assets),
        "url": conversation.url,
        "archived": conversation.is_archived,
        "backup_time": fmt_iso(dt.datetime.now().astimezone()),
    }
    if extra:
        fields.update(extra)
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def render_conversation(
    conversation: Conversation,
    options: Optional[RenderOptions] = None,
    extra_frontmatter: Optional[Dict[str, object]] = None,
) -> str:
    options = options or RenderOptions()
    parts: List[str] = []
    if options.frontmatter:
        parts.append(render_frontmatter(conversation, extra_frontmatter))

    parts.append(f"# {conversation.title}")

    meta_bits = [
        f"创建时间: {fmt_datetime(conversation.create_time, options.time_format)}",
        f"更新时间: {fmt_datetime(conversation.update_time, options.time_format)}",
        f"消息数: {len(conversation.messages)}",
    ]
    if conversation.model:
        meta_bits.append(f"模型: {conversation.model}")
    if conversation.url:
        meta_bits.append(f"[在 ChatGPT 中打开]({conversation.url})")
    parts.append(_blockquote(" · ".join(meta_bits)))

    if not conversation.messages:
        parts.append("*（这个对话没有可导出的消息）*")

    for message in conversation.messages:
        rendered = render_message(message, options).strip()
        if rendered:
            parts.append("---")
            parts.append(rendered)

    return "\n\n".join(parts).rstrip() + "\n"


def render_index(entries: Sequence[Dict[str, object]], output_dir: str) -> str:
    """Root ``index.md`` listing every backed-up conversation, newest first."""
    now = fmt_datetime(dt.datetime.now().astimezone())
    lines = [
        "# ChatGPT 聊天记录备份",
        "",
        f"> 备份目录: `{output_dir}`  ",
        f"> 最近更新: {now}  ",
        f"> 对话数量: {len(entries)}",
        "",
        "| 更新时间 | 标题 | 消息 | 图片/附件 | 文件 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        title = str(entry.get("title") or "未命名对话").replace("|", "\\|")
        link = str(entry.get("rel_path") or "").replace(" ", "%20")
        updated = str(entry.get("update_time") or "")[:19].replace("T", " ")
        lines.append(
            f"| {updated} | {title} | {entry.get('message_count', 0)} | "
            f"{entry.get('asset_count', 0)} | [打开]({link}) |"
        )
    return "\n".join(lines) + "\n"
