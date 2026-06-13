from __future__ import annotations

"""Directory-plugin wrapper for Git installs."""

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from cluxion_agentplugin_preprocessing.plugin import register

__all__ = ["register"]
