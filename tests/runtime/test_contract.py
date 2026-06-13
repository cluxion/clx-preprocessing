from __future__ import annotations

import pytest

from cluxion_runtime.adapters.contract import render_adapter_manifest, work_item_from_adapter_payload
from cluxion_runtime.core.types import AgentSurface, WorkPriority


def test_empty_prompt_rejected() -> None:
    with pytest.raises(ValueError, match="prompt"):
        work_item_from_adapter_payload({"prompt": "   "}, default_surface=AgentSurface.HERMES)


def test_missing_work_id_gets_stable_hash_id() -> None:
    first = work_item_from_adapter_payload({"prompt": "fix the bug"}, default_surface=AgentSurface.HERMES)
    second = work_item_from_adapter_payload({"prompt": "fix the bug"}, default_surface=AgentSurface.HERMES)
    assert first.work_id == second.work_id
    assert first.work_id.startswith("work-")
    other = work_item_from_adapter_payload({"prompt": "fix another bug"}, default_surface=AgentSurface.HERMES)
    assert other.work_id != first.work_id


def test_explicit_work_id_preserved() -> None:
    item = work_item_from_adapter_payload({"prompt": "x", "work_id": "w-42"}, default_surface=AgentSurface.CODEX)
    assert item.work_id == "w-42"


def test_priority_accepts_int_string_and_default() -> None:
    base = {"prompt": "x"}
    as_int = work_item_from_adapter_payload({**base, "priority": 1}, default_surface=AgentSurface.HERMES)
    as_str = work_item_from_adapter_payload({**base, "priority": "high"}, default_surface=AgentSurface.HERMES)
    omitted = work_item_from_adapter_payload(base, default_surface=AgentSurface.HERMES)
    assert as_int.priority == WorkPriority.HIGH
    assert as_str.priority == WorkPriority.HIGH
    assert omitted.priority == WorkPriority.NORMAL


def test_surface_defaults_and_overrides() -> None:
    defaulted = work_item_from_adapter_payload({"prompt": "x", "surface": ""}, default_surface=AgentSurface.CLAUDE)
    explicit = work_item_from_adapter_payload({"prompt": "x", "surface": "codex"}, default_surface=AgentSurface.CLAUDE)
    assert defaulted.surface == AgentSurface.CLAUDE
    assert explicit.surface == AgentSurface.CODEX


def test_metadata_merges_cwd() -> None:
    item = work_item_from_adapter_payload(
        {"prompt": "x", "metadata": {"repo": "demo"}, "cwd": "/tmp/project"},
        default_surface=AgentSurface.HERMES,
    )
    assert item.metadata == {"repo": "demo", "cwd": "/tmp/project"}


def test_negative_budgets_clamped_to_zero() -> None:
    item = work_item_from_adapter_payload(
        {"prompt": "x", "expected_ram_mb": -512, "context_tokens": -10},
        default_surface=AgentSurface.HERMES,
    )
    assert item.expected_ram_mb == 0
    assert item.context_tokens == 0


def test_manifest_targets_plan_cli_for_surface() -> None:
    manifest = render_adapter_manifest(AgentSurface.HERMES)
    assert manifest["name"] == "cluxion_harness"
    assert manifest["surface"] == "hermes"
    assert manifest["command"] == ["cluxion-runtime", "plan", "--json-stdin", "--surface", "hermes"]
    assert "prompt" in manifest["input_schema"]["required"]
