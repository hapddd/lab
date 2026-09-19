#!/usr/bin/env python3
"""
Clean up Markdown exported from the VS Code Codex extension.

Goals:
1. Remove giant data:image/...;base64,... payloads while preserving an image marker.
2. Make user / Codex turns visually explicit.
3. Collapse Codex's "N previous messages" execution-progress block.
4. Compact noisy tool logs such as "Searched the web" and "Created a file".

Usage:
    python3 codex_md_cleaner.py chat.md
    python3 codex_md_cleaner.py chat.md -o chat.cleaned.md
    python3 codex_md_cleaner.py chat.md --process drop
    python3 codex_md_cleaner.py chat.md --process keep
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

PREVIOUS_RE = re.compile(
    r"^\s*<details><summary>\s*(.*?)\s+previous messages?\s*</summary>\s*$",
    re.IGNORECASE,
)
DETAILS_SUMMARY_RE = re.compile(
    r"^\s*(?:>\s*)?<details><summary>(.*?)</summary>\s*$",
    re.IGNORECASE,
)
DATA_IMAGE_RE = re.compile(
    r"data:image/([A-Za-z0-9.+-]+);base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
WROTE_RE = re.compile(r"Wrote\s+`([^`]+)`", re.IGNORECASE)
SEARCH_RE = re.compile(r"Searched the web for\s+`?(.+?)`?\s*$", re.IGNORECASE)


def strip_quote(line: str) -> str:
    """Remove one Markdown blockquote marker from a Codex-export line."""
    if line == ">":
        return ""
    if line.startswith("> "):
        return line[2:]
    if line.startswith(">"):
        return line[1:].lstrip()
    return line


def collapse_blank_lines(lines: Iterable[str], max_blank: int = 1) -> list[str]:
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            out.append(line.rstrip())
        else:
            blanks += 1
            if blanks <= max_blank:
                out.append("")
    while out and not out[-1].strip():
        out.pop()
    return out


def replace_data_images(text: str) -> tuple[str, int]:
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        ext = match.group(1).upper()
        return f"[图片数据已省略：{ext}]"

    return DATA_IMAGE_RE.sub(repl, text), count


def extract_balanced_details(lines: list[str], start: int) -> tuple[list[str], int]:
    """Return a full <details>...</details> block and the next line index."""
    block: list[str] = []
    depth = 0
    i = start

    while i < len(lines):
        line = lines[i]
        block.append(line)

        # Codex export puts nested <details> on their own lines.
        opens = len(re.findall(r"<details(?:\s[^>]*)?>", line, flags=re.IGNORECASE))
        closes = len(re.findall(r"</details>", line, flags=re.IGNORECASE))
        depth += opens - closes

        i += 1
        if depth <= 0 and block:
            break

    return block, i


def clean_user_block(block: list[str], image_note: bool = True) -> str:
    content = [strip_quote(x) for x in block]

    # "User context" is export metadata, not part of the user's visible message.
    context_at = next(
        (i for i, x in enumerate(content) if x.strip().lower() == "user context"),
        None,
    )

    visible = content if context_at is None else content[:context_at]
    metadata = [] if context_at is None else content[context_at + 1 :]

    metadata_text = "\n".join(metadata)
    _, image_count = replace_data_images(metadata_text)

    # Also guard against data URIs unexpectedly appearing in the visible message.
    visible_text, visible_image_count = replace_data_images("\n".join(visible))
    image_count += visible_image_count

    visible_lines = collapse_blank_lines(visible_text.splitlines())

    if image_note and image_count:
        if visible_lines:
            visible_lines += [""]
        visible_lines.append(
            f"> 🖼️ *原消息包含 {image_count} 张图片；Base64 图片数据已移除。*"
        )

    return "\n".join(visible_lines).strip()


def summarize_tool_block(block: list[str]) -> list[str]:
    """Compact a nested Codex tool <details> block into one or a few short lines."""
    first = strip_quote(block[0]).strip()
    m = DETAILS_SUMMARY_RE.match(first)
    summary = m.group(1).strip() if m else "Tool activity"

    body = [strip_quote(x) for x in block[1:-1]]
    body_nonempty = [x.strip() for x in body if x.strip()]

    low = summary.lower()

    if "searched the web" in low or "search" in low:
        queries = []
        for line in body_nonempty:
            sm = SEARCH_RE.search(line)
            if sm:
                queries.append(sm.group(1))
        n = len(queries) or 1
        return [f"- 🔎 网页搜索 ×{n}"]

    if "created a file" in low or "create" in low:
        files = []
        for line in body_nonempty:
            wm = WROTE_RE.search(line)
            if wm:
                files.append(wm.group(1))
        if files:
            return [f"- 📝 创建文件：`{x}`" for x in files]
        return ["- 📝 创建了文件"]

    if "edited" in low or "updated" in low or "modified" in low:
        return [f"- ✏️ {summary}"]

    if "command" in low or "terminal" in low or "ran" in low:
        return [f"- ⌨️ {summary}"]

    # Unknown tool block: preserve the summary, discard verbose internals.
    return [f"- 🛠️ {summary}"]


def compact_process_block(block: list[str]) -> str:
    """
    Convert <details><summary>N previous messages</summary> into a readable,
    collapsed Codex execution log.
    """
    first = block[0].strip()
    m = PREVIOUS_RE.match(first)
    original_label = m.group(1).strip() if m else "若干"

    inner = block[1:-1]
    out: list[str] = []
    i = 0

    while i < len(inner):
        line = inner[i]

        # Nested tool detail block.
        stripped = strip_quote(line).strip()
        if DETAILS_SUMMARY_RE.match(stripped):
            nested, next_i = extract_balanced_details(inner, i)
            out.extend(summarize_tool_block(nested))
            i = next_i
            continue

        cleaned = strip_quote(line)
        cleaned, _ = replace_data_images(cleaned)
        out.append(cleaned.rstrip())
        i += 1

    out = collapse_blank_lines(out)

    # Improve readability: separate tool-summary bullets from prose naturally,
    # but do not rewrite the assistant's actual wording.
    body = "\n".join(out).strip()
    if not body:
        body = "*（无可读的中间过程）*"

    return (
        f"<details>\n"
        f"<summary>🛠️ Codex 中间执行过程（原导出：{original_label} previous messages）</summary>\n\n"
        f"{body}\n\n"
        f"</details>"
    )


def keep_process_block(block: list[str]) -> str:
    """Keep process text but still remove base64 data images."""
    text = "\n".join(block)
    text, _ = replace_data_images(text)
    return text.strip()


def is_previous_details_start(line: str) -> bool:
    return PREVIOUS_RE.match(line.strip()) is not None


def is_top_level_user_line(line: str) -> bool:
    # In Codex's Markdown export, top-level user messages are blockquotes.
    return line.startswith(">")


def clean_markdown(
    text: str,
    process_mode: str = "collapse",
    image_note: bool = True,
) -> str:
    lines = text.splitlines()

    title = None
    start = 0
    # Preserve the first H1 as conversation title.
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and re.match(r"^#\s+\S", lines[start]):
        title = lines[start].rstrip()
        start += 1

    sections: list[str] = []
    pending_process: list[str] = []
    i = start

    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue

        # Codex "previous messages" execution-progress details.
        if is_previous_details_start(lines[i]):
            block, i = extract_balanced_details(lines, i)
            if process_mode == "collapse":
                pending_process.append(compact_process_block(block))
            elif process_mode == "keep":
                pending_process.append(keep_process_block(block))
            # process_mode == "drop": intentionally discard it
            continue

        # User turn (Codex export uses a top-level Markdown blockquote).
        if is_top_level_user_line(lines[i]):
            block: list[str] = []
            while i < len(lines):
                if lines[i].startswith(">"):
                    block.append(lines[i])
                    i += 1
                    continue
                # Keep internal blank lines only if another quoted line follows.
                if (
                    not lines[i].strip()
                    and i + 1 < len(lines)
                    and lines[i + 1].startswith(">")
                ):
                    block.append(lines[i])
                    i += 1
                    continue
                break

            user_text = clean_user_block(block, image_note=image_note)
            if user_text:
                sections.append(f"## 👤 你\n\n{user_text}")
            continue

        # Assistant final response: gather until next user turn or process block.
        block = []
        while i < len(lines):
            if is_previous_details_start(lines[i]):
                break
            if lines[i].startswith(">"):
                break
            block.append(lines[i])
            i += 1

        assistant_text = "\n".join(collapse_blank_lines(block)).strip()
        assistant_text, _ = replace_data_images(assistant_text)

        if assistant_text or pending_process:
            parts = []
            if pending_process:
                parts.extend(pending_process)
                pending_process = []
            if assistant_text:
                parts.append(assistant_text)
            sections.append("## 🤖 Codex\n\n" + "\n\n".join(parts))

    # If the export ended immediately after a process block, do not lose it.
    if pending_process:
        sections.append("## 🤖 Codex\n\n" + "\n\n".join(pending_process))

    header = title or "# Codex 对话整理"
    generated_note = (
        "> 整理版：已移除 Base64 图片数据，并重新标记用户 / Codex 对话层级。"
    )

    result = header + "\n\n" + generated_note
    if sections:
        result += "\n\n---\n\n" + "\n\n---\n\n".join(sections)

    result = "\n".join(collapse_blank_lines(result.splitlines()))
    return result.rstrip() + "\n"


def build_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.cleaned{input_path.suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean and reformat Markdown exported from the VS Code Codex extension."
    )
    parser.add_argument("input", type=Path, help="Codex-exported Markdown file")
    parser.add_argument(
        "-o", "--output", type=Path, help="Output Markdown path (default: *.cleaned.md)"
    )
    parser.add_argument(
        "--process",
        choices=("collapse", "drop", "keep"),
        default="collapse",
        help=(
            "How to handle Codex 'previous messages' execution logs: "
            "collapse (default), drop, or keep"
        ),
    )
    parser.add_argument(
        "--drop-image-note",
        action="store_true",
        help="Remove image payloads without leaving an 'image omitted' note",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow output path to overwrite an existing file",
    )
    args = parser.parse_args()

    src = args.input.expanduser()
    if not src.is_file():
        parser.error(f"Input file does not exist: {src}")

    dst = (args.output or build_output_path(src)).expanduser()

    if dst.exists() and not args.overwrite:
        parser.error(
            f"Output already exists: {dst}\n"
            f"Use --overwrite or choose another path with -o."
        )

    raw = src.read_text(encoding="utf-8-sig")
    cleaned = clean_markdown(
        raw,
        process_mode=args.process,
        image_note=not args.drop_image_note,
    )
    dst.write_text(cleaned, encoding="utf-8")

    before = len(raw.encode("utf-8"))
    after = len(cleaned.encode("utf-8"))
    saved = before - after
    pct = (saved / before * 100) if before else 0

    print(f"Done: {dst}")
    print(f"Size: {before:,} -> {after:,} bytes  (reduced {pct:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
