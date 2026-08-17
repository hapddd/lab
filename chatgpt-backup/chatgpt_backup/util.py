"""Small helpers shared across the package (stdlib only)."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

# Characters that are illegal (or merely painful) inside file names on the
# three platforms we care about.
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_DASHES = re.compile(r"-{2,}")

# OpenAI wraps citation/navigation metadata in private-use code points, e.g.
# "\ue200cite\ue202turn0search1\ue201". The payload between the delimiters is
# machine-readable junk, so the whole span has to go — not just the delimiters.
_MARKER_SPANS = tuple(
    re.compile(f"{start}[^\n]{{0,600}}?{end}")
    for start, end in (("\ue200", "\ue201"), ("\ue203", "\ue204"), ("\ue205", "\ue206"))
)
_PRIVATE_USE = re.compile(r"[\ue000-\uf8ff\U000f0000-\U000ffffd]")

MAX_NAME_BYTES = 120


def slugify(text: Optional[str], max_len: int = 48, fallback: str = "untitled") -> str:
    """Make ``text`` safe for use as a single path segment, keeping CJK intact."""
    raw = strip_private_use((text or "").strip())
    raw = _ILLEGAL_FS.sub(" ", raw)
    raw = _WHITESPACE.sub("-", raw)
    raw = _DASHES.sub("-", raw).strip("-. ")
    if not raw:
        return fallback
    raw = raw[:max_len]
    # ext4/APFS limit names by bytes, not code points; CJK costs 3 bytes each.
    while len(raw.encode("utf-8")) > MAX_NAME_BYTES and raw:
        raw = raw[:-1]
    return raw.strip("-. ") or fallback


def strip_private_use(text: str) -> str:
    for pattern in _MARKER_SPANS:
        text = pattern.sub("", text)
    return _PRIVATE_USE.sub("", text)


def to_datetime(ts: Any) -> Optional[dt.datetime]:
    """Convert a ChatGPT timestamp (epoch seconds, or ISO string) to local time."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(ts)).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(ts, str):
        try:
            return dt.datetime.fromtimestamp(float(ts)).astimezone()
        except ValueError:
            pass
        try:
            cleaned = ts.replace("Z", "+00:00")
            parsed = dt.datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone()
    return None


def fmt_datetime(value: Optional[dt.datetime], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return value.strftime(fmt) if value else "未知时间"


def fmt_iso(value: Optional[dt.datetime]) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def date_prefix(value: Optional[dt.datetime]) -> str:
    return value.strftime("%Y-%m-%d") if value else "0000-00-00"


def short_id(value: Optional[str], length: int = 8) -> str:
    text = (value or "").replace("-", "")
    return text[:length] if text else "00000000"


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def human_size(num: Optional[int]) -> str:
    if not num or num < 0:
        return "未知大小"
    step = 1024.0
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def unique_path(directory: Path, name: str) -> Path:
    """Return ``directory/name``, appending -1, -2 ... when already taken."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-{os.getpid()}{suffix}"


def first(values: Iterable[Any], default: Any = None) -> Any:
    for value in values:
        if value:
            return value
    return default


def dig(data: Any, *keys: Any, default: Any = None) -> Any:
    """Safe nested lookup through dicts and lists."""
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, (list, tuple)) and isinstance(key, int):
            if -len(current) <= key < len(current):
                current = current[key]
            else:
                return default
        else:
            return default
        if current is None:
            return default
    return current


def supports_color(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("NO_COLOR") is None
