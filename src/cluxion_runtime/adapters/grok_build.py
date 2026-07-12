"""Grok Build CLI helper for reimplementation development support.

The Cluxion runtime plan never calls this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

GROK_COMPOSER_25_FAST = "grok-composer-2.5-fast"
GROK_BUILD_MODEL = "grok-4.5"


def build_grok_composer_command(
    prompt: str,
    *,
    cwd: Path,
    model: str = GROK_BUILD_MODEL,
    json_output: bool = True,
    check: bool = True,
) -> tuple[str, ...]:
    """Build the Grok Build headless command for developers to run manually."""
    cmd = ["grok", "-m", model, "--cwd", str(cwd)]
    if json_output:
        cmd.extend(["--output-format", "json"])
    if check:
        cmd.append("--check")
    cmd.extend(["-p", prompt])
    return tuple(cmd)


__all__ = ["GROK_BUILD_MODEL", "GROK_COMPOSER_25_FAST", "build_grok_composer_command"]
