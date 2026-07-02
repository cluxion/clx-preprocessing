from __future__ import annotations

from cluxion_runtime.adapters.contract import work_item_from_adapter_payload
from cluxion_runtime.core.harness import build_harness_plan
from cluxion_runtime.core.types import AgentSurface


def _clarification_required(prompt: str) -> bool:
    item = work_item_from_adapter_payload({"prompt": prompt}, default_surface=AgentSurface.CODEX)
    plan = build_harness_plan(item)
    return plan.clarification_required


def test_vague_change_requests_require_clarification() -> None:
    assert _clarification_required("make it better")
    assert _clarification_required("improve this")
    assert _clarification_required("최적화 해줘")


def test_targeted_and_question_prompts_do_not_over_trigger() -> None:
    assert not _clarification_required("optimize src/cluxion_runtime/core/loop_auto.py hot loop")
    assert not _clarification_required("explain how the dispatch store works")
