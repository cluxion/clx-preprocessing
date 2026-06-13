"""WorkItem serialization codec used by the durable ledger."""

from __future__ import annotations

from cluxion_runtime.core.types import AgentSurface, WorkItem, WorkPriority


def item_to_dict(item: WorkItem) -> dict[str, object]:
    """Convert a WorkItem into a JSON-safe object."""
    return {
        "work_id": item.work_id,
        "prompt": item.prompt,
        "surface": item.surface.value,
        "priority": int(item.priority),
        "model_route": item.model_route,
        "expected_ram_mb": item.expected_ram_mb,
        "context_tokens": item.context_tokens,
        "metadata": dict(item.metadata),
    }


def item_from_dict(payload: object) -> WorkItem:
    """Restore a WorkItem from a JSON object."""
    if not isinstance(payload, dict):
        raise TypeError("item payload must be an object")
    return WorkItem(
        work_id=str(payload["work_id"]),
        prompt=str(payload["prompt"]),
        surface=AgentSurface(str(payload["surface"])),
        priority=WorkPriority(int(payload["priority"])),
        model_route=str(payload["model_route"]),
        expected_ram_mb=int(payload["expected_ram_mb"]),
        context_tokens=int(payload["context_tokens"]),
        metadata={str(key): str(value) for key, value in dict(payload["metadata"]).items()},
    )


__all__ = ["item_from_dict", "item_to_dict"]
