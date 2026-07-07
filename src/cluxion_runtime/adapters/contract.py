"""Input contract shared by the Hermes/Codex/Claude/Grok wrappers."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from cluxion_runtime.core.types import AgentSurface, WorkItem, WorkPriority

if TYPE_CHECKING:
    from collections.abc import Mapping


def work_item_from_adapter_payload(payload: Mapping[str, object], *, default_surface: AgentSurface) -> WorkItem:
    """Convert an external wrapper JSON payload into a WorkItem."""
    raw = payload.get("prompt", "")
    if not isinstance(raw, str):
        raise ValueError("prompt must be a string.")
    prompt = raw.strip()
    if not prompt:
        raise ValueError("prompt must not be empty.")
    work_id = str(payload.get("work_id", "")) or _stable_work_id(prompt)
    return WorkItem(
        work_id=work_id,
        prompt=prompt,
        surface=_surface(payload.get("surface"), default_surface),
        priority=_priority(payload.get("priority")),
        model_route=str(payload.get("model_route", "host/default")),
        expected_ram_mb=max(0, int(payload.get("expected_ram_mb", 0))),
        context_tokens=max(0, int(payload.get("context_tokens", 0))),
        metadata=_metadata(payload.get("metadata"), payload.get("cwd"), payload.get("clarification_answers")),
    )


def render_adapter_manifest(surface: AgentSurface) -> dict[str, object]:
    """Build the common tool manifest embeddable in each agent product."""
    return {
        "name": "cluxion_harness",
        "surface": surface.value,
        "command": ["cluxion-runtime", "plan", "--json-stdin", "--surface", surface.value],
        "description": "Plan Cluxion preprocessing, work queue, Rust resource admission, and local model route before execution.",
        "input_schema": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string"},
                "work_id": {"type": "string"},
                "priority": {"type": "string", "enum": ["critical", "high", "normal", "low"]},
                "model_route": {"type": "string"},
                "expected_ram_mb": {"type": "integer", "minimum": 0},
                "metadata": {"type": "object"},
            },
        },
    }


def _stable_work_id(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    return f"work-{digest}"


def _surface(value: object, default: AgentSurface) -> AgentSurface:
    if value is None or value == "":
        return default
    return AgentSurface(str(value))


def _priority(value: object) -> WorkPriority:
    if value is None or value == "":
        return WorkPriority.NORMAL
    if isinstance(value, int):
        try:
            return WorkPriority(value)
        except ValueError as exc:
            raise ValueError("priority must be one of critical, high, normal, low") from exc
    try:
        return WorkPriority[str(value).upper()]
    except KeyError as exc:
        raise ValueError("priority must be one of critical, high, normal, low") from exc


def _metadata(value: object, cwd: object, clarification_answers: object = None) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if isinstance(value, dict):
        metadata.update({str(key): str(val) for key, val in value.items()})
    if cwd is not None and cwd != "":
        metadata["cwd"] = str(cwd)
    # Top-level clarification_answers is the documented way the host answers a
    # clarification gate; without merging it here the gate stays blocked and the
    # work queue never engages, even for very long prompts. A nested
    # metadata.clarification_answers (if present) is not overwritten by an empty top-level value.
    if clarification_answers is not None and str(clarification_answers) != "":
        metadata["clarification_answers"] = str(clarification_answers)
    return metadata


__all__ = ["render_adapter_manifest", "work_item_from_adapter_payload"]
