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
