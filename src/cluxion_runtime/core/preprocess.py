"""Task preprocessing and long-task queue segment generation."""

from __future__ import annotations

import hashlib

from cluxion_runtime.core.types import AnswerPolicy, PreprocessResult, QueueSegment, WorkItem, WorkPriority

_SIMPLE_PROMPT_CHAR_LIMIT = 800
_SIMPLE_PROMPT_TOKEN_LIMIT = 160
_SIMPLE_CONTEXT_TOKEN_LIMIT = 512
_VERIFICATION_PROMPT_CHAR_LIMIT = 1_200
_VERIFICATION_PROMPT_TOKEN_LIMIT = 240
_SIGNAL_SCAN_CHAR_LIMIT = 4_096
_PREPROCESS_INTENT_CATEGORIES = {"engineering", "security", "documentation", "local_model"}
_PREPROCESS_KEYWORDS = (
    "audit",
    "benchmark",
    "build",
    "code",
    "debug",
    "deploy",
    "fix",
    "implement",
    "install",
    "patch",
    "pytest",
    "refactor",
    "security",
    "test",
    "vllm",
    "vllm-mlx",
    "구현",
    "긴",
    "디버그",
    "리팩터",
    "문서",
    "배포",
    "보안",
    "설치",
    "수정",
    "업그레이드",
    "전처리",
    "점검",
    "테스트",
    "패치",
)
_CURRENT_FACT_KEYWORDS = (
    "current",
    "latest",
    "recent",
    "release",
    "today",
    "version",
    "최신",
    "최근",
    "오늘",
    "현재",
    "릴리즈",
    "버전",
)
_EXTERNAL_SOURCE_KEYWORDS = (
    "docs",
    "github",
    "news",
    "paper",
    "pypi",
    "reference",
    "source",
    "url",
    "논문",
    "뉴스",
    "문서",
    "소스",
    "출처",
    "파이피",
    "깃허브",
    "링크",
)
_RUNTIME_STATE_KEYWORDS = (
    "ci",
    "command",
    "env",
    "file",
    "installed",
    "log",
    "output",
    "path",
    "port",
    "process",
    "terminal",
    "venv",
    "경로",
    "로그",
    "명령",
    "설치",
    "실행",
    "출력",
    "파일",
    "포트",
    "프로세스",
    "환경",
)
_CHECK_REQUEST_KEYWORDS = (
    "check",
    "confirm",
    "inspect",
    "look up",
    "search",
    "verify",
    "검사",
    "검색",
    "보기",
    "점검",
    "확인",
)


def preprocess_work(
    item: WorkItem,
    *,
    max_segment_chars: int = 72_000,
    max_segment_tokens: int = 32_000,
    intent_category: str | None = None,
    local_model_requested: bool = False,
    force_mode: str | None = None,
) -> PreprocessResult:
    """Normalize a task into model-friendly directives and segment evidence."""
    token_estimate = estimate_tokens(item.prompt)
    split_required = len(item.prompt) > max_segment_chars or token_estimate > max_segment_tokens
    signal_text = _bounded_signal_text(item.prompt)
    category = intent_category or _lightweight_intent_category(signal_text)
    verification_signals = _verification_signals(signal_text)
    if force_mode == "needs_clarification":
        mode, preprocess_required, reason_codes = "needs_clarification", False, ("clarification_required",)
    else:
        mode, preprocess_required, reason_codes = _preprocess_policy(
            item,
            token_estimate=token_estimate,
            split_required=split_required,
            intent_category=category,
            local_model_requested=local_model_requested,
            verification_signals=verification_signals,
        )
    segments = _segments_for(item.prompt, max_segment_chars, max_segment_tokens, split_required, preprocess_required)
    normalized = _normalized_prompt(item, segments, token_estimate, split_required)
    evidence = (
        tuple(f"{segment.segment_id}:{segment.checksum}:{segment.token_estimate}" for segment in segments)
        if preprocess_required
        else ()
    )
    return PreprocessResult(
        normalized_prompt=normalized,
        segments=segments,
        token_estimate=token_estimate,
        split_required=split_required,
        effort=_effort_for(item, token_estimate, split_required, mode=mode, preprocess_required=preprocess_required),
        evidence=evidence,
        mode=mode,
        preprocess_required=preprocess_required,
        reason_codes=reason_codes,
        answer_policy=_answer_policy_for(
            item,
            intent_category=category,
            mode=mode,
            split_required=split_required,
            reason_codes=reason_codes,
            verification_signals=verification_signals,
        ),
    )


def estimate_tokens(text: str) -> int:
    """Estimate tokens conservatively without depending on the old Cluxion OS package."""
    ascii_chars = len(text.encode("ascii", "ignore"))
    cjk = len(text) - ascii_chars
    return max(1, cjk + ascii_chars // 4)


def _preprocess_policy(
    item: WorkItem,
    *,
    token_estimate: int,
    split_required: bool,
    intent_category: str | None,
    local_model_requested: bool,
    verification_signals: tuple[str, ...],
) -> tuple[str, bool, tuple[str, ...]]:
    category = intent_category or "general"
    route = item.model_route.lower()
    if split_required:
        return "queued", True, ("split_required",)
    if local_model_requested or _explicit_local_route(route):
        return "standard", True, ("local_model_route",)
    if item.priority <= WorkPriority.HIGH:
        return "standard", True, ("priority_requires_harness",)
    if item.expected_ram_mb > 0:
        return "standard", True, ("explicit_memory_budget",)
    if item.context_tokens > _SIMPLE_CONTEXT_TOKEN_LIMIT:
        return "standard", True, ("large_context",)
    if _can_use_verification_fast_path(item, token_estimate, category, verification_signals):
        return "verification_answer", False, ("verification_required", *verification_signals)
    if category in _PREPROCESS_INTENT_CATEGORIES:
        return "standard", True, (f"intent_{category}",)
    if token_estimate > _SIMPLE_PROMPT_TOKEN_LIMIT:
        return "standard", True, ("prompt_token_threshold",)
    if len(item.prompt) > _SIMPLE_PROMPT_CHAR_LIMIT:
        return "standard", True, ("prompt_char_threshold",)
    if _has_any(item.prompt.lower(), _PREPROCESS_KEYWORDS):
        return "standard", True, ("preprocess_keyword",)
    return "simple_answer", False, ("short_prompt", "no_tool_or_local_signals")


def _can_use_verification_fast_path(
    item: WorkItem,
    token_estimate: int,
    category: str,
    verification_signals: tuple[str, ...],
) -> bool:
    if not verification_signals:
        return False
    if category in {"engineering", "security", "local_model"}:
        return False
    if token_estimate > _VERIFICATION_PROMPT_TOKEN_LIMIT:
        return False
    return len(item.prompt) <= _VERIFICATION_PROMPT_CHAR_LIMIT


def _answer_policy_for(
    item: WorkItem,
    *,
    intent_category: str,
    mode: str,
    split_required: bool,
    reason_codes: tuple[str, ...],
    verification_signals: tuple[str, ...],
) -> AnswerPolicy:
    required_checks = _required_checks_for(
        item,
        intent_category=intent_category,
        mode=mode,
        split_required=split_required,
        verification_signals=verification_signals,
    )
    verification_required = bool(required_checks)
    citation_required = "external_source" in verification_signals or "current_fact" in verification_signals
    uncertainty_level = _uncertainty_level_for(
        mode=mode,
        verification_required=verification_required,
        citation_required=citation_required,
        split_required=split_required,
    )
    return AnswerPolicy(
        response_contract=_response_contract_for(
            mode=mode,
            verification_required=verification_required,
            citation_required=citation_required,
        ),
        verification_required=verification_required,
        citation_required=citation_required,
        uncertainty_level=uncertainty_level,
        required_checks=required_checks,
        rules=_rules_for(
            mode=mode,
            verification_required=verification_required,
            citation_required=citation_required,
            reason_codes=reason_codes,
        ),
    )


def _required_checks_for(
    item: WorkItem,
    *,
    intent_category: str,
    mode: str,
    split_required: bool,
    verification_signals: tuple[str, ...],
) -> tuple[str, ...]:
    checks: list[str] = []
    if "current_fact" in verification_signals:
        checks.append("verify_current_or_recent_fact")
    if "external_source" in verification_signals:
        checks.append("cite_external_source_or_document")
    if "runtime_state" in verification_signals:
        checks.append("inspect_runtime_state_before_claiming")
    if "explicit_check_request" in verification_signals:
        checks.append("run_requested_check_or_state_not_run")
    if intent_category == "engineering":
        checks.append("tie_claims_to_file_diff_or_command_output")
    if intent_category == "security":
        checks.append("tie_security_claims_to_evidence")
    if mode == "queued" or split_required:
        checks.append("preserve_segment_checksums_in_synthesis")
    if _explicit_local_route(item.model_route.lower()):
        checks.append("verify_local_model_endpoint_before_claiming_ready")
    return tuple(dict.fromkeys(checks))


def _response_contract_for(*, mode: str, verification_required: bool, citation_required: bool) -> str:
    if mode == "queued":
        return "synthesize_from_segment_evidence"
    if verification_required and citation_required:
        return "verify_and_cite_before_answer"
    if verification_required:
        return "verify_before_answer"
    if mode == "verification_answer":
        return "verify_before_answer"
    return "direct_answer_with_uncertainty_boundary"


def _uncertainty_level_for(
    *,
    mode: str,
    verification_required: bool,
    citation_required: bool,
    split_required: bool,
) -> str:
    if split_required or mode == "queued":
        return "high"
    if verification_required or citation_required:
        return "medium"
    return "low"


def _rules_for(
    *,
    mode: str,
    verification_required: bool,
    citation_required: bool,
    reason_codes: tuple[str, ...],
) -> tuple[str, ...]:
    rules = [
        "If the available context is insufficient, say that clearly before proceeding.",
        "Do not fabricate file state, external facts, tool results, or model availability.",
        "For current or environment-specific claims, verify through tools before presenting them as facts.",
        "Separate verified facts from inferences and unknowns when accuracy matters.",
        "If a check was not run, say it was not run; do not imply that it passed.",
    ]
    if verification_required:
        rules.append("Run the required checks before making the requested factual claim.")
    if citation_required:
        rules.append(
            "Attach source references for external or time-sensitive claims when the host surface supports it."
        )
    if mode == "verification_answer":
        rules.append("Keep the answer short after verification; do not expand into unrelated planning.")
    if "verification_required" in reason_codes:
        rules.append("Treat short verification prompts as fact-finding, not as ordinary simple chat.")
    return tuple(dict.fromkeys(rules))


def _segments_for(
    prompt: str,
    max_segment_chars: int,
    max_segment_tokens: int,
    split_required: bool,
    preprocess_required: bool,
) -> tuple[QueueSegment, ...]:
    if not preprocess_required:
        return ()
    if split_required:
        return tuple(_split_segments(prompt, max_segment_chars, max_segment_tokens))
    return (_segment("seg_000", prompt, 0, len(prompt)),)


def _split_segments(text: str, max_chars: int, max_tokens: int) -> list[QueueSegment]:
    return [
        _segment(f"seg_{index:03d}", content, start, end)
        for index, (content, start, end) in enumerate(_collect_segment_ranges(text, max_chars, max_tokens, 0))
    ]


def _collect_segment_ranges(text: str, max_chars: int, max_tokens: int, origin: int) -> list[tuple[str, int, int]]:
    segments: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        boundary = _find_boundary(text, start, end)
        raw_chunk = text[start:boundary]
        chunk = raw_chunk.strip()
        if chunk:
            orig_start = origin + start + (len(raw_chunk) - len(raw_chunk.lstrip()))
            orig_end = orig_start + len(chunk)
            if estimate_tokens(chunk) > max_tokens and max_chars > 1_024:
                segments.extend(_collect_segment_ranges(chunk, max(1_024, max_chars // 2), max_tokens, orig_start))
            else:
                segments.append((chunk, orig_start, orig_end))
        start = max(boundary, start + 1)
    return segments


def _find_boundary(text: str, start: int, end: int) -> int:
    if end >= len(text):
        return len(text)
    window = text[start:end]
    for marker in ("\n\n", "\n", ". ", "? ", "! ", "。"):
        index = window.rfind(marker)
        if index > max(200, len(window) // 2):
            return start + index + len(marker)
    return end


def _segment(segment_id: str, content: str, start: int, end: int) -> QueueSegment:
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return QueueSegment(
        segment_id=segment_id,
        char_start=start,
        char_end=end,
        token_estimate=estimate_tokens(content),
        checksum=checksum,
        preview=_summarize_edges(content, 480),
        content=content,
    )


def _normalized_prompt(
    item: WorkItem,
    segments: tuple[QueueSegment, ...],
    token_estimate: int,
    split_required: bool,
) -> str:
    if not split_required:
        return item.prompt
    lines = [
        "[cluxion_queue_required]",
        f"work_id={item.work_id}",
        f"surface={item.surface}",
        f"priority={item.priority.name.lower()}",
        f"estimated_tokens={token_estimate}",
        f"segment_count={len(segments)}",
        "Process segments in order, preserve evidence, then synthesize.",
        "[segment_index]",
    ]
    lines.extend(f"{segment.segment_id} checksum={segment.checksum} preview={segment.preview}" for segment in segments)
    return "\n".join(lines)


def _effort_for(
    item: WorkItem,
    token_estimate: int,
    split_required: bool,
    *,
    mode: str,
    preprocess_required: bool,
) -> str:
    if not preprocess_required or mode == "simple_answer":
        return "simple"
    if item.priority <= WorkPriority.HIGH or split_required or token_estimate > 24_000:
        return "high"
    if token_estimate > 6_000:
        return "medium"
    return "low"


def _summarize_edges(text: str, max_chars: int) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return clean
    edge = max(120, max_chars // 2)
    return f"{clean[:edge]} ... {clean[-edge:]}"


def _lightweight_intent_category(prompt: str) -> str:
    text = prompt
    if _has_any(text, ("vllm-mlx", "mlx", "local model", "로컬모델", "로컬 모델", "serve-local")):
        return "local_model"
    if _has_any(text, ("security", "audit", "vulnerability", "보안", "취약점", "시크릿")):
        return "security"
    if _has_any(text, ("test", "pytest", "unit", "테스트", "검증")):
        return "engineering"
    if _has_any(text, ("code", "implement", "fix", "refactor", "패치", "수정", "구현", "리팩터")):
        return "engineering"
    if _has_any(text, ("docs", "readme", "문서", "가이드")):
        return "documentation"
    return "general"


def _verification_signals(prompt: str) -> tuple[str, ...]:
    text = prompt
    signals: list[str] = []
    if _has_any(text, _CURRENT_FACT_KEYWORDS):
        signals.append("current_fact")
    if _has_any(text, _EXTERNAL_SOURCE_KEYWORDS):
        signals.append("external_source")
    if _has_any(text, _RUNTIME_STATE_KEYWORDS):
        signals.append("runtime_state")
    if _has_any(text, _CHECK_REQUEST_KEYWORDS):
        signals.append("explicit_check_request")
    return tuple(dict.fromkeys(signals))


def _bounded_signal_text(prompt: str) -> str:
    if len(prompt) <= _SIGNAL_SCAN_CHAR_LIMIT:
        return prompt.lower()
    edge = max(1, _SIGNAL_SCAN_CHAR_LIMIT // 2)
    return f"{prompt[:edge]}\n{prompt[-edge:]}".lower()


def _explicit_local_route(route: str) -> bool:
    return route.startswith(("local/", "mlx/", "vllm-mlx/", "vllm_mlx/")) and route != "local/default"


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


__all__ = ["estimate_tokens", "preprocess_work"]
