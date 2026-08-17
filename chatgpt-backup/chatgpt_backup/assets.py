"""Persist images and other attachments next to the markdown files."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence

from . import model
from .config import log
from .model import Asset
from .util import atomic_write_bytes, sha1_bytes, short_id, slugify

# Signature -> extension. Ordered because some prefixes overlap.
_MAGIC: Sequence[tuple] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"%PDF", ".pdf"),
    (b"ID3", ".mp3"),
    (b"OggS", ".ogg"),
    (b"fLaC", ".flac"),
    (b"\x1f\x8b", ".gz"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"\x00\x00\x01\x00", ".ico"),
    (b"PK\x03\x04", ".zip"),
    (b"{\\rtf", ".rtf"),
    (b"\xd0\xcf\x11\xe0", ".doc"),
)

_FTYP_BRANDS = {
    b"heic": ".heic",
    b"heix": ".heic",
    b"hevc": ".heic",
    b"mif1": ".heic",
    b"avif": ".avif",
    b"M4A ": ".m4a",
    b"qt  ": ".mov",
}

_KIND_STEMS = {
    model.IMAGE: "image",
    model.AUDIO: "audio",
    model.VIDEO: "video",
    model.FILE: "file",
}

# Data-export members are named "file-<id>-<original name>"; we re-add a short
# id ourselves, so drop the long one to keep file names readable.
_EXPORT_PREFIX = re.compile(r"^file[-_][A-Za-z0-9]{4,}[-_]")


def clean_asset_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return name
    return _EXPORT_PREFIX.sub("", name) or name


def sniff_extension(data: bytes, mime: Optional[str] = None, name: Optional[str] = None) -> str:
    """Pick a file extension from magic bytes, then MIME type, then the name."""
    head = data[:32]
    if head[:4] == b"RIFF" and len(head) >= 12:
        container = head[8:12]
        if container == b"WEBP":
            return ".webp"
        if container == b"WAVE":
            return ".wav"
        if container == b"AVI ":
            return ".avi"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _FTYP_BRANDS:
            return _FTYP_BRANDS[brand]
        return ".mp4"
    for signature, extension in _MAGIC:
        if head.startswith(signature):
            if extension == ".zip" and name:
                suffix = Path(name).suffix.lower()
                if suffix in (".docx", ".xlsx", ".pptx", ".odt", ".epub", ".zip"):
                    return suffix
            return extension
    stripped = head.lstrip()[:64].lower()
    if stripped.startswith(b"<svg") or (stripped.startswith(b"<?xml") and b"<svg" in data[:512].lower()):
        return ".svg"

    if mime:
        clean_mime = mime.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_extension(clean_mime)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    if name:
        suffix = Path(name).suffix
        if 1 < len(suffix) <= 8:
            return suffix.lower()
    return ".bin"


@dataclass
class Fetched:
    data: bytes
    mime: Optional[str] = None
    name: Optional[str] = None


class AssetProvider:
    """Source of asset bytes. Subclasses implement :meth:`fetch`."""

    def fetch(self, asset: Asset) -> Optional[Fetched]:  # pragma: no cover - interface
        raise NotImplementedError


class LocalFileProvider(AssetProvider):
    """Reads assets that already live on disk (official data export)."""

    def fetch(self, asset: Asset) -> Optional[Fetched]:
        if not asset.source_path or not asset.source_path.is_file():
            return None
        try:
            data = asset.source_path.read_bytes()
        except OSError as exc:
            log.warning("读取本地附件失败 %s: %s", asset.source_path, exc)
            return None
        return Fetched(data=data, name=asset.name or asset.source_path.name)


class ChainProvider(AssetProvider):
    """Tries several providers in order (local export first, then network)."""

    def __init__(self, providers: Sequence[AssetProvider]) -> None:
        self.providers = [provider for provider in providers if provider is not None]

    def fetch(self, asset: Asset) -> Optional[Fetched]:
        for provider in self.providers:
            result = provider.fetch(asset)
            if result and result.data:
                return result
        return None


class AssetSaver:
    """Writes assets into ``assets_dir`` and fills in ``rel_path`` on each asset.

    Content is hashed so the same image referenced by several messages (or
    already saved by a previous run) is only written once.
    """

    def __init__(
        self,
        assets_dir: Path,
        provider: Optional[AssetProvider],
        rel_prefix: str = "assets",
        enabled: bool = True,
    ) -> None:
        self.assets_dir = assets_dir
        self.provider = provider
        self.rel_prefix = rel_prefix.rstrip("/")
        self.enabled = enabled
        self._by_hash: Dict[str, str] = {}
        self._by_file_id: Dict[str, str] = {}
        self.saved = 0
        self.reused = 0
        self.failed = 0

    def _target_name(self, asset: Asset, extension: str) -> str:
        base = clean_asset_name(asset.name) or ""
        stem = Path(base).stem if base else ""
        stem = slugify(stem, max_len=40, fallback="") if stem else ""
        if not stem:
            stem = _KIND_STEMS.get(asset.kind, "file")
        return f"{stem}-{short_id(asset.file_id.replace('file-', '').replace('file_', ''), 10)}{extension}"

    def save(self, asset: Asset) -> bool:
        if not self.enabled:
            return False

        cached = self._by_file_id.get(asset.file_id)
        if cached:
            asset.rel_path = cached
            asset.local_path = self.assets_dir / Path(cached).name
            self.reused += 1
            return True

        if self.provider is None:
            asset.failed = True
            return False

        fetched = self.provider.fetch(asset)
        if not fetched or not fetched.data:
            asset.failed = True
            self.failed += 1
            log.warning("附件下载失败: %s (%s)", asset.display_name, asset.file_id)
            return False

        digest = sha1_bytes(fetched.data)
        known = self._by_hash.get(digest)
        if known:
            asset.rel_path = known
            asset.local_path = self.assets_dir / Path(known).name
            self._by_file_id[asset.file_id] = known
            self.reused += 1
            return True

        if fetched.name and not asset.name:
            asset.name = fetched.name
        if fetched.mime and not asset.mime_type:
            asset.mime_type = fetched.mime

        extension = sniff_extension(fetched.data, asset.mime_type, asset.name)
        target = self.assets_dir / self._target_name(asset, extension)
        if target.exists() and target.stat().st_size == len(fetched.data):
            self.reused += 1
        else:
            atomic_write_bytes(target, fetched.data)
            self.saved += 1

        rel = f"{self.rel_prefix}/{target.name}" if self.rel_prefix else target.name
        asset.local_path = target
        asset.rel_path = rel
        asset.size_bytes = asset.size_bytes or len(fetched.data)
        if asset.kind == model.FILE and (asset.mime_type or "").startswith("image/"):
            asset.kind = model.IMAGE
        self._by_hash[digest] = rel
        self._by_file_id[asset.file_id] = rel
        return True

    def save_all(self, assets: Sequence[Asset]) -> None:
        for asset in assets:
            try:
                self.save(asset)
            except Exception as exc:  # keep backing up the transcript regardless
                asset.failed = True
                self.failed += 1
                log.warning("处理附件 %s 时出错: %s", asset.file_id, exc)
