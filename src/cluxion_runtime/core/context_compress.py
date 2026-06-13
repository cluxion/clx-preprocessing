"""Deterministic context compression: stage 1 of the 70% -> 30% pipeline.

Pure-Python mirror of ``rust/cluxion_queue/src/context.rs`` — every
constant, threshold, ordering rule, and the token estimator must stay in
lockstep so the three backends produce identical output (parity-tested).

What stays untouched: pinned messages (explicit ``pinned``, the first
user message = task intent, the most recent ``keep_recent`` turns).
Stages run oldest-first and stop as soon as usage reaches the target:
  A. truncate long messages (head + tail excerpt)
  B. drop exact duplicates (trimmed-content match)
  C. fold remaining old turns into one-line digests
If the target is still not met the result carries ``ai_summary_request``
telling the host AI which messages to summarize and what to preserve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cluxion_runtime.core.preprocess import estimate_tokens

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_CONTEXT_LIMIT = 128_000
DEFAULT_TRIGGER_RATIO = 0.70
DEFAULT_TARGET_RATIO = 0.30
DEFAULT_KEEP_RECENT = 4
TRUNCATE_MIN_TOKENS = 512
TRUNCATE_HEAD_CHARS = 1200
TRUNCATE_TAIL_CHARS = 600
DEDUP_MIN_CHARS = 40
DIGEST_LINE_CHARS = 120
SUMMARY_REQUEST_LIMIT = 8

# Known context windows by model-name substring, checked in order.
# Conservative: only widely fixed values; everything else falls back to
# DEFAULT_CONTEXT_LIMIT (callers should pass context_limit_tokens).
MODEL_CONTEXT: tuple[tuple[str, int], ...] = (
    ("claude", 200_000),
    ("gemini", 1_000_000),
    ("gpt", 128_000),
    ("llama", 128_000),
)


@dataclass
class _Msg:
    role: str
    content: str
    pinned: bool


def compress(payload: Mapping[str, object]) -> dict[str, object]:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise RuntimeError("missing required field: messages")
    messages = [
        _Msg(
            role=str(raw.get("role", "user")) if isinstance(raw, dict) else "user",
            content=str(raw.get("content", "")) if isinstance(raw, dict) else "",
            pinned=bool(raw.get("pinned", False)) if isinstance(raw, dict) else False,
        )
        for raw in raw_messages
    ]

    context_limit = _resolve_context_limit(payload)
    trigger_ratio = _ratio(payload, "trigger_ratio", DEFAULT_TRIGGER_RATIO)
    target_ratio = _ratio(payload, "target_ratio", DEFAULT_TARGET_RATIO)
    keep_recent = _uint(payload, "keep_recent_turns", DEFAULT_KEEP_RECENT)

    tokens_before = sum(estimate_tokens(m.content) for m in messages)
    usage_before = tokens_before / context_limit
    target_tokens = int(target_ratio * context_limit)

    if usage_before < trigger_ratio:
        return _result_payload(
            messages,
            tokens_before,
            tokens_before,
            context_limit,
            [],
            None,
            _pinned_indices(messages, keep_recent),
        )

    pinned = _pinned_indices(messages, keep_recent)
    stages: list[str] = []

    total = tokens_before
    total, changed = _stage_truncate(messages, pinned, total, target_tokens)
    if changed:
        stages.append("truncate")
    if total > target_tokens:
        total, changed = _stage_dedup(messages, pinned, total, target_tokens)
        if changed:
            stages.append("dedup")
    if total > target_tokens:
        total, changed = _stage_digest(messages, pinned, total, target_tokens)
        if changed:
            stages.append("digest")

    summary_request = None
    if total > target_tokens:
        summary_request = _build_summary_request(messages, pinned, total, target_tokens)

    return _result_payload(messages, tokens_before, total, context_limit, stages, summary_request, pinned)


def _resolve_context_limit(payload: Mapping[str, object]) -> int:
    limit = payload.get("context_limit_tokens")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        return limit
    model = payload.get("model")
    if isinstance(model, str):
        lowered = model.lower()
        for pattern, known_limit in MODEL_CONTEXT:
            if pattern in lowered:
                return known_limit
    return DEFAULT_CONTEXT_LIMIT


def _ratio(payload: Mapping[str, object], key: str, default: float) -> float:
    value = payload.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 < float(value) < 1.0:
        return float(value)
    return default


def _uint(payload: Mapping[str, object], key: str, default: int) -> int:
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _pinned_indices(messages: list[_Msg], keep_recent: int) -> list[int]:
    pinned = [idx for idx, msg in enumerate(messages) if msg.pinned]
    first_user = next((idx for idx, msg in enumerate(messages) if msg.role == "user"), None)
    if first_user is not None and first_user not in pinned:
        pinned.append(first_user)
    recent_start = max(0, len(messages) - keep_recent)
    for idx in range(recent_start, len(messages)):
        if idx not in pinned:
            pinned.append(idx)
    pinned.sort()
    return pinned


def _stage_truncate(messages: list[_Msg], pinned: list[int], total: int, target: int) -> tuple[int, bool]:
    changed = False
    for idx, msg in enumerate(messages):
        if total <= target:
            break
        if idx in pinned:
            continue
        tokens = estimate_tokens(msg.content)
        if tokens <= TRUNCATE_MIN_TOKENS:
            continue
        if len(msg.content) <= TRUNCATE_HEAD_CHARS + TRUNCATE_TAIL_CHARS:
            continue
        elided = len(msg.content) - TRUNCATE_HEAD_CHARS - TRUNCATE_TAIL_CHARS
        head = msg.content[:TRUNCATE_HEAD_CHARS]
        tail = msg.content[len(msg.content) - TRUNCATE_TAIL_CHARS :]
        replacement = f"{head}\n[...cluxion: {elided} chars elided...]\n{tail}"
        total = total - tokens + estimate_tokens(replacement)
        msg.content = replacement
        changed = True
    return total, changed


def _stage_dedup(messages: list[_Msg], pinned: list[int], total: int, target: int) -> tuple[int, bool]:
    changed = False
    seen: dict[str, int] = {}
    for idx, msg in enumerate(messages):
        trimmed = msg.content.strip()
        if len(trimmed) < DEDUP_MIN_CHARS:
            continue
        if trimmed in seen:
            if total <= target or idx in pinned:
                continue
            tokens = estimate_tokens(msg.content)
            replacement = f"[cluxion: duplicate of message #{seen[trimmed]} elided]"
            total = total - tokens + estimate_tokens(replacement)
            msg.content = replacement
            changed = True
        else:
            seen[trimmed] = idx
    return total, changed


def _stage_digest(messages: list[_Msg], pinned: list[int], total: int, target: int) -> tuple[int, bool]:
    changed = False
    for idx, msg in enumerate(messages):
        if total <= target:
            break
        if idx in pinned:
            continue
        tokens = estimate_tokens(msg.content)
        first_line = msg.content.split("\n", 1)[0][:DIGEST_LINE_CHARS]
        replacement = f"[cluxion digest] {msg.role}: {first_line} [{tokens} tokens elided]"
        new_tokens = estimate_tokens(replacement)
        if new_tokens >= tokens:
            continue
        total = total - tokens + new_tokens
        msg.content = replacement
        changed = True
    return total, changed


def _build_summary_request(messages: list[_Msg], pinned: list[int], total: int, target: int) -> dict[str, object]:
    candidates = [(estimate_tokens(msg.content), idx) for idx, msg in enumerate(messages) if idx not in pinned]
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    indices = [idx for _, idx in candidates[:SUMMARY_REQUEST_LIMIT]]
    return {
        "reason": "deterministic stages insufficient",
        "current_tokens": total,
        "target_tokens": target,
        "summarize_indices": indices,
        "instructions": (
            "Summarize each listed message, preserving: user intent, decisions made, "
            "unresolved items, file paths and identifiers. Replace each with a summary "
            "under 10% of its original length."
        ),
    }


def _result_payload(
    messages: list[_Msg],
    tokens_before: int,
    tokens_after: int,
    context_limit: int,
    stages: list[str],
    summary_request: dict[str, object] | None,
    pinned: list[int],
) -> dict[str, object]:
    return {
        "ok": True,
        "compressed": bool(stages),
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "usage_before": tokens_before / context_limit,
        "usage_after": tokens_after / context_limit,
        "context_limit": context_limit,
        "stages_applied": stages,
        "pinned_indices": pinned,
        "messages": [{"role": m.role, "content": m.content, "pinned": m.pinned} for m in messages],
        "ai_summary_request": summary_request,
    }


__all__ = ["compress", "estimate_tokens"]
