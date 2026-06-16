"""LLM-backed message summarization for context compression stage 2.

Calls the main model via ``hermes -z`` (or ``cluxion_hermes_call`` when available).
Stage 2 is Python-only; the Rust ``context.rs`` mirror intentionally does not
replicate LLM calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_TIMEOUT_S = 120.0
_HERMES_BIN = "hermes"

_SUMMARY_INSTRUCTIONS = (
    "Summarize each message by importance. PRESERVE ABOVE ALL: the user's intent and "
    "direction, decisions made, unresolved items, file paths / identifiers / commands — "
    "regardless of language (Korean or English). "
    "Compress everything else. Each summary < 10% of the original. "
    'Output STRICT JSON: {"<index>": "<summary>", ...} only.'
)


class _MessageLike(Protocol):
    role: str
    content: str


def hermes_available() -> bool:
    return shutil.which(_HERMES_BIN) is not None


def summarize_messages(
    messages: Sequence[_MessageLike],
    indices: Sequence[int],
    instructions: str | None = None,
    *,
    model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[int, str] | None:
    """Summarize selected messages via hermes -z. Returns None on any failure."""
    if not indices:
        return {}
    if not hermes_available():
        return None
    prompt = _build_prompt(messages, indices, instructions or _SUMMARY_INSTRUCTIONS)
    try:
        stdout = _call_hermes_oneshot(prompt, model=model, timeout_s=timeout_s)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return None
    parsed = _parse_summary_json(stdout)
    if parsed is None:
        return None
    result: dict[int, str] = {}
    for idx in indices:
        key = str(idx)
        if key in parsed and isinstance(parsed[key], str) and parsed[key].strip():
            result[idx] = parsed[key].strip()
    if not result:
        return None
    return result


def _build_prompt(
    messages: Sequence[_MessageLike],
    indices: Sequence[int],
    instructions: str,
) -> str:
    blocks: list[str] = [instructions, "", "Messages to summarize:"]
    for idx in indices:
        if idx < 0 or idx >= len(messages):
            continue
        msg = messages[idx]
        blocks.append(f"--- message index {idx} ({msg.role}) ---")
        blocks.append(msg.content)
        blocks.append("")
    return "\n".join(blocks)


def _call_hermes_oneshot(prompt: str, *, model: str | None, timeout_s: float) -> str:
    prev = os.environ.get("CLUXION_PREPROCESS_IN_COMPRESS")
    os.environ["CLUXION_PREPROCESS_IN_COMPRESS"] = "1"
    try:
        try:
            from cluxion_hermes_call.core import hermes_oneshot  # type: ignore[import-not-found]

            return str(hermes_oneshot(prompt, model=model, timeout_s=timeout_s))
        except ImportError:
            pass

        cmd = [_HERMES_BIN, "-z", prompt]
        if model:
            cmd[1:1] = ["-m", model]
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return completed.stdout
    finally:
        if prev is None:
            os.environ.pop("CLUXION_PREPROCESS_IN_COMPRESS", None)
        else:
            os.environ["CLUXION_PREPROCESS_IN_COMPRESS"] = prev


def _parse_summary_json(stdout: str) -> Mapping[str, object] | None:
    text = stdout.strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


__all__ = ["DEFAULT_TIMEOUT_S", "hermes_available", "summarize_messages"]