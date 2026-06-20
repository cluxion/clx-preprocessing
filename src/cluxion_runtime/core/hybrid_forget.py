"""Hybrid forgetting for context compression stage 3.

Recoverable messages are cold-demoted via forgetforge when available; true junk
is removed permanently. Stage 3 is Python-only; the Rust mirror does not run this.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cluxion_runtime.core.preprocess import estimate_tokens

if TYPE_CHECKING:
    from collections.abc import Sequence

def _resolve_bin(name: str) -> str:
    candidate = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which(name) or name


_FORGETFORGE_BIN = _resolve_bin("forgetforge")
_JUNK_MARKERS = ("[cluxion: duplicate", "[cluxion digest]")
_IMPORTANCE_KEYWORDS = (
    "decide",
    "decision",
    "unresolved",
    "todo",
    "path",
    "file",
    "implement",
    "error",
    "fix",
    "must",
    "require",
    "intent",
    "command",
    "/",
    "\\",
    ".py",
    ".rs",
    ".ts",
    ".js",
    "git",
    "결정",
    "의도",
    "방향",
    "수정",
    "오류",
    "구현",
    "경로",
    "파일",
    "명령",
    "필수",
    "반드시",
    "미해결",
    "할일",
    "변경",
    "핵심",
    "중요",
)


class _MessageLike(Protocol):
    role: str
    content: str


@dataclass
class ForgetResult:
    messages: list
    tokens_after: int
    dropped_indices: list[int]
    dropped_without_backup: bool
    over_target_pinned_only: bool


def forgetforge_available() -> bool:
    return (os.path.isfile(_FORGETFORGE_BIN) and os.access(_FORGETFORGE_BIN, os.X_OK)) or (
        shutil.which(_FORGETFORGE_BIN) is not None
    )


def apply_hybrid_forget(
    messages: list,
    pinned: Sequence[int],
    total: int,
    target: int,
    *,
    session_id: str | None = None,
) -> ForgetResult:
    """Drop lowest-importance non-pinned messages until total <= target."""
    pinned_set = set(pinned)
    if total <= target:
        return ForgetResult(messages, total, [], False, False)

    removable = [idx for idx in range(len(messages)) if idx not in pinned_set]
    if not removable:
        return ForgetResult(messages, total, [], False, True)

    scored = sorted(
        ((idx, _importance_score(idx, messages[idx], len(messages))) for idx in removable),
        key=lambda pair: (pair[1], pair[0]),
    )

    to_drop: set[int] = set()
    dropped_without_backup = False
    current_total = total
    prefix = session_id or "cluxion-ctx"

    for idx, _score in scored:
        if current_total <= target:
            break
        msg = messages[idx]
        tokens = estimate_tokens(msg.content)
        if not _is_true_junk(msg.content):
            backed_up = _cold_demote(msg.content, f"{prefix}-{uuid.uuid4().hex[:12]}")
            if not backed_up:
                dropped_without_backup = True
        to_drop.add(idx)
        current_total -= tokens

    new_messages = [msg for i, msg in enumerate(messages) if i not in to_drop]
    over_pinned_only = current_total > target and not any(
        i not in pinned_set and i not in to_drop for i in range(len(messages))
    )
    return ForgetResult(
        new_messages,
        max(0, current_total),
        sorted(to_drop),
        dropped_without_backup,
        over_pinned_only,
    )


def _importance_score(idx: int, msg: _MessageLike, total: int) -> float:
    score = 0.0
    if total > 1:
        score += (idx / (total - 1)) * 40.0
    content = msg.content
    lowered = content.lower()
    if any(kw in lowered for kw in _IMPORTANCE_KEYWORDS):
        score += 25.0
    if re.search(r"/[\w./-]+", content) or re.search(
        r"\b[\w.-]+\.(py|rs|ts|js|md|yaml|yml|toml)\b", content
    ):
        score += 20.0
    if _is_true_junk(content):
        score -= 60.0
    if len(content.strip()) < 20:
        score -= 10.0
    return score


def _is_true_junk(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return True
    return any(marker in stripped for marker in _JUNK_MARKERS)


def _cold_demote(content: str, store_id: str) -> bool:
    if not forgetforge_available():
        return False
    try:
        subprocess.run(
            [_FORGETFORGE_BIN, "store", store_id, "--content", content],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return True
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return False


__all__ = [
    "ForgetResult",
    "apply_hybrid_forget",
    "forgetforge_available",
]