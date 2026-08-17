"""Read an official ChatGPT data export (Settings → 数据管理 → 导出数据).

The export is a zip containing ``conversations.json`` plus every uploaded and
generated file, named ``file-<id>-<original name>`` (image generations live in
``dalle-generations/``). Because the images are already inside the archive this
source needs no credentials at all and is the most reliable path.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..assets import AssetProvider, Fetched, clean_asset_name
from ..config import log
from ..model import Asset

CONVERSATIONS_MEMBER = "conversations.json"
_FILE_ID = re.compile(r"(file[-_][A-Za-z0-9]{4,})")
_SKIP_MEMBERS = {"chat.html", "user.json", "message_feedback.json", "shared_conversations.json", "model_comparisons.json"}


def _normalise_id(file_id: str) -> str:
    return file_id.replace("-", "").replace("_", "").lower()


class ExportArchive:
    """Uniform access to an export whether it is still zipped or unpacked."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"导出文件不存在: {self.path}")
        self._zip: Optional[zipfile.ZipFile] = None
        self._members: List[str] = []
        self._index: Dict[str, str] = {}

        if self.path.is_dir():
            self._members = [
                item.relative_to(self.path).as_posix()
                for item in self.path.rglob("*")
                if item.is_file()
            ]
        elif zipfile.is_zipfile(self.path):
            self._zip = zipfile.ZipFile(self.path)
            self._members = [info.filename for info in self._zip.infolist() if not info.is_dir()]
        elif self.path.suffix.lower() == ".json":
            # Someone handed us conversations.json directly.
            self._members = [self.path.name]
        else:
            raise ValueError(f"无法识别的导出格式（需要 .zip、目录或 conversations.json）: {self.path}")

        self._build_index()

    # -- members ------------------------------------------------------------ #
    def _build_index(self) -> None:
        for member in self._members:
            base = member.rsplit("/", 1)[-1]
            if base in _SKIP_MEMBERS or base.endswith(".json") and base == CONVERSATIONS_MEMBER:
                continue
            match = _FILE_ID.match(base) or _FILE_ID.search(base)
            if not match:
                continue
            key = _normalise_id(match.group(1))
            # Prefer the shortest member name (the plain upload over derivatives).
            existing = self._index.get(key)
            if existing is None or len(member) < len(existing):
                self._index[key] = member
        log.debug("导出包内索引到 %d 个附件", len(self._index))

    def read_member(self, member: str) -> Optional[bytes]:
        try:
            if self._zip is not None:
                with self._zip.open(member) as handle:
                    return handle.read()
            if self.path.is_dir():
                return (self.path / member).read_bytes()
            if member == self.path.name:
                return self.path.read_bytes()
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            log.debug("读取导出成员 %s 失败: %s", member, exc)
        return None

    def find_member(self, file_id: str) -> Optional[str]:
        key = _normalise_id(file_id)
        member = self._index.get(key)
        if member:
            return member
        # Pointers are sometimes truncated relative to the on-disk name.
        for candidate_key, candidate in self._index.items():
            if candidate_key.startswith(key) or key.startswith(candidate_key):
                return candidate
        return None

    # -- conversations ------------------------------------------------------ #
    def load_conversations(self) -> List[Dict[str, Any]]:
        member = None
        if self.path.is_file() and self.path.suffix.lower() == ".json":
            member = self.path.name
        else:
            for candidate in self._members:
                if candidate.rsplit("/", 1)[-1] == CONVERSATIONS_MEMBER:
                    member = candidate
                    break
        if member is None:
            raise FileNotFoundError(f"导出包里找不到 {CONVERSATIONS_MEMBER}: {self.path}")

        raw = self.read_member(member)
        if raw is None:
            raise OSError(f"无法读取 {member}")
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            data = data.get("conversations") or [data]
        if not isinstance(data, list):
            raise ValueError(f"{CONVERSATIONS_MEMBER} 格式不正确")
        return [item for item in data if isinstance(item, dict)]

    def asset_provider(self) -> "ExportAssetProvider":
        return ExportAssetProvider(self)

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> "ExportArchive":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class ExportAssetProvider(AssetProvider):
    def __init__(self, archive: ExportArchive) -> None:
        self.archive = archive

    def fetch(self, asset: Asset) -> Optional[Fetched]:
        if asset.source_path and asset.source_path.is_file():
            return Fetched(data=asset.source_path.read_bytes(), name=asset.source_path.name)
        if not asset.file_id:
            return None
        member = self.archive.find_member(asset.file_id)
        if not member:
            return None
        data = self.archive.read_member(member)
        if not data:
            return None
        return Fetched(data=data, name=asset.name or clean_asset_name(member.rsplit("/", 1)[-1]))


def find_latest_export(search_dirs: Optional[List[Path]] = None) -> Optional[Path]:
    """Locate the newest ``*.zip`` that looks like a ChatGPT export."""
    if search_dirs is None:
        home = Path.home()
        search_dirs = [
            home / "Downloads",
            home / "下载",
            home / "Desktop",
            home / "桌面",
            Path.cwd(),
        ]
    candidates: List[Path] = []
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for item in directory.glob("*.zip"):
            name = item.name.lower()
            if any(token in name for token in ("chatgpt", "openai", "conversations", "data-export", "export")):
                candidates.append(item)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0]
