"""Paths, defaults and logging setup."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "chatgpt-backup"

# Chinese desktops localise ~/Documents to ~/文档; the user asked for the latter.
DOC_DIR_CANDIDATES = ("文档", "Documents", "ドキュメント", "문서")
DEFAULT_BACKUP_SUBDIR = "chat_bak"

ENV_OUT_DIR = "CHATGPT_BACKUP_DIR"
ENV_ACCESS_TOKEN = "CHATGPT_ACCESS_TOKEN"
ENV_SESSION_TOKEN = "CHATGPT_SESSION_TOKEN"
ENV_CONFIG_DIR = "CHATGPT_BACKUP_CONFIG_DIR"

STATE_FILE = ".backup-state.json"
INDEX_FILE = "index.md"
ASSETS_DIRNAME = "assets"
CONVERSATIONS_DIRNAME = "conversations"

log = logging.getLogger(APP_NAME)


def documents_dir() -> Path:
    """Best guess at the user's Documents folder, preferring the localised name."""
    home = Path.home()
    xdg = _xdg_documents_dir()
    if xdg is not None and xdg != home:
        return xdg
    for name in DOC_DIR_CANDIDATES:
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home / DOC_DIR_CANDIDATES[0]


def _xdg_documents_dir() -> Optional[Path]:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "user-dirs.dirs"
    if config.is_file():
        try:
            for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith("XDG_DOCUMENTS_DIR"):
                    continue
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                path = Path(value).expanduser()
                if path.is_dir():
                    return path
        except (OSError, IndexError):
            pass
    try:
        result = subprocess.run(
            ["xdg-user-dir", "DOCUMENTS"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            path = Path(result.stdout.strip()).expanduser()
            if path.is_dir():
                return path
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def default_output_dir() -> Path:
    env_value = os.environ.get(ENV_OUT_DIR)
    if env_value:
        return Path(env_value).expanduser()
    return documents_dir() / DEFAULT_BACKUP_SUBDIR


def config_dir() -> Path:
    env_value = os.environ.get(ENV_CONFIG_DIR)
    if env_value:
        return Path(env_value).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def auth_file() -> Path:
    return config_dir() / "auth.json"


def settings_file() -> Path:
    return config_dir() / "settings.json"


@dataclass
class Settings:
    """User-tweakable knobs, persisted in ``settings.json``."""

    output_dir: Optional[str] = None
    limit: int = 20
    include_system: bool = False
    include_tools: bool = False
    include_thoughts: bool = True
    all_branches: bool = False
    download_assets: bool = True
    layout: str = "folder"  # folder | flat
    user_agent: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = path or settings_file()
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("配置文件无法解析，使用默认值: %s (%s)", path, exc)
            return cls()
        known = {f for f in cls.__dataclass_fields__ if f != "extras"}
        kwargs = {key: value for key, value in data.items() if key in known}
        extras = {key: value for key, value in data.items() if key not in known}
        instance = cls(**kwargs)
        instance.extras = extras
        return instance

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: getattr(self, key) for key in self.__dataclass_fields__ if key != "extras"}
        payload.update(self.extras)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


def setup_logging(verbose: bool = False, quiet: bool = False, logfile: Optional[Path] = None) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)

    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(logfile, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        log.addHandler(file_handler)
