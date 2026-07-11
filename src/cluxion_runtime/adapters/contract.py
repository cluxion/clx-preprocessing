"""Input contract shared by the Hermes/Codex/Claude/Grok wrappers."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from cluxion_runtime.core.types import AgentSurface, WorkItem, WorkPriority

if TYPE_CHECKING:
    from collections.abc import Mapping

# Reserved WorkItem.metadata keys owned by the adapter; user metadata cannot set them.
_RESERVED_OWNER_KEYS = frozenset({"owner_scope", "owner_cwd", "owner_session_id"})


def work_item_from_adapter_payload(payload: Mapping[str, object], *, default_surface: AgentSurface) -> WorkItem:
    """Convert an external wrapper JSON payload into a WorkItem."""
    raw = payload.get("prompt", "")
    if not isinstance(raw, str):
        raise ValueError("prompt must be a string.")
    prompt = raw.strip()
    if not prompt:
        raise ValueError("prompt must not be empty.")

    session_id = _canonical_session_id(payload.get("session_id"))
    canonical_cwd = _canonical_cwd(payload.get("cwd"))
    explicit_work_id = _explicit_work_id(payload.get("work_id"))

    if session_id is not None:
        owner_scope = f"session:{session_id}"
    elif explicit_work_id is not None:
        owner_scope = f"project:{canonical_cwd}"
    else:
        owner_scope = f"invocation:{uuid.uuid4()}"

    work_id = (
        explicit_work_id
        if explicit_work_id is not None
        else _auto_work_id(owner_scope, canonical_cwd, prompt)
    )

    return WorkItem(
        work_id=work_id,
        prompt=prompt,
        surface=_surface(payload.get("surface"), default_surface),
        priority=_priority(payload.get("priority")),
        model_route=str(payload.get("model_route", "host/default")),
        expected_ram_mb=max(0, int(payload.get("expected_ram_mb", 0))),
        context_tokens=max(0, int(payload.get("context_tokens", 0))),
        metadata=_metadata(
            payload.get("metadata"),
            payload.get("clarification_answers"),
            owner_scope=owner_scope,
            canonical_cwd=canonical_cwd,
            session_id=session_id or "",
        ),
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
                "session_id": {
                    "type": "string",
                    "description": "Canonical full UUID for session-scoped owner isolation of auto work IDs.",
                },
                "priority": {"type": "string", "enum": ["critical", "high", "normal", "low"]},
                "model_route": {"type": "string"},
                "expected_ram_mb": {"type": "integer", "minimum": 0},
                "cwd": {"type": "string"},
                "metadata": {"type": "object"},
            },
        },
    }


def _explicit_work_id(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value  # Preserve explicit work_id byte-for-byte.
    return str(value)


def _auto_work_id(owner_scope: str, canonical_cwd: str, normalized_prompt: str) -> str:
    material = owner_scope.encode("utf-8") + b"\0" + canonical_cwd.encode("utf-8") + b"\0" + normalized_prompt.encode(
        "utf-8"
    )
    digest = hashlib.sha256(material).hexdigest()[:16]
    return f"work-{digest}"


def _canonical_session_id(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("session_id must be a canonical full UUID.")
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = uuid.UUID(raw)
    except ValueError as exc:
        raise ValueError("session_id must be a canonical full UUID.") from exc
    canonical = str(parsed)
    if raw != canonical:
        raise ValueError("session_id must be a canonical full UUID.")
    return canonical


def _canonical_cwd(value: object) -> str:
    raw = str(Path.cwd()) if value is None or value == "" else str(value)
    if "\x00" in raw:
        raise ValueError("cwd must not contain NUL.")
    return str(Path(raw).expanduser().resolve(strict=False))


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


def _metadata(
    value: object,
    clarification_answers: object,
    *,
    owner_scope: str,
    canonical_cwd: str,
    session_id: str,
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if isinstance(value, dict):
        for key, val in value.items():
            name = str(key)
            if name in _RESERVED_OWNER_KEYS:
                continue
            metadata[name] = str(val)
    # Canonical cwd is always stored; reserved owner fields always win over user input.
    metadata["cwd"] = canonical_cwd
    metadata["owner_scope"] = owner_scope
    metadata["owner_cwd"] = canonical_cwd
    metadata["owner_session_id"] = session_id
    # Top-level clarification_answers is the documented way the host answers a
    # clarification gate; without merging it here the gate stays blocked and the
    # work queue never engages, even for very long prompts. A nested
    # metadata.clarification_answers (if present) is not overwritten by an empty top-level value.
    if clarification_answers is not None and str(clarification_answers) != "":
        metadata["clarification_answers"] = str(clarification_answers)
    return metadata


__all__ = ["render_adapter_manifest", "work_item_from_adapter_payload"]
