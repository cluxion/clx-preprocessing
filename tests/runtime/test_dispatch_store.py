from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cluxion_runtime.core.dispatch_store import (
    DispatchStoreError,
    build_briefing_payload,
    load_dispatch_bundle,
    next_dispatch_step,
    persist_dispatch_bundle,
    record_dispatch_result,
)
from cluxion_runtime.core.harness import build_harness_plan
from cluxion_runtime.core.types import AgentSurface, ResourceSnapshot, WorkItem

if TYPE_CHECKING:
    from pathlib import Path

    from cluxion_runtime.core.types import HarnessPlan

_SNAPSHOT = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=40_000, swap_used_mb=0, cpu_percent=20.0)


@pytest.fixture(scope="module")
def queued_plan() -> HarnessPlan:
    # Long enough to cross the 72k-char split threshold and force queued mode;
    # clarification_answers marks the direction as already confirmed by the user.
    prompt = "\n".join(f"REQ-{idx}: implement work item and record evidence token {idx}." for idx in range(1500))
    assert len(prompt) > 72_000
    item = WorkItem(
        "w-queued",
        prompt,
        surface=AgentSurface.HERMES,
        metadata={"clarification_answers": "implement every REQ line in order"},
    )
    plan = build_harness_plan(item, snapshot=_SNAPSHOT)
    assert plan.execution.queue_required is True
    return plan


def _drain_ids(work_id: str, dispatch_dir: Path) -> list[str]:
    step_ids: list[str] = []
    while True:
        step = next_dispatch_step(work_id, dispatch_dir=dispatch_dir)
        if not step["ready"]:
            return step_ids
        step_id = str(step["step"]["step_id"])
        step_ids.append(step_id)
        record_dispatch_result(work_id, step_id, result=f"done:{step_id}", dispatch_dir=dispatch_dir)


def test_persist_skips_plans_without_queue(tmp_path: Path) -> None:
    plan = build_harness_plan(WorkItem("w-short", "작업: 작은 버그를 고쳐줘."), snapshot=_SNAPSHOT)
    assert plan.execution.queue_required is False
    assert persist_dispatch_bundle(plan, dispatch_dir=tmp_path) is None


def test_persist_and_load_roundtrip(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    path = persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    assert path is not None and path.parent == tmp_path
    bundle = load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)
    steps = bundle["steps"]
    assert isinstance(steps, list) and len(steps) >= 2
    assert all(step["status"] == "queued" for step in steps)
    assert all(step["checksum"] for step in steps)


def test_next_marks_step_running_on_disk(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    payload = next_dispatch_step("w-queued", dispatch_dir=tmp_path)
    assert payload["ready"] is True
    assert payload["step"]["content"]
    assert payload["step"]["checksum"]
    reloaded = load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)
    statuses = [step["status"] for step in reloaded["steps"]]
    assert statuses.count("running") == 1


def test_next_reports_not_ready_when_nothing_queued(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    while next_dispatch_step("w-queued", dispatch_dir=tmp_path)["ready"]:
        pass  # consume every queued step without recording results
    payload = next_dispatch_step("w-queued", dispatch_dir=tmp_path)
    assert payload["ready"] is False
    assert payload["synthesis_ready"] is False  # running steps are not successes


def test_full_drain_unlocks_briefing(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    early = build_briefing_payload("w-queued", dispatch_dir=tmp_path)
    assert early["ready"] is False
    assert early["missing_steps"]
    step_ids = _drain_ids("w-queued", tmp_path)
    briefing = build_briefing_payload("w-queued", dispatch_dir=tmp_path)
    assert briefing["ready"] is True
    assert briefing["missing_steps"] == []
    assert briefing["result_count"] == len(step_ids)
    for step_id in step_ids:
        assert f"done:{step_id}" in str(briefing["briefing_prompt"])


def test_record_unknown_step_raises(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    with pytest.raises(DispatchStoreError):
        record_dispatch_result("w-queued", "exec_missing", dispatch_dir=tmp_path)


def test_load_missing_bundle_raises(tmp_path: Path) -> None:
    with pytest.raises(DispatchStoreError):
        load_dispatch_bundle("w-absent", dispatch_dir=tmp_path)


def test_work_id_traversal_is_neutralized(tmp_path: Path) -> None:
    # Path separators and dots are stripped, so traversal collapses to a
    # plain missing-bundle error instead of escaping the dispatch dir.
    with pytest.raises(DispatchStoreError, match="not found"):
        load_dispatch_bundle("../../etc/passwd", dispatch_dir=tmp_path)


def test_work_id_with_no_safe_chars_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DispatchStoreError, match="empty"):
        load_dispatch_bundle("../..", dispatch_dir=tmp_path)
