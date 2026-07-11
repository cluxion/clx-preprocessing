from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from cluxion_runtime.adapters.contract import render_adapter_manifest, work_item_from_adapter_payload
from cluxion_runtime.core.preprocess import _split_segments
from cluxion_runtime.core.types import AgentSurface, WorkPriority

_SID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_SID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _cwd(path: str) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def test_prompt_must_be_a_non_empty_string() -> None:
    for payload in ({"prompt": None}, {"prompt": 123}):
        with pytest.raises(ValueError, match=r"prompt must be a string\."):
            work_item_from_adapter_payload(payload, default_surface=AgentSurface.HERMES)

    for payload in ({"prompt": "   "}, {}):
        with pytest.raises(ValueError, match=r"prompt must not be empty\."):
            work_item_from_adapter_payload(payload, default_surface=AgentSurface.HERMES)

    item = work_item_from_adapter_payload({"prompt": "hi"}, default_surface=AgentSurface.HERMES)
    assert item.prompt == "hi"


def test_missing_work_id_sessionless_auto_ids_differ_per_call() -> None:
    """C111: sessionless auto work_id uses invocation scope — each call is unique."""
    first = work_item_from_adapter_payload({"prompt": "fix the bug"}, default_surface=AgentSurface.HERMES)
    second = work_item_from_adapter_payload({"prompt": "fix the bug"}, default_surface=AgentSurface.HERMES)
    assert first.work_id.startswith("work-")
    assert second.work_id.startswith("work-")
    assert first.work_id != second.work_id
    assert first.metadata["owner_scope"].startswith("invocation:")
    assert second.metadata["owner_scope"].startswith("invocation:")
    assert first.metadata["owner_scope"] != second.metadata["owner_scope"]


def test_auto_work_id_same_prompt_different_projects_differ() -> None:
    prompt = "implement feature X carefully"
    a = work_item_from_adapter_payload(
        {"prompt": prompt, "cwd": "/tmp/project-a", "session_id": _SID_A},
        default_surface=AgentSurface.HERMES,
    )
    b = work_item_from_adapter_payload(
        {"prompt": prompt, "cwd": "/tmp/project-b", "session_id": _SID_A},
        default_surface=AgentSurface.HERMES,
    )
    assert a.work_id != b.work_id
    assert a.work_id.startswith("work-") and b.work_id.startswith("work-")
    assert a.metadata["owner_cwd"] == _cwd("/tmp/project-a")
    assert b.metadata["owner_cwd"] == _cwd("/tmp/project-b")


def test_auto_work_id_same_cwd_two_session_ids_differ() -> None:
    prompt = "shared prompt text"
    cwd = "/tmp/shared-project"
    a = work_item_from_adapter_payload(
        {"prompt": prompt, "cwd": cwd, "session_id": _SID_A},
        default_surface=AgentSurface.HERMES,
    )
    b = work_item_from_adapter_payload(
        {"prompt": prompt, "cwd": cwd, "session_id": _SID_B},
        default_surface=AgentSurface.HERMES,
    )
    assert a.work_id != b.work_id
    assert a.metadata["owner_scope"] == f"session:{_SID_A}"
    assert b.metadata["owner_scope"] == f"session:{_SID_B}"


def test_auto_work_id_same_session_cwd_prompt_is_stable() -> None:
    payload = {
        "prompt": "stable retry prompt",
        "cwd": "/tmp/stable-project",
        "session_id": _SID_A,
    }
    first = work_item_from_adapter_payload(payload, default_surface=AgentSurface.HERMES)
    second = work_item_from_adapter_payload(payload, default_surface=AgentSurface.HERMES)
    assert first.work_id == second.work_id
    assert first.work_id.startswith("work-")
    assert first.metadata["owner_scope"] == f"session:{_SID_A}"
    material = (
        f"session:{_SID_A}".encode()
        + b"\0"
        + _cwd("/tmp/stable-project").encode()
        + b"\0"
        + b"stable retry prompt"
    )
    expected = "work-" + hashlib.sha256(material).hexdigest()[:16]
    assert first.work_id == expected


def test_sessionless_auto_work_ids_differ() -> None:
    payload = {"prompt": "no session prompt", "cwd": "/tmp/solo"}
    ids = {
        work_item_from_adapter_payload(payload, default_surface=AgentSurface.HERMES).work_id for _ in range(5)
    }
    assert len(ids) == 5


def test_explicit_work_id_preserved() -> None:
    item = work_item_from_adapter_payload(
        {"prompt": "x", "work_id": "w-42", "cwd": "/tmp/p"},
        default_surface=AgentSurface.CODEX,
    )
    assert item.work_id == "w-42"
    assert item.metadata["owner_scope"] == f"project:{_cwd('/tmp/p')}"


def test_explicit_work_id_preserved_byte_for_byte() -> None:
    raw = "Work_ID-Exact.Bytes-01"
    item = work_item_from_adapter_payload(
        {"prompt": "x", "work_id": raw, "cwd": "/tmp/p"},
        default_surface=AgentSurface.HERMES,
    )
    assert item.work_id == raw
    assert item.work_id is not None


def test_user_metadata_cannot_override_reserved_owner_fields() -> None:
    item = work_item_from_adapter_payload(
        {
            "prompt": "x",
            "cwd": "/tmp/real",
            "session_id": _SID_A,
            "metadata": {
                "owner_scope": "session:forged",
                "owner_cwd": "/evil",
                "owner_session_id": _SID_B,
                "repo": "demo",
            },
        },
        default_surface=AgentSurface.HERMES,
    )
    assert item.metadata["owner_scope"] == f"session:{_SID_A}"
    assert item.metadata["owner_cwd"] == _cwd("/tmp/real")
    assert item.metadata["owner_session_id"] == _SID_A
    assert item.metadata["repo"] == "demo"


def test_session_id_must_be_canonical_full_uuid() -> None:
    with pytest.raises(ValueError, match=r"session_id"):
        work_item_from_adapter_payload(
            {"prompt": "x", "session_id": "not-a-uuid"},
            default_surface=AgentSurface.HERMES,
        )
    with pytest.raises(ValueError, match=r"session_id"):
        work_item_from_adapter_payload(
            {"prompt": "x", "session_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            default_surface=AgentSurface.HERMES,
        )
    # Non-canonical casing is rejected (must already be canonical str(UUID)).
    with pytest.raises(ValueError, match=r"session_id"):
        work_item_from_adapter_payload(
            {"prompt": "x", "session_id": _SID_A.upper()},
            default_surface=AgentSurface.HERMES,
        )
    ok = work_item_from_adapter_payload(
        {"prompt": "x", "session_id": _SID_A},
        default_surface=AgentSurface.HERMES,
    )
    assert _UUID_RE.match(ok.metadata["owner_session_id"])


def test_cwd_rejects_nul() -> None:
    with pytest.raises(ValueError, match=r"NUL|nul"):
        work_item_from_adapter_payload(
            {"prompt": "x", "cwd": "/tmp/has\x00nul"},
            default_surface=AgentSurface.HERMES,
        )


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
    assert item.metadata["repo"] == "demo"
    assert item.metadata["cwd"] == _cwd("/tmp/project")
    assert item.metadata["owner_cwd"] == _cwd("/tmp/project")
    assert item.metadata["owner_scope"].startswith("invocation:")


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
    assert "session_id" in manifest["input_schema"]["properties"]


def test_recursive_split_segments_have_unique_ids_and_original_offsets() -> None:
    text = "   " + ("가나다라마바사아자차카타파하" * 260)

    segments = _split_segments(text, max_chars=2048, max_tokens=100)

    assert [segment.segment_id for segment in segments] == [f"seg_{index:03d}" for index in range(len(segments))]
    assert [segment.char_start for segment in segments] == sorted(segment.char_start for segment in segments)
    assert segments[0].char_start == 3
    assert segments[-1].char_end == len(text)
    assert all(segment.content == text[segment.char_start : segment.char_end] for segment in segments)
