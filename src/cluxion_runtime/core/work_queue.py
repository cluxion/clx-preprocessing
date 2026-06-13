"""Priority-based agent work queue."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cluxion_runtime.core.types import WorkItem


@dataclass(frozen=True)
class QueueAdmission:
    """Work queue insertion result."""

    accepted: bool
    reason: str
    evicted_work_id: str = ""


@dataclass(order=True)
class _QueueEntry:
    priority: int
    sequence: int
    item: WorkItem = field(compare=False)


class AgentWorkQueue:
    """Small queue honouring both priority and FIFO order."""

    def __init__(self, max_size: int = 256) -> None:
        if max_size < 1:
            raise ValueError("max_size must be at least 1.")
        self._max_size = max_size
        self._sequence = 0
        self._heap: list[_QueueEntry] = []

    def enqueue(self, item: WorkItem) -> QueueAdmission:
        """Evict only lower-priority work when the queue is full."""
        entry = _QueueEntry(int(item.priority), self._sequence, item)
        self._sequence += 1
        if len(self._heap) < self._max_size:
            heapq.heappush(self._heap, entry)
            return QueueAdmission(True, "queued")
        worst_index = self._worst_index()
        worst = self._heap[worst_index]
        if entry.priority >= worst.priority:
            return QueueAdmission(False, "queue_full_lower_or_equal_priority")
        evicted = worst.item.work_id
        self._heap[worst_index] = entry
        heapq.heapify(self._heap)
        return QueueAdmission(True, "queued_after_eviction", evicted)

    def dequeue(self) -> WorkItem | None:
        """Pop the highest-priority work item."""
        if not self._heap:
            return None
        return heapq.heappop(self._heap).item

    def peek_order(self) -> tuple[str, ...]:
        """Return the planned execution order as work IDs."""
        return tuple(entry.item.work_id for entry in sorted(self._heap))

    def size(self) -> int:
        """Return the number of currently waiting items."""
        return len(self._heap)

    def _worst_index(self) -> int:
        return max(range(len(self._heap)), key=lambda index: (self._heap[index].priority, self._heap[index].sequence))


__all__ = ["AgentWorkQueue", "QueueAdmission"]
