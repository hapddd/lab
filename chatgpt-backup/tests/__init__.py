"""Test suite for chatgpt-backup (stdlib unittest, no external deps)."""

import sys
from pathlib import Path

# Allow `python3 -m unittest discover` from either the repo root or this folder.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
