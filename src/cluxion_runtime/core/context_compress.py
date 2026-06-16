"""Context compression: 70% trigger -> 30% target pipeline.

Stage 1 (deterministic) mirrors ``rust/cluxion_queue/src/context.rs`` — every
constant, threshold, ordering rule, and the token estimator must stay in
lockstep so the three backends produce identical Stage-1 output (parity-tested).

Stages 2 (LLM summarization via ``hermes -z``) and 3 (hybrid forgetting) are
Python-only; the Rust mirror intentionally does not replicate LLM or forgetforge
calls. Stage 4 (last-resort truncation of pinned recent turns) is also
Python-only — it runs when every remaining message is pinned yet still exceeds
the target. Disable stages 2-4 with ``enable_llm_summary`` / ``enable_forget``
for Stage-1 parity.

What stays untouched: pinned messages (explicit ``pinned``, the first
user message = task intent, the most recent ``keep_recent`` turns).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cluxion_runtime.core.hybrid_forget import apply_hybrid_forget
from cluxion_runtime.core.llm_compress import hermes_available, summarize_messages
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
    enable_llm = _bool_flag(payload, "enable_llm_summary", True)
    enable_forget = _bool_flag(payload, "enable_forget", True)
    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    timeout_raw = payload.get("llm_timeout_s")
    timeout_s = float(timeout_raw) if isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool) else 120.0

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

    summary_request: dict[str, object] | None = None
    dropped_without_backup = False
    over_target_pinned_only = False
    forced_over_target = False

    if total > target_tokens and enable_llm and hermes_available():
        summary_request = _build_summary_request(messages, pinned, total, target_tokens)
        indices = summary_request["summarize_indices"]
        if isinstance(indices, list) and indices:
            summaries = summarize_messages(
                messages,
                indices,  # type: ignore[arg-type]
                str(summary_request.get("instructions", "")),
                model=model,
                timeout_s=timeout_s,
            )
            if summaries is not None:
                for idx, summary in summaries.items():
                    if idx in pinned or idx < 0 or idx >= len(messages):
                        continue
                    old_tokens = estimate_tokens(messages[idx].content)
                    messages[idx].content = summary
                    total = total - old_tokens + estimate_tokens(summary)
                stages.append("llm_summary")
                summary_request = None
        # fail-safe: keep Stage-1 output and ai_summary_request on LLM failure

    if total > target_tokens and enable_forget:
        forget_result = apply_hybrid_forget(
            messages,
            pinned,
            total,
            target_tokens,
            session_id=session_id,
        )
        messages = forget_result.messages
        total = forget_result.tokens_after
        dropped_without_backup = forget_result.dropped_without_backup
        over_target_pinned_only = forget_result.over_target_pinned_only
        if forget_result.dropped_indices:
            stages.append("forget")
        pinned = _pinned_indices(messages, keep_recent)

    intent_idx = _first_user_index(messages)
    if total > target_tokens and (
        over_target_pinned_only or not any(idx not in pinned for idx in range(len(messages)))
    ):
        total, changed = _stage_truncate_pinned_recent(
            messages, keep_recent, total, target_tokens, intent_idx=intent_idx
        )
        if changed:
            stages.append("truncate_pinned_recent")
        over_target_pinned_only = False

    if total > target_tokens:
        if summary_request is None:
            summary_request = _build_summary_request(messages, pinned, total, target_tokens)
        intent_tokens = (
            estimate_tokens(messages[intent_idx].content) if intent_idx is not None else 0
        )
        if intent_tokens > target_tokens:
            forced_over_target = True
            over_target_pinned_only = True

    return _result_payload(
        messages,
        tokens_before,
        total,
        context_limit,
        stages,
        summary_request,
        pinned,
        dropped_without_backup=dropped_without_backup,
        over_target_pinned_only=over_target_pinned_only,
        forced_over_target=forced_over_target,
    )


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


def _bool_flag(payload: Mapping[str, object], key: str, default: bool) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return default


def _first_user_index(messages: list[_Msg]) -> int | None:
    return next((idx for idx, msg in enumerate(messages) if msg.role == "user"), None)


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


def _apply_head_tail_truncate(content: str) -> str | None:
    if estimate_tokens(content) <= TRUNCATE_MIN_TOKENS:
        return None
    if len(content) <= TRUNCATE_HEAD_CHARS + TRUNCATE_TAIL_CHARS:
        return None
    elided = len(content) - TRUNCATE_HEAD_CHARS - TRUNCATE_TAIL_CHARS
    head = content[:TRUNCATE_HEAD_CHARS]
    tail = content[len(content) - TRUNCATE_TAIL_CHARS :]
    return f"{head}\n[...cluxion: {elided} chars elided...]\n{tail}"


def _stage_truncate(messages: list[_Msg], pinned: list[int], total: int, target: int) -> tuple[int, bool]:
    changed = False
    for idx, msg in enumerate(messages):
        if total <= target:
            break
        if idx in pinned:
            continue
        replacement = _apply_head_tail_truncate(msg.content)
        if replacement is None:
            continue
        tokens = estimate_tokens(msg.content)
        total = total - tokens + estimate_tokens(replacement)
        msg.content = replacement
        changed = True
    return total, changed


def _pinned_recent_indices(messages: list[_Msg], keep_recent: int, intent_idx: int | None) -> list[int]:
    recent_start = max(0, len(messages) - keep_recent)
    return [idx for idx in range(recent_start, len(messages)) if idx != intent_idx]


def _stage_truncate_pinned_recent(
    messages: list[_Msg],
    keep_recent: int,
    total: int,
    target: int,
    *,
    intent_idx: int | None,
) -> tuple[int, bool]:
    """Last-resort: truncate pinned recent turns (never intent) until total <= target."""
    if total <= target:
        return total, False

    candidates = _pinned_recent_indices(messages, keep_recent, intent_idx)
    changed = False
    while total > target:
        progressed = False
        for idx in candidates:
            if total <= target:
                break
            replacement = _apply_head_tail_truncate(messages[idx].content)
            if replacement is None:
                continue
            tokens = estimate_tokens(messages[idx].content)
            total = total - tokens + estimate_tokens(replacement)
            messages[idx].content = replacement
            changed = True
            progressed = True
        if not progressed:
            break
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
    *,
    dropped_without_backup: bool = False,
    over_target_pinned_only: bool = False,
    forced_over_target: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
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
    if dropped_without_backup:
        result["dropped_without_backup"] = True
    if over_target_pinned_only:
        result["over_target_pinned_only"] = True
    if forced_over_target:
        result["forced_over_target"] = True
    return result


__all__ = ["compress", "estimate_tokens"]