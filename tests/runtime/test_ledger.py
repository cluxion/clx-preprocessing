from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cluxion_runtime.core.ledger import DurableWorkLedger, LedgerCorruptionError, WorkStatus
from cluxion_runtime.core.ledger_codec import item_from_dict, item_to_dict
from cluxion_runtime.core.types import AgentSurface, WorkItem, WorkPriority

if TYPE_CHECKING:
    from pathlib import Path


def _item(work_id: str = "w-1", priority: WorkPriority = WorkPriority.NORMAL) -> WorkItem:
    return WorkItem(
        work_id,
        "fix the parser",
        surface=AgentSurface.HERMES,
        priority=priority,
        metadata={"cwd": "/tmp/project"},
    )


def _ledger(tmp_path: Path) -> DurableWorkLedger:
    return DurableWorkLedger(tmp_path / "ledger.jsonl", sync_on_write=False)


def test_enqueued_entry_is_latest_state(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_item())
    entry = ledger.latest()["w-1"]
    assert entry.status == WorkStatus.QUEUED
    assert entry.attempt == 0
    assert entry.item == _item()


def test_started_increments_attempt(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_item())
    entry = ledger.record_started("w-1")
    assert entry.status == WorkStatus.RUNNING
    assert entry.attempt == 1


def test_failed_retryable_uses_exponential_backoff(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_item())
    ledger.record_started("w-1", now=100.0)
    first = ledger.record_failed("w-1", reason="boom", backoff_base_sec=2.0, now=100.0)
    assert first.retryable is True
    assert first.next_after_epoch == 102.0  # attempt 1 -> base * 2^0
    ledger.record_started("w-1", now=200.0)
    second = ledger.record_failed("w-1", reason="boom", backoff_base_sec=2.0, now=200.0)
    assert second.next_after_epoch == 204.0  # attempt 2 -> base * 2^1
    assert ledger.latest()["w-1"].status == WorkStatus.RETRY_WAIT


def test_failed_beyond_max_attempts_is_dead(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_item(), max_attempts=1)
    ledger.record_started("w-1")
    decision = ledger.record_failed("w-1", reason="boom", now=100.0)
    assert decision.retryable is False
    assert ledger.latest()["w-1"].status == WorkStatus.DEAD


def test_failed_non_retryable_is_dead_immediately(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_item())
    ledger.record_started("w-1")
    decision = ledger.record_failed("w-1", reason="fatal", retryable=False, now=100.0)
    assert decision.retryable is False
    assert decision.next_after_epoch == 0.0
    assert ledger.latest()["w-1"].status == WorkStatus.DEAD


def test_finished_marks_succeeded(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_item())
    ledger.record_started("w-1")
    entry = ledger.record_finished("w-1")
    assert entry.status == WorkStatus.SUCCEEDED
    assert ledger.latest()["w-1"].status == WorkStatus.SUCCEEDED


def test_ready_to_retry_orders_by_priority_then_id(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for work_id, priority in (
        ("w-normal", WorkPriority.NORMAL),
        ("w-high", WorkPriority.HIGH),
        ("w-future", WorkPriority.CRITICAL),
    ):
        ledger.record_enqueued(_item(work_id, priority))
        ledger.record_started(work_id, now=100.0)
    ledger.record_failed("w-normal", reason="boom", backoff_base_sec=1.0, now=100.0)
    ledger.record_failed("w-high", reason="boom", backoff_base_sec=1.0, now=100.0)
    # w-future's retry window has not opened yet, so it must be excluded.
    ledger.record_failed("w-future", reason="boom", backoff_base_sec=10_000.0, now=100.0)
    ready = ledger.ready_to_retry(now=200.0)
    assert [entry.work_id for entry in ready] == ["w-high", "w-normal"]


def test_state_survives_process_restart(tmp_path: Path) -> None:
    first = _ledger(tmp_path)
    first.record_enqueued(_item())
    first.record_started("w-1")
    # A fresh ledger instance over the same file must rebuild identical state.
    reopened = _ledger(tmp_path)
    entry = reopened.latest()["w-1"]
    assert entry.status == WorkStatus.RUNNING
    assert entry.attempt == 1


def test_corrupt_line_fails_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_item())
    path = tmp_path / "ledger.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        ledger.latest()


def test_started_unknown_work_id_raises(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(KeyError):
        ledger.record_started("nope")


def test_item_codec_roundtrip() -> None:
    item = _item("w-codec", WorkPriority.HIGH)
    assert item_from_dict(item_to_dict(item)) == item


def test_item_codec_rejects_non_object_payload() -> None:
    with pytest.raises(TypeError):
        item_from_dict([1, 2, 3])


def _owned_item(
    work_id: str = "w-1",
    *,
    prompt: str = "fix the parser",
    cwd: str = "/tmp/project",
    scope: str = "project:/tmp/project",
    session_id: str = "",
    extra: dict | None = None,
) -> WorkItem:
    metadata = {
        "cwd": cwd,
        "owner_cwd": cwd,
        "owner_session_id": session_id,
        "owner_scope": scope,
    }
    if extra:
        metadata.update(extra)
    return WorkItem(
        work_id,
        prompt,
        surface=AgentSurface.HERMES,
        priority=WorkPriority.NORMAL,
        metadata=metadata,
    )


def test_ledger_cross_owner_raises_and_preserves_bytes(tmp_path: Path) -> None:
    from cluxion_runtime.core.ledger import LedgerOwnerConflictError

    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_owned_item(cwd="/tmp/a", scope="project:/tmp/a"))
    path = tmp_path / "ledger.jsonl"
    before = path.read_bytes()

    with pytest.raises(LedgerOwnerConflictError):
        ledger.record_enqueued(_owned_item(cwd="/tmp/b", scope="project:/tmp/b", prompt="other"))

    assert path.read_bytes() == before


def test_ledger_exactly_one_side_ownerless_raises(tmp_path: Path) -> None:
    from cluxion_runtime.core.ledger import LedgerOwnerConflictError

    ledger = _ledger(tmp_path)
    ledger.record_enqueued(_owned_item())
    path = tmp_path / "ledger.jsonl"
    before = path.read_bytes()

    with pytest.raises(LedgerOwnerConflictError):
        ledger.record_enqueued(_item("w-1"))  # no reserved owner keys

    assert path.read_bytes() == before

    ledger2 = DurableWorkLedger(tmp_path / "ledger2.jsonl", sync_on_write=False)
    ledger2.record_enqueued(_item("w-2"))
    path2 = tmp_path / "ledger2.jsonl"
    before2 = path2.read_bytes()
    with pytest.raises(LedgerOwnerConflictError):
        ledger2.record_enqueued(_owned_item("w-2"))
    assert path2.read_bytes() == before2


def test_ledger_same_owner_and_ownerless_replay_appends(tmp_path: Path) -> None:
    """Existing same-owner/ownerless replay stays append-only (no idempotent short-circuit)."""
    ledger = _ledger(tmp_path)
    path = tmp_path / "ledger.jsonl"

    item = _owned_item(prompt="v1")
    ledger.record_enqueued(item)
    line_count = path.read_text(encoding="utf-8").count("\n")
    again = ledger.record_enqueued(item)
    assert again.item.prompt == "v1"
    assert path.read_text(encoding="utf-8").count("\n") == line_count + 1

    entry = ledger.record_enqueued(_owned_item(prompt="v2"))
    assert entry.item.prompt == "v2"
    assert path.read_text(encoding="utf-8").count("\n") == line_count + 2
    assert ledger.latest()["w-1"].item.prompt == "v2"

    # Both ownerless: legacy append, not conflict.
    ledger2 = DurableWorkLedger(tmp_path / "ledger2.jsonl", sync_on_write=False)
    path2 = tmp_path / "ledger2.jsonl"
    ledger2.record_enqueued(_item("w-ol"))
    lines2 = path2.read_text(encoding="utf-8").count("\n")
    ledger2.record_enqueued(_item("w-ol"))
    assert path2.read_text(encoding="utf-8").count("\n") == lines2 + 1


def test_ledger_partial_or_malformed_owner_raises_invalid(tmp_path: Path) -> None:
    from cluxion_runtime.core.ledger import LedgerInvalidOwnerError

    ledger = _ledger(tmp_path)
    path = tmp_path / "ledger.jsonl"

    partial = WorkItem(
        "w-partial",
        "x",
        surface=AgentSurface.HERMES,
        metadata={"owner_cwd": "/tmp/a", "owner_scope": "project:/tmp/a"},
    )
    with pytest.raises(LedgerInvalidOwnerError):
        ledger.record_enqueued(partial)
    assert not path.exists() or path.read_bytes() == b""

    # Existing bytes must stay intact when a later enqueue is malformed.
    ledger.record_enqueued(_owned_item("w-keep"))
    before = path.read_bytes()
    bad = WorkItem(
        "w-keep",
        "mutate",
        surface=AgentSurface.HERMES,
        metadata={"owner_cwd": "", "owner_session_id": "", "owner_scope": "project:/tmp/a"},
    )
    with pytest.raises(LedgerInvalidOwnerError):
        ledger.record_enqueued(bad)
    assert path.read_bytes() == before


def test_ledger_none_and_non_dict_metadata_owner_parser(tmp_path: Path) -> None:
    """WorkItem metadata is dict-only; None/non-dict values fail before append."""
    from cluxion_runtime.core.ledger import LedgerInvalidOwnerError, _owner_from_metadata

    with pytest.raises(LedgerInvalidOwnerError):
        _owner_from_metadata(None)
    with pytest.raises(LedgerInvalidOwnerError):
        _owner_from_metadata(["not", "a", "dict"])
    with pytest.raises(LedgerInvalidOwnerError):
        _owner_from_metadata("string")

    # WorkItem runtime accepts non-dict metadata; enqueue gate must reject before append.
    ledger = _ledger(tmp_path)
    path = tmp_path / "ledger.jsonl"
    ledger.record_enqueued(_item("w-keep"))
    before = path.read_bytes()
    with pytest.raises(LedgerInvalidOwnerError):
        ledger.record_enqueued(
            WorkItem(
                "w-none",
                "x",
                surface=AgentSurface.HERMES,
                metadata=None,  # type: ignore[arg-type]
            )
        )
    with pytest.raises(LedgerInvalidOwnerError):
        ledger.record_enqueued(
            WorkItem(
                "w-list",
                "x",
                surface=AgentSurface.HERMES,
                metadata=["not", "a", "dict"],  # type: ignore[arg-type]
            )
        )
    with pytest.raises(LedgerInvalidOwnerError):
        ledger.record_enqueued(
            WorkItem(
                "w-keep",
                "mutate",
                surface=AgentSurface.HERMES,
                metadata=("tuple",),  # type: ignore[arg-type]
            )
        )
    assert path.read_bytes() == before


def _ledger_owner_race_worker(path_str: str, cwd: str, out_str: str) -> None:
    """Module-level worker for multiprocessing spawn (must be picklable)."""
    import json
    from pathlib import Path

    from cluxion_runtime.core.ledger import (
        DurableWorkLedger,
        LedgerOwnerConflictError,
    )
    from cluxion_runtime.core.types import AgentSurface, WorkItem, WorkPriority

    path = Path(path_str)
    item = WorkItem(
        "w-race",
        cwd,
        surface=AgentSurface.HERMES,
        priority=WorkPriority.NORMAL,
        metadata={
            "cwd": cwd,
            "owner_cwd": cwd,
            "owner_session_id": "",
            "owner_scope": f"project:{cwd}",
        },
    )
    ledger = DurableWorkLedger(path, sync_on_write=True)
    try:
        entry = ledger.record_enqueued(item)
        Path(out_str).write_text(
            json.dumps({"status": "ok", "owner": entry.item.metadata["owner_cwd"]}),
            encoding="utf-8",
        )
    except LedgerOwnerConflictError as exc:
        Path(out_str).write_text(
            json.dumps({"status": "conflict", "error": str(exc)}),
            encoding="utf-8",
        )


def test_ledger_concurrent_ab_owner_writes_serialize(tmp_path: Path) -> None:
    """fcntl lock serializes cross-process record_enqueued: 1 success + 1 conflict."""
    import json
    import multiprocessing as mp

    path = tmp_path / "ledger.jsonl"
    out_a = tmp_path / "out-a.json"
    out_b = tmp_path / "out-b.json"
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_ledger_owner_race_worker, args=(str(path), "/tmp/a", str(out_a))),
        ctx.Process(target=_ledger_owner_race_worker, args=(str(path), "/tmp/b", str(out_b))),
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=15)
        assert proc.exitcode == 0

    results = [
        json.loads(out_a.read_text(encoding="utf-8")),
        json.loads(out_b.read_text(encoding="utf-8")),
    ]
    oks = [r for r in results if r["status"] == "ok"]
    conflicts = [r for r in results if r["status"] == "conflict"]
    assert len(oks) == 1
    assert len(conflicts) == 1

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    latest = DurableWorkLedger(path, sync_on_write=False).latest()["w-race"]
    assert latest.item.metadata["owner_cwd"] == oks[0]["owner"]


def _ledger_transition_writer(path_str: str, out_str: str, cycles: int) -> None:
    """Module-level spawn worker: exclusive transition fold under lock."""
    from pathlib import Path

    from cluxion_runtime.core.ledger import DurableWorkLedger
    from cluxion_runtime.core.types import AgentSurface, WorkItem, WorkPriority

    path = Path(path_str)
    ledger = DurableWorkLedger(path, sync_on_write=True)
    item = WorkItem(
        "w-fold",
        "transition",
        surface=AgentSurface.HERMES,
        priority=WorkPriority.NORMAL,
        metadata={"cwd": "/tmp/fold"},
    )
    ledger.record_enqueued(item)
    for _ in range(cycles):
        ledger.record_started("w-fold")
        ledger.record_finished("w-fold", reason="ok")
        # re-queue for next cycle (same ownerless/owned keys — ownerless here)
        ledger.record_enqueued(item)
    Path(out_str).write_text("ok", encoding="utf-8")


def _ledger_transition_reader(path_str: str, out_str: str, spins: int) -> None:
    """Module-level spawn worker: concurrent shared-lock latest folds."""
    import json
    from pathlib import Path

    from cluxion_runtime.core.ledger import DurableWorkLedger, LedgerCorruptionError, WorkStatus

    path = Path(path_str)
    ledger = DurableWorkLedger(path, sync_on_write=True)
    valid = 0
    corrupt = 0
    for _ in range(spins):
        try:
            states = ledger.latest()
            for entry in states.values():
                assert entry.work_id
                assert entry.status in WorkStatus
                assert entry.attempt >= 0
                assert entry.max_attempts >= 1
            valid += 1
        except LedgerCorruptionError:
            corrupt += 1
    Path(out_str).write_text(json.dumps({"valid": valid, "corrupt": corrupt}), encoding="utf-8")


def test_ledger_concurrent_transition_read_valid_fold(tmp_path: Path) -> None:
    """Real spawn: concurrent transition writes + latest reads never lose/invalid-fold."""
    import json
    import multiprocessing as mp

    path = tmp_path / "ledger.jsonl"
    out_w = tmp_path / "out-writer.txt"
    out_r = tmp_path / "out-reader.json"
    ctx = mp.get_context("spawn")
    writer = ctx.Process(target=_ledger_transition_writer, args=(str(path), str(out_w), 20))
    reader = ctx.Process(target=_ledger_transition_reader, args=(str(path), str(out_r), 80))
    writer.start()
    reader.start()
    writer.join(timeout=30)
    reader.join(timeout=30)
    assert writer.exitcode == 0
    assert reader.exitcode == 0
    assert out_w.read_text(encoding="utf-8") == "ok"
    report = json.loads(out_r.read_text(encoding="utf-8"))
    assert report["corrupt"] == 0
    assert report["valid"] == 80

    # Final fold must be coherent (last writer cycle left QUEUED after re-enqueue).
    final = DurableWorkLedger(path, sync_on_write=False).latest()["w-fold"]
    assert final.status in {
        WorkStatus.QUEUED,
        WorkStatus.RUNNING,
        WorkStatus.SUCCEEDED,
    }
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 1
    # Re-fold every prefix: no lost intermediate state that fails parse mid-file.
    for i in range(1, len(lines) + 1):
        partial = tmp_path / f"partial-{i}.jsonl"
        partial.write_text("\n".join(lines[:i]) + "\n", encoding="utf-8")
        folded = DurableWorkLedger(partial, sync_on_write=False).latest()
        assert "w-fold" in folded
