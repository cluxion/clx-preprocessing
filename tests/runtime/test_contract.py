from __future__ import annotations

import pytest

from cluxion_runtime.adapters.contract import render_adapter_manifest, work_item_from_adapter_payload
from cluxion_runtime.core.types import AgentSurface, WorkPriority


def test_prompt_must_be_a_non_empty_string() -> None:
    for payload in ({"prompt": None}, {"prompt": 123}):
        with pytest.raises(ValueError, match=r"prompt must be a string\."):
            work_item_from_adapter_payload(payload, default_surface=AgentSurface.HERMES)

    for payload in ({"prompt": "   "}, {}):
        with pytest.raises(ValueError, match=r"prompt must not be empty\."):
            work_item_from_adapter_payload(payload, default_surface=AgentSurface.HERMES)

    item = work_item_from_adapter_payload({"prompt": "hi"}, default_surface=AgentSurface.HERMES)
    assert item.prompt == "hi"


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


def test_top_level_clarification_answers_reaches_metadata() -> None:
    # Regression: the adapter dropped a top-level clarification_answers field, so
    # the clarification gate stayed blocked and the work queue never engaged even
    # for very long prompts. It must reach WorkItem.metadata.
    item = work_item_from_adapter_payload(
        {"prompt": "x", "clarification_answers": "fix src/app.py in order", "cwd": "/tmp/p"},
        default_surface=AgentSurface.HERMES,
    )
    assert item.metadata["clarification_answers"] == "fix src/app.py in order"
    # An empty top-level value must not clobber a nested metadata answer.
    nested = work_item_from_adapter_payload(
        {"prompt": "x", "metadata": {"clarification_answers": "nested"}, "clarification_answers": ""},
        default_surface=AgentSurface.HERMES,
    )
    assert nested.metadata["clarification_answers"] == "nested"


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
