"""Deterministic ambiguity detection and user clarification before queueing.

Question text is localized: the locale comes from item.metadata["locale"],
then the CLUXION_LANG environment variable, then Hangul detection on the
prompt itself, falling back to English. Detection keyword tuples stay
mixed-language on purpose — they classify user input, they are not output.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from cluxion_runtime.core.types import WorkIntent, WorkItem


@dataclass(frozen=True)
class ClarificationQuestion:
    """Single question the host agent must ask the user."""

    question_id: str
    prompt: str
    why: str
    blocking: bool = True


@dataclass(frozen=True)
class ClarificationResult:
    """Whether work can proceed or must ask the user first."""

    required: bool
    ready_for_queue: bool
    reason_codes: tuple[str, ...]
    questions: tuple[ClarificationQuestion, ...]
    resolved_direction: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "required": self.required,
            "ready_for_queue": self.ready_for_queue,
            "reason_codes": list(self.reason_codes),
            "resolved_direction": self.resolved_direction,
            "questions": [
                {
                    "question_id": question.question_id,
                    "prompt": question.prompt,
                    "why": question.why,
                    "blocking": question.blocking,
                }
                for question in self.questions
            ],
        }


LANG_ENV = "CLUXION_LANG"
DEFAULT_LOCALE = "en"

# question_id -> (prompt, why) per locale. Keep both locales key-identical;
# _question() falls back to English for any missing entry.
_QUESTION_TEXT: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "intent_direction": (
            "Confirm the direction in one sentence. (e.g. fix a bug / write docs / research only)",
            "The request intent can be read in several different ways.",
        ),
        "disambiguate_choice": (
            "The request contains ambiguous wording. Pick one of the options (A/B) or state the priority explicitly.",
            "Ambiguous instructions can lead to the wrong work queue.",
        ),
        "target_scope": (
            "Which file/module/feature should this target? Provide a path or symbol name.",
            "This is a coding task but no change target was specified.",
        ),
        "scope_boundary": (
            "The scope looks broad. Specify the paths or components to include/exclude.",
            "Whole-scope work can fail or cost far more than needed.",
        ),
        "resolve_conflict": (
            "Multiple work-type signals were detected. Choose the single top-priority goal for this turn.",
            "The request reads as several different kinds of work at once.",
        ),
    },
    "ko": {
        "intent_direction": (
            "어떤 방향으로 진행할지 한 문장으로 확정해 주세요. (예: 버그 수정 / 문서 작성 / 조사만)",
            "요청 의도가 여러 갈래로 해석될 수 있습니다.",
        ),
        "disambiguate_choice": (
            "애매한 표현이 있습니다. 원하는 결과를 A/B 중 하나로 골라 주시거나, 우선순위를 명시해 주세요.",
            "모호한 지시는 잘못된 작업큐로 이어질 수 있습니다.",
        ),
        "target_scope": (
            "어떤 파일/모듈/기능을 대상으로 할까요? 경로나 심볼 이름을 알려 주세요.",
            "코딩 작업인데 변경 대상이 명시되지 않았습니다.",
        ),
        "scope_boundary": (
            "범위가 넓어 보입니다. 포함/제외할 경로나 컴포넌트를 지정해 주세요.",
            "전체 범위 작업은 실패하거나 과도한 비용이 들 수 있습니다.",
        ),
        "resolve_conflict": (
            "서로 다른 작업 유형 신호가 감지됐습니다. 이번 턴의 1순위 목표를 하나만 선택해 주세요.",
            "동시에 여러 종류의 작업으로 해석됩니다.",
        ),
    },
}


def resolve_locale(text: str, explicit: str | None = None) -> str:
    """Pick the question locale: explicit > CLUXION_LANG > Hangul detection > English."""
    if explicit:
        candidate = explicit.strip().lower()[:2]
        if candidate in _QUESTION_TEXT:
            return candidate
    env = os.environ.get(LANG_ENV, "").strip().lower()[:2]
    if env in _QUESTION_TEXT:
        return env
    if any("가" <= char <= "힣" for char in text):
        return "ko"
    return DEFAULT_LOCALE


def _question(question_id: str, locale: str) -> ClarificationQuestion:
    catalog = _QUESTION_TEXT.get(locale, _QUESTION_TEXT[DEFAULT_LOCALE])
    prompt, why = catalog.get(question_id, _QUESTION_TEXT[DEFAULT_LOCALE][question_id])
    return ClarificationQuestion(question_id, prompt, why)


# PHRASES: multi-word or longer markers — use plain substring match
_AMBIGUOUS_PHRASES = (
    "maybe",
    "perhaps",
    "not sure",
    "unsure",
    "아마",
    "어느",
    "둘 중",
    "모르겠",
    "헷갈",
    "애매",
)
# WORDS: short standalone tokens — match on whole-word boundaries only
_AMBIGUOUS_WORDS = ("or", "either")


def _has_ambiguous(text: str) -> bool:
    """Return True if text contains ambiguous phrasing or choice words."""
    t = text.lower()
    if any(p in t for p in _AMBIGUOUS_PHRASES):
        return True
    return any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in _AMBIGUOUS_WORDS)


_SCOPE_KEYWORDS = ("all", "everything", "전부", "전체", "모든")
_TARGET_MISSING_KEYWORDS = ("fix", "implement", "refactor", "patch", "수정", "구현", "리팩터", "패치")
# Change-requests with no nameable target: "make it better", "improve this",
# "optimize", "고쳐줘", "개선해줘". Questions/explanations stay out on purpose.
_VAGUE_CHANGE_VERBS = (
    "improve",
    "make it better",
    "make this better",
    "optimize",
    "optimise",
    "clean up",
    "cleanup",
    "enhance",
    "polish",
    "개선",
    "최적화",
    "다듬",
    "고쳐",
    "좋게",
    "낫게",
)
_VAGUE_PRONOUN_OBJECTS = ("it", "this", "that", "things", "stuff", "이거", "그거", "저거", "이것", "그것")
_VAGUE_PROMPT_CHAR_LIMIT = 120
_LOW_CONFIDENCE_THRESHOLD = 0.62


def assess_clarification(item: WorkItem, intent: WorkIntent) -> ClarificationResult:
    """Decide whether the host agent must ask the user before queueing work."""
    text = item.prompt.lower()
    locale = resolve_locale(item.prompt, str(item.metadata.get("locale", "")) or None)
    reasons: list[str] = []
    questions: list[ClarificationQuestion] = []

    if intent.confidence < _LOW_CONFIDENCE_THRESHOLD and _needs_direction_confirmation(text, intent):
        reasons.append("low_intent_confidence")
        questions.append(_question("intent_direction", locale))

    if _has_ambiguous(text):
        reasons.append("ambiguous_language")
        questions.append(_question("disambiguate_choice", locale))

    if intent.category == "engineering" and _looks_like_coding_without_target(text):
        reasons.append("missing_target_scope")
        questions.append(_question("target_scope", locale))

    if _looks_like_vague_change_request(text):
        reasons.append("vague_change_request")
        questions.append(_question("target_scope", locale))

    if _has_any(text, _SCOPE_KEYWORDS) and not _has_any(
        text, ("repo", "project", "directory", "레포", "프로젝트", "폴더")
    ):
        reasons.append("broad_scope_without_boundary")
        questions.append(_question("scope_boundary", locale))

    if _conflicting_signals(text, intent):
        reasons.append("conflicting_signals")
        questions.append(_question("resolve_conflict", locale))

    if item.metadata.get("clarification_answers"):
        return ClarificationResult(
            required=False,
            ready_for_queue=True,
            reason_codes=("user_clarified",),
            questions=(),
            resolved_direction=item.metadata.get("clarification_answers", ""),
        )

    if not questions:
        return ClarificationResult(
            required=False,
            ready_for_queue=True,
            reason_codes=("direction_clear",),
            questions=(),
            resolved_direction=intent.direction,
        )

    deduped = _dedupe_questions(questions)
    return ClarificationResult(
        required=True,
        ready_for_queue=False,
        reason_codes=tuple(dict.fromkeys(reasons)),
        questions=tuple(deduped),
        resolved_direction="",
    )


def _needs_direction_confirmation(text: str, intent: WorkIntent) -> bool:
    if intent.category == "general" and len(text) < 240:
        return False
    if _has_ambiguous(text):
        return True
    if intent.category in {"engineering", "security", "documentation", "local_model"}:
        return True
    return len(text) > 400


def _looks_like_vague_change_request(text: str) -> bool:
    """Short imperative change-requests without a nameable target must clarify."""
    if len(text) > _VAGUE_PROMPT_CHAR_LIMIT:
        return False
    if not _has_any(text, _VAGUE_CHANGE_VERBS):
        return False
    target_markers = (".py", ".rs", ".ts", ".js", "/", "src", "test", "파일", "모듈", "함수", "클래스", "class ", "def ")
    if _has_any(text, target_markers):
        return False
    words = set(re.findall(r"[a-z가-힣]+", text))
    has_pronoun_object = bool(words & set(_VAGUE_PRONOUN_OBJECTS))
    has_named_object = any(len(word) > 3 and word not in _VAGUE_PRONOUN_OBJECTS for word in words - set(["make", "better", "improve", "optimize", "clean", "up", "enhance", "polish"]))
    return has_pronoun_object or not has_named_object


def _looks_like_coding_without_target(text: str) -> bool:
    if not _has_any(text, _TARGET_MISSING_KEYWORDS):
        return False
    target_markers = (".py", ".rs", ".ts", ".js", "/", "\\", "src/", "tests/", "파일", "모듈", "함수", "class ")
    return not _has_any(text, target_markers)


def _conflicting_signals(text: str, intent: WorkIntent) -> bool:
    coding = _has_any(text, ("code", "implement", "fix", "patch", "코드", "구현", "수정"))
    docs = _has_any(text, ("docs", "readme", "문서", "가이드"))
    security = _has_any(text, ("security", "audit", "보안", "취약점"))
    local = intent.local_model_requested or _has_any(text, ("local model", "vllm", "로컬"))
    active = sum(signal for signal in (coding, docs, security, local) if signal)
    return active >= 2 and intent.confidence < 0.8


def _dedupe_questions(questions: list[ClarificationQuestion]) -> list[ClarificationQuestion]:
    seen: set[str] = set()
    unique: list[ClarificationQuestion] = []
    for question in questions:
        if question.question_id in seen:
            continue
        seen.add(question.question_id)
        unique.append(question)
    return unique


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


__all__ = ["ClarificationQuestion", "ClarificationResult", "assess_clarification", "resolve_locale"]
