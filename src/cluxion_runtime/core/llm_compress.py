"""LLM-backed message summarization for context compression stage 2.

Calls the main model via ``hermes -z`` (or ``cluxion_hermes_call`` when available).
Stage 2 is Python-only; the Rust ``context.rs`` mirror intentionally does not
replicate LLM calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_TIMEOUT_S = 120.0
_HERMES_BIN = "hermes"
logger = logging.getLogger(__name__)

_SUMMARY_INSTRUCTIONS = (
    "Summarize each message by importance. PRESERVE ABOVE ALL: the user's intent and "
    "direction, decisions made, unresolved items, file paths / identifiers / commands — "
    "regardless of language (Korean or English). "
    "ONLY summarize content actually present in the message. NEVER invent, add, infer, "
    "or fabricate any identifier, number, name, port, path, or fact that is not in the "
    "original. If unsure whether something is in the source, OMIT it. "
    "Compress everything else. Each summary < 10% of the original. "
    'Output STRICT JSON: {"<index>": "<summary>", ...} only.'
)

_HARD_TOKEN_RE = re.compile(
    r"\b(?:"
    r"\d+(?:\.\d+)?(?:k|m|만|억)?"
    r"|[A-Za-z][\w.-]*\d[\w.-]*"
    r"|\d[\w.-]+"
    r")\b",
    re.IGNORECASE,
)
_NUMERIC_SUFFIX_RE = re.compile(r"^(\d+(?:\.\d+)?)(k|m|만|억)?$", re.IGNORECASE)
_STRIP_LABEL_PREFIX_RE = re.compile(r"(?:\w+:\s*)+", re.IGNORECASE)
_SUFFIX_MULTIPLIERS = {"k": 1000, "m": 1_000_000, "만": 10_000, "억": 100_000_000}


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
    hallucination_stripped = 0
    for idx in indices:
        key = str(idx)
        if key not in parsed or not isinstance(parsed[key], str) or not parsed[key].strip():
            continue
        summary = parsed[key].strip()
        if idx < 0 or idx >= len(messages):
            result[idx] = summary
            continue
        try:
            guarded, stripped = _apply_hallucination_guard(summary, messages[idx].content)
        except Exception:
            logger.exception("llm_compress: hallucination guard failed for message %s", idx)
            return None
        if guarded is None:
            return None
        hallucination_stripped += stripped
        result[idx] = guarded
    if not result:
        return None
    if hallucination_stripped > 0:
        logger.info(
            "llm_compress: stripped %d hallucinated token(s) from summaries",
            hallucination_stripped,
        )
    return result


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[,\s]+", "", text.lower())


def _numeric_variants(token: str) -> set[str]:
    norm = _normalize_for_match(token)
    variants = {norm}
    match = _NUMERIC_SUFFIX_RE.match(norm)
    if not match:
        return variants
    base, suffix = match.group(1), match.group(2)
    variants.add(_normalize_for_match(base))
    if suffix:
        multiplier = _SUFFIX_MULTIPLIERS.get(suffix.lower())
        if multiplier is not None:
            try:
                expanded = str(int(float(base) * multiplier))
            except ValueError:
                expanded = None
            if expanded:
                variants.add(expanded)
    return variants


def _token_traceable_in_source(token: str, source: str) -> bool:
    norm_source = _normalize_for_match(source)
    norm_token = _normalize_for_match(token)

    if norm_token in norm_source:
        return True

    for variant in _numeric_variants(token):
        if variant in norm_source:
            return True

    digit_groups = re.findall(r"\d+", norm_token)
    if digit_groups:
        all_digits_traceable = True
        for digits in digit_groups:
            if digits in norm_source:
                continue
            traceable = any(variant in norm_source for variant in _numeric_variants(digits))
            if not traceable:
                all_digits_traceable = False
                break
        if all_digits_traceable:
            alpha_prefix = re.sub(r"[\d._-]+", "", norm_token)
            if not alpha_prefix or alpha_prefix in norm_source:
                return True

    if "." in norm_token:
        without_dots = norm_token.replace(".", "")
        if without_dots in norm_source or without_dots in norm_source.replace(".", ""):
            return True

    return False


def _extract_hard_tokens(summary: str) -> list[str]:
    return list(dict.fromkeys(_HARD_TOKEN_RE.findall(summary)))


def _strip_fabricated_token(summary: str, token: str) -> str | None:
    escaped = re.escape(token)
    pattern = rf"(?:{_STRIP_LABEL_PREFIX_RE.pattern})?{escaped}\b"
    stripped = re.sub(pattern, "", summary, count=1, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+", " ", stripped).strip(" \t\n\r,;:-")
    stripped = re.sub(r"[,;:\-]\s*$", "", stripped).strip()
    if not stripped or not re.search(r"\w", stripped):
        return None
    return stripped


def _apply_hallucination_guard(summary: str, source: str) -> tuple[str | None, int]:
    guarded = summary
    stripped_count = 0
    for token in _extract_hard_tokens(summary):
        if _token_traceable_in_source(token, source):
            continue
        updated = _strip_fabricated_token(guarded, token)
        if updated is None:
            return None, stripped_count
        guarded = updated
        stripped_count += 1
    return guarded, stripped_count


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