from __future__ import annotations

import pytest

from cluxion_runtime.core import clarification
from cluxion_runtime.core.harness import build_harness_plan
from cluxion_runtime.core.types import WorkItem


@pytest.fixture(autouse=True)
def _no_lang_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(clarification.LANG_ENV, raising=False)


def test_ambiguous_prompt_requires_clarification() -> None:
    item = WorkItem("w-ambig", "아마 둘 중 하나로 수정해줘. 어느 쪽인지 모르겠어.")
    plan = build_harness_plan(item)
    assert plan.clarification_required is True
    assert plan.preprocessing.mode == "needs_clarification"
    assert plan.execution.strategy == "ask_user_before_queue"
    assert plan.clarification_questions


def test_clear_prompt_skips_clarification() -> None:
    item = WorkItem("w-clear", "Is this possible?")
    plan = build_harness_plan(item)
    assert plan.clarification_required is False
    assert plan.preprocessing.mode == "simple_answer"


def test_user_clarification_metadata_bypasses_questions() -> None:
    item = WorkItem(
        "w-answered",
        "아마 수정해줘",
        metadata={"clarification_answers": "src/foo.py 버그 수정"},
    )
    plan = build_harness_plan(item)
    assert plan.clarification_required is False


def test_korean_prompt_gets_korean_questions() -> None:
    plan = build_harness_plan(WorkItem("w-ko", "아마 둘 중 하나로 수정해줘."))
    assert plan.clarification_required is True
    assert any("애매한 표현" in question for question in plan.clarification_questions)


def test_english_prompt_gets_english_questions() -> None:
    plan = build_harness_plan(WorkItem("w-en", "Maybe fix it, not sure which of the two."))
    assert plan.clarification_required is True
    assert all(question.isascii() for question in plan.clarification_questions)
    assert any("ambiguous wording" in question for question in plan.clarification_questions)


def test_env_overrides_prompt_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(clarification.LANG_ENV, "ko")
    plan = build_harness_plan(WorkItem("w-env", "Maybe fix it, not sure which of the two."))
    assert any("애매한 표현" in question for question in plan.clarification_questions)


def test_metadata_locale_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(clarification.LANG_ENV, "ko")
    plan = build_harness_plan(WorkItem("w-meta", "아마 둘 중 하나로 수정해줘.", metadata={"locale": "en"}))
    assert all(question.isascii() for question in plan.clarification_questions)


def test_resolve_locale_fallbacks() -> None:
    assert clarification.resolve_locale("plain english text") == "en"
    assert clarification.resolve_locale("한국어 텍스트") == "ko"
    assert clarification.resolve_locale("plain", explicit="ko-KR") == "ko"
    assert clarification.resolve_locale("plain", explicit="fr") == "en"
