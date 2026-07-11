"""Durable ledger recording work-queue state as JSONL events."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from cluxion_runtime.core.ledger_codec import item_from_dict, item_to_dict

if TYPE_CHECKING:
    from pathlib import Path

    from cluxion_runtime.core.types import WorkItem

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX
    _fcntl = None

_OWNER_KEYS = ("owner_cwd", "owner_session_id", "owner_scope")


class WorkStatus(StrEnum):
    """Closed set of work ledger states."""

    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


@dataclass(frozen=True)
class RetryDecision:
    """Whether a failed work item may be retried."""

    retryable: bool
    next_after_epoch: float
    attempt: int
    reason: str


@dataclass(frozen=True)
class LedgerEntry:
    """Latest ledger state for a given work_id."""

    work_id: str
    status: WorkStatus
    attempt: int
    max_attempts: int
    next_after_epoch: float
    reason: str
    item: WorkItem


class LedgerCorruptionError(RuntimeError):
    """Error preventing silent pass-over of corrupted ledger JSONL."""


class LedgerOwnerConflictError(RuntimeError):
    """Cross-owner or mixed ownerless/owned re-enqueue of the same work_id."""


class LedgerInvalidOwnerError(ValueError):
    """Partial or malformed reserved owner metadata on a work item."""


def _owner_from_metadata(metadata: Any) -> dict[str, str] | None:
    """Canonical owner tuple from reserved metadata keys; None if ownerless.

    WorkItem metadata is a dict contract; every non-dict value is malformed.
    """
    if not isinstance(metadata, dict):
        raise LedgerInvalidOwnerError("invalid_queue_owner")
    present = [key for key in _OWNER_KEYS if key in metadata]
    if not present:
        return None
    if any(key not in metadata for key in _OWNER_KEYS):
        raise LedgerInvalidOwnerError("invalid_queue_owner")
    cwd, session_id, scope = (
        metadata["owner_cwd"],
        metadata["owner_session_id"],
        metadata["owner_scope"],
    )
    if not isinstance(cwd, str) or not cwd:
        raise LedgerInvalidOwnerError("invalid_queue_owner")
    if not isinstance(session_id, str):
        raise LedgerInvalidOwnerError("invalid_queue_owner")
    if not isinstance(scope, str) or not scope:
        raise LedgerInvalidOwnerError("invalid_queue_owner")
    return {"cwd": cwd, "session_id": session_id, "scope": scope}


def _owners_conflict(existing: dict[str, str] | None, new: dict[str, str] | None) -> bool:
    if existing is None and new is None:
        return False
    if existing is None or new is None:
        return True
    return existing != new


class DurableWorkLedger:
    """JSONL ledger restoring work state across process restarts."""

    def __init__(self, path: Path, *, sync_on_write: bool = True) -> None:
        self._path = path
        self._sync_on_write = sync_on_write

    def record_enqueued(self, item: WorkItem, *, max_attempts: int = 3) -> LedgerEntry:
        """Register new work into the durable queue with owner isolation."""
        new_owner = _owner_from_metadata(getattr(item, "metadata", None))
        with self._exclusive_lock():
            latest = self._latest_unlocked()
            existing = latest.get(item.work_id)
            if existing is not None:
                existing_owner = _owner_from_metadata(getattr(existing.item, "metadata", None))
                if _owners_conflict(existing_owner, new_owner):
                    raise LedgerOwnerConflictError(
                        f"ledger_owner_conflict work_id={item.work_id}"
                    )
                # Same owner (including both ownerless): legacy append/replay.
            event = self._event(
                item, WorkStatus.QUEUED, attempt=0, max_attempts=max_attempts, reason="queued"
            )
            self._append_unlocked(event)
            return self._entry_from_event(event)

    def record_started(self, work_id: str, *, now: float | None = None) -> LedgerEntry:
        """Record a run-start event."""
        with self._exclusive_lock():
            entry = self._latest_unlocked()[work_id]
            event = self._event(
                entry.item,
                WorkStatus.RUNNING,
                attempt=entry.attempt + 1,
                max_attempts=entry.max_attempts,
                reason="started",
                now=now,
            )
            self._append_unlocked(event)
            return self._entry_from_event(event)

    def record_finished(self, work_id: str, *, reason: str = "succeeded") -> LedgerEntry:
        """Record a successful completion event."""
        with self._exclusive_lock():
            entry = self._latest_unlocked()[work_id]
            event = self._event(
                entry.item,
                WorkStatus.SUCCEEDED,
                attempt=entry.attempt,
                max_attempts=entry.max_attempts,
                reason=reason,
            )
            self._append_unlocked(event)
            return self._entry_from_event(event)

    def record_failed(
        self,
        work_id: str,
        *,
        reason: str,
        retryable: bool = True,
        backoff_base_sec: float = 1.0,
        now: float | None = None,
    ) -> RetryDecision:
        """Record a failure event plus the retry-wait or dead state."""
        current = time.time() if now is None else now
        with self._exclusive_lock():
            entry = self._latest_unlocked()[work_id]
            can_retry = retryable and entry.attempt < entry.max_attempts
            delay = backoff_base_sec * (2 ** max(0, entry.attempt - 1))
            status = WorkStatus.RETRY_WAIT if can_retry else WorkStatus.DEAD
            next_after = current + delay if can_retry else 0.0
            event = self._event(
                entry.item,
                status,
                attempt=entry.attempt,
                max_attempts=entry.max_attempts,
                next_after_epoch=next_after,
                reason=reason,
                now=current,
            )
            self._append_unlocked(event)
            return RetryDecision(can_retry, next_after, entry.attempt, reason)

    def latest(self) -> dict[str, LedgerEntry]:
        """Fold the whole JSONL into the latest state per work_id (shared lock)."""
        with self._shared_lock():
            return self._latest_unlocked()

    def ready_to_retry(self, *, now: float | None = None) -> tuple[LedgerEntry, ...]:
        """Return only work whose retry time has arrived, in priority order."""
        current = time.time() if now is None else now
        ready = [
            entry
            for entry in self.latest().values()
            if entry.status == WorkStatus.RETRY_WAIT and entry.next_after_epoch <= current
        ]
        return tuple(sorted(ready, key=lambda entry: (int(entry.item.priority), entry.work_id)))

    def _latest_unlocked(self) -> dict[str, LedgerEntry]:
        """Fold JSONL without acquiring a lock (caller must hold flock)."""
        states: dict[str, LedgerEntry] = {}
        if not self._path.exists():
            return states
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                entry = self._entry_from_event(event)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise LedgerCorruptionError(f"ledger line {number} is invalid: {exc}") from exc
            states[entry.work_id] = entry
        return states

    def _append_unlocked(self, event: dict[str, object]) -> None:
        """Append+flush+fsync without acquiring a lock (caller must hold exclusive)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            if self._sync_on_write:
                handle.flush()
                os.fsync(handle.fileno())

    @contextmanager
    def _flock(self, exclusive: bool) -> Iterator[None]:
        """POSIX fcntl lock; no-op off POSIX. No nested flock — unlocked helpers only."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if _fcntl is None:
            yield
            return
        lock_path = self._path.parent / f".{self._path.name}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with os.fdopen(fd, "r+b") as lock_file:
                mode = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
                _fcntl.flock(lock_file.fileno(), mode)
                try:
                    yield
                finally:
                    _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
        except Exception:
            # fdopen owns fd on success; only close on open/fstat failure path
            # before fdopen — here fdopen succeeded so no double-close.
            raise

    def _exclusive_lock(self) -> Iterator[None]:
        return self._flock(exclusive=True)

    def _shared_lock(self) -> Iterator[None]:
        return self._flock(exclusive=False)

    def _event(
        self,
        item: WorkItem,
        status: WorkStatus,
        *,
        attempt: int,
        max_attempts: int,
        reason: str,
        next_after_epoch: float = 0.0,
        now: float | None = None,
    ) -> dict[str, object]:
        return {
            "created_at": time.time() if now is None else now,
            "work_id": item.work_id,
            "status": status.value,
            "attempt": max(0, attempt),
            "max_attempts": max(1, max_attempts),
            "next_after_epoch": max(0.0, next_after_epoch),
            "reason": reason,
            "item": item_to_dict(item),
        }

    def _entry_from_event(self, event: dict[str, object]) -> LedgerEntry:
        return LedgerEntry(
            work_id=str(event["work_id"]),
            status=WorkStatus(str(event["status"])),
            attempt=int(event["attempt"]),
            max_attempts=int(event["max_attempts"]),
            next_after_epoch=float(event["next_after_epoch"]),
            reason=str(event["reason"]),
            item=item_from_dict(event["item"]),
        )


__all__ = [
    "DurableWorkLedger",
    "LedgerCorruptionError",
    "LedgerEntry",
    "LedgerInvalidOwnerError",
    "LedgerOwnerConflictError",
    "RetryDecision",
    "WorkStatus",
]
