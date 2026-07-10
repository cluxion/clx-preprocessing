"""Concurrency coverage for the file-backed dispatch store."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cluxion_runtime.core import dispatch_store
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
    from cluxion_runtime.core.types import HarnessPlan

_SNAPSHOT = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=40_000, swap_used_mb=0, cpu_percent=20.0)
_RACE_TIMEOUT_SECONDS = 0.25


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


def _run_concurrently(worker_count: int, worker) -> list[object]:
    start = threading.Barrier(worker_count)

    def run(index: int) -> object:
        start.wait()
        return worker(index)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(run, range(worker_count)))


def _stall_dispatch_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    original_write = dispatch_store._atomic_write_json
    write_barrier = threading.Barrier(2)

    def write(path: Path, payload: dict[str, object]) -> None:
        with suppress(threading.BrokenBarrierError):
            write_barrier.wait(timeout=_RACE_TIMEOUT_SECONDS)
        original_write(path, payload)

    monkeypatch.setattr(dispatch_store, "_atomic_write_json", write)


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


def test_dispatch_store_uses_shared_lock_instead_of_per_bundle_locks(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    payload = next_dispatch_step("w-queued", dispatch_dir=tmp_path)
    record_dispatch_result("w-queued", str(payload["step"]["step_id"]), result="done", dispatch_dir=tmp_path)

    assert not (tmp_path / "w-queued.json.lock").exists()
    assert ((tmp_path / "w-queued.json").stat().st_mode & 0o777) == 0o600
    if dispatch_store._fcntl is not None:
        lock_path = tmp_path / ".dispatch.lock"
        assert lock_path.exists()
        assert (lock_path.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX mode migration only")
def test_dispatch_store_migrates_dir_and_bundle_modes(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    tmp_path.chmod(0o755)
    path = persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    assert path is not None
    path.chmod(0o644)
    load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)
    assert (tmp_path.stat().st_mode & 0o777) == 0o700
    assert (path.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX fail-closed symlink policy")
def test_dispatch_store_rejects_bundle_and_lock_symlinks(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    victim = tmp_path / "outside.json"
    victim.write_text('{"work_id":"victim","steps":[]}', encoding="utf-8")
    victim.chmod(0o644)
    victim_mode = victim.stat().st_mode
    victim_text = victim.read_text(encoding="utf-8")

    planted = tmp_path / "w-queued.json"
    planted.symlink_to(victim)
    with pytest.raises(DispatchStoreError, match=r"symlink|expected"):
        load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)
    assert victim.read_text(encoding="utf-8") == victim_text
    assert victim.stat().st_mode == victim_mode

    if planted.is_symlink() or planted.exists():
        planted.unlink()
    lock_victim = tmp_path / "lock-outside"
    lock_victim.write_text("LOCK", encoding="utf-8")
    lock_victim.chmod(0o644)
    lock_mode = lock_victim.stat().st_mode
    lock_text = lock_victim.read_text(encoding="utf-8")
    (tmp_path / ".dispatch.lock").symlink_to(lock_victim)
    with pytest.raises(DispatchStoreError, match=r"symlink|expected"):
        persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    assert lock_victim.read_text(encoding="utf-8") == lock_text
    assert lock_victim.stat().st_mode == lock_mode


def test_concurrent_next_dispatch_steps_do_not_claim_same_step(
    tmp_path: Path, queued_plan: HarnessPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent queue-next calls must not overwrite each other's running marker."""
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    _stall_dispatch_writes(monkeypatch)

    results = _run_concurrently(2, lambda _index: next_dispatch_step("w-queued", dispatch_dir=tmp_path))
    step_ids = [str(result["step"]["step_id"]) for result in results if result["ready"]]

    assert len(step_ids) == 2
    assert len(set(step_ids)) == 2
    statuses = [step["status"] for step in load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)["steps"]]
    assert statuses.count("running") == 2


def _backdate_running_step(dispatch_dir: Path, work_id: str, step_id: str) -> None:
    path = dispatch_dir / f"{work_id}.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    for step in bundle["steps"]:
        if step["step_id"] == step_id:
            step["updated_at"] = time.time() - dispatch_store.RUNNING_LEASE_SECONDS - 1
    path.write_text(json.dumps(bundle), encoding="utf-8")


def test_stale_running_step_is_reclaimed(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    """A step orphaned in 'running' (killed worker) is claimable again after the lease."""
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    step_id = str(next_dispatch_step("w-queued", dispatch_dir=tmp_path)["step"]["step_id"])
    _backdate_running_step(tmp_path, "w-queued", step_id)

    reclaimed = next_dispatch_step("w-queued", dispatch_dir=tmp_path)

    assert reclaimed["ready"] is True
    assert str(reclaimed["step"]["step_id"]) == step_id
    record_dispatch_result("w-queued", step_id, result="done after reclaim", dispatch_dir=tmp_path)
    steps = {str(step["step_id"]): step for step in load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)["steps"]}
    assert steps[step_id]["status"] == "succeeded"


def test_fresh_running_step_is_not_reclaimed(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    first = str(next_dispatch_step("w-queued", dispatch_dir=tmp_path)["step"]["step_id"])
    second = str(next_dispatch_step("w-queued", dispatch_dir=tmp_path)["step"]["step_id"])
    assert second != first


def test_record_retryable_failure_requeues_step(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    step_id = str(next_dispatch_step("w-queued", dispatch_dir=tmp_path)["step"]["step_id"])

    recorded = record_dispatch_result(
        "w-queued", step_id, error="worker crashed", succeeded=False, retryable=True, dispatch_dir=tmp_path
    )

    assert recorded["ok"] is True
    assert recorded["status"] == "retry_wait"
    assert recorded["synthesis_ready"] is False
    retaken = next_dispatch_step("w-queued", dispatch_dir=tmp_path)
    assert str(retaken["step"]["step_id"]) == step_id


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


def test_concurrent_record_dispatch_results_preserve_both_updates(
    tmp_path: Path, queued_plan: HarnessPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent queue-record calls for different steps must both persist."""
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    step_ids = [
        str(next_dispatch_step("w-queued", dispatch_dir=tmp_path)["step"]["step_id"]),
        str(next_dispatch_step("w-queued", dispatch_dir=tmp_path)["step"]["step_id"]),
    ]
    _stall_dispatch_writes(monkeypatch)

    _run_concurrently(
        2,
        lambda index: record_dispatch_result(
            "w-queued",
            step_ids[index],
            result=f"done:{index}",
            dispatch_dir=tmp_path,
        ),
    )

    steps = {str(step["step_id"]): step for step in load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)["steps"]}
    assert steps[step_ids[0]]["result"] == "done:0"
    assert steps[step_ids[1]]["result"] == "done:1"


def test_concurrent_record_same_step_is_idempotent_for_same_result(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    step_id = str(next_dispatch_step("w-queued", dispatch_dir=tmp_path)["step"]["step_id"])

    results = _run_concurrently(
        5,
        lambda _index: record_dispatch_result(
            "w-queued",
            step_id,
            result="same result",
            dispatch_dir=tmp_path,
        ),
    )

    assert all(isinstance(result, dict) and result["recorded"] is True for result in results)
    assert sum(1 for result in results if isinstance(result, dict) and result.get("idempotent")) == 4
    steps = {str(step["step_id"]): step for step in load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)["steps"]}
    assert steps[step_id]["result"] == "same result"


def test_concurrent_record_same_step_conflicts_for_different_result(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    step_id = str(next_dispatch_step("w-queued", dispatch_dir=tmp_path)["step"]["step_id"])

    results = _run_concurrently(
        5,
        lambda index: record_dispatch_result(
            "w-queued",
            step_id,
            result=f"different:{index}",
            dispatch_dir=tmp_path,
        ),
    )

    successes = [result for result in results if isinstance(result, dict) and result.get("recorded")]
    conflicts = [result for result in results if isinstance(result, dict) and result.get("ok") is False]
    assert len(successes) == 1
    assert len(conflicts) == 4
    steps = load_dispatch_bundle("w-queued", dispatch_dir=tmp_path)["steps"]
    stored_result = str(next(step["result"] for step in steps if step["step_id"] == step_id))
    assert all(conflict["error"] == "step_already_recorded" for conflict in conflicts)
    assert all(conflict["stored_result"] == stored_result for conflict in conflicts)


def test_record_unknown_step_raises(tmp_path: Path, queued_plan: HarnessPlan) -> None:
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    with pytest.raises(DispatchStoreError):
        record_dispatch_result("w-queued", "exec_missing", dispatch_dir=tmp_path)


def test_load_missing_bundle_raises(tmp_path: Path) -> None:
    with pytest.raises(DispatchStoreError):
        load_dispatch_bundle("w-absent", dispatch_dir=tmp_path)


def test_corrupt_bundle_reports_found_vs_expected(tmp_path: Path) -> None:
    (tmp_path / "w-bad.json").write_text("[]", encoding="utf-8")

    with pytest.raises(DispatchStoreError, match=r"expected object, found array"):
        load_dispatch_bundle("w-bad", dispatch_dir=tmp_path)


def test_bundle_without_steps_reports_found_vs_expected(tmp_path: Path) -> None:
    (tmp_path / "w-bad.json").write_text('{"steps": "nope"}', encoding="utf-8")

    with pytest.raises(DispatchStoreError, match=r"expected steps array, found str"):
        next_dispatch_step("w-bad", dispatch_dir=tmp_path)


def test_atomic_write_cleans_tempfile_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(dispatch_store.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        dispatch_store._atomic_write_json(tmp_path / "bundle.json", {"ok": True})

    assert not list(tmp_path.glob("tmp*"))


def test_work_id_traversal_is_rejected_as_invalid(tmp_path: Path) -> None:
    # Sanitized path differs from the original — reject; never alias to etcpasswd.
    with pytest.raises(DispatchStoreError, match="invalid"):
        load_dispatch_bundle("../../etc/passwd", dispatch_dir=tmp_path)
    assert not (tmp_path / "etcpasswd.json").exists()


def test_work_id_with_no_safe_chars_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DispatchStoreError, match="empty"):
        load_dispatch_bundle("../..", dispatch_dir=tmp_path)


def test_work_id_alias_vic_tim_does_not_touch_victim(tmp_path: Path) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text(
        json.dumps({"work_id": "victim", "steps": []}),
        encoding="utf-8",
    )
    with pytest.raises(DispatchStoreError, match="invalid"):
        load_dispatch_bundle("vic!tim", dispatch_dir=tmp_path)
    with pytest.raises(DispatchStoreError, match="invalid"):
        next_dispatch_step("vic!tim", dispatch_dir=tmp_path)
    with pytest.raises(DispatchStoreError, match="invalid"):
        record_dispatch_result("vic!tim", "s1", dispatch_dir=tmp_path)
    with pytest.raises(DispatchStoreError, match="invalid"):
        build_briefing_payload("vic!tim", dispatch_dir=tmp_path)
    assert victim.read_text(encoding="utf-8")


def test_work_id_unicode_alphanumeric_remains_valid(tmp_path: Path) -> None:
    work_id = "작업-테스트1"
    path = tmp_path / f"{work_id}.json"
    path.write_text(
        json.dumps(
            {
                "work_id": work_id,
                "steps": [
                    {
                        "step_id": "s1",
                        "segment_id": "g1",
                        "checksum": "c1",
                        "token_estimate": 1,
                        "content": "x",
                        "status": "queued",
                        "result": "",
                        "error": "",
                        "updated_at": time.time(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bundle = load_dispatch_bundle(work_id, dispatch_dir=tmp_path)
    assert bundle["work_id"] == work_id
    step = next_dispatch_step(work_id, dispatch_dir=tmp_path)
    assert step["ready"] is True
    assert path.exists()


from cluxion_runtime.core.types import ResourceSnapshot
from cluxion_runtime.resources.queue_bridge import resolve_backend


def test_queue_backend_label_matches_resolve_backend():
    item = WorkItem("w-test", "short prompt", surface=AgentSurface.HERMES)
    plan = build_harness_plan(item)
    assert plan.queue_backend == resolve_backend()
