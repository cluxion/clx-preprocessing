"""Universal Cluxion preprocessing agent plugin."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cluxion-agentplugin-preprocessing")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.3.47"

__all__ = ["__version__"]
