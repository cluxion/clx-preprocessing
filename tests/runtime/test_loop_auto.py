"""Tests for autonomous /loopAuto queue drain."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cluxion_runtime.cli import main
from cluxion_runtime.core.dispatch_store import persist_dispatch_bundle
from cluxion_runtime.core.harness import build_harness_plan
from cluxion_runtime.core.loop_auto import (
    HermesSegmentResult,
    LoopAutoOptions,
    loop_auto_enabled,
    run_loop_auto,
    should_auto_loop_plan,
    strip_loop_auto_directive,
)
from cluxion_runtime.core.plan_codec import plan_to_dict
from cluxion_runtime.core.types import AgentSurface, ResourceSnapshot, WorkItem

if TYPE_CHECKING:
    from pathlib import Path

_SNAPSHOT = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=40_000, swap_used_mb=0, cpu_percent=20.0)


@pytest.fixture
def queued_plan():
    prompt = "\n".join(f"REQ-{idx}: implement work item and record evidence token {idx}." for idx in range(1500))
    assert len(prompt) > 72_000
    item = WorkItem(
        "w-loop-auto",
        prompt,
        surface=AgentSurface.HERMES,
        metadata={"clarification_answers": "implement every REQ line in order"},
    )
    return build_harness_plan(item, snapshot=_SNAPSHOT)


def test_strip_loop_auto_directive_removes_prefix() -> None:
    cleaned, had = strip_loop_auto_directive("/loopAuto refactor the auth module")
    assert had is True
    assert cleaned == "refactor the auth module"


def test_strip_loop_auto_directive_case_insensitive() -> None:
    cleaned, had = strip_loop_auto_directive("/LOOPAUTO: run tests")
    assert had is True
    assert cleaned == "run tests"


def test_loop_auto_enabled_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLUXION_LOOP_AUTO", raising=False)
    monkeypatch.setenv("CLUXION_LOOP_AUTO_DEFAULT", "0")
    assert loop_auto_enabled() is False
    assert loop_auto_enabled({"loop_auto": True}) is True


def test_should_auto_loop_plan_requires_queue() -> None:
    payload = {"host_execution": {"queue_required": False}}
    assert should_auto_loop_plan(payload) is False
    payload = {"host_execution": {"queue_required": True}}
    assert should_auto_loop_plan(payload, loop_auto=True) is True


def test_run_loop_auto_drains_queue_dry_run(tmp_path: Path, queued_plan, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", str(tmp_path))
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    result = run_loop_auto(LoopAutoOptions(work_id="w-loop-auto", dry_run=True))
    assert result.ok is True
    assert result.status in {"complete", "complete_unmarked"}
    assert result.segments_processed >= 1
    assert result.briefing_answer


def test_plan_cli_auto_loops_queued_plan_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys
    from io import StringIO

    monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", str(tmp_path))
    monkeypatch.setattr("cluxion_runtime.core.harness.collect_resource_snapshot", lambda: _SNAPSHOT)
    prompt = "\n".join(f"REQ-{idx}: implement work item and record evidence token {idx}." for idx in range(1500))
    stdin_payload = json.dumps(
        {
            "prompt": prompt,
            "work_id": "w-cli-loop",
            "clarification_answers": "go",
            "loop_auto": True,
            "loop_auto_dry_run": True,
        }
    )
    monkeypatch.setattr(sys, "stdin", StringIO(stdin_payload))
    code = main(["plan", "--json-stdin", "--surface", "hermes"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["host_execution"]["queue_required"] is True
    assert payload["loop_auto"]["ok"] is True
    assert payload["loop_auto"]["segments_processed"] >= 1


def test_plan_cli_loopauto_prefix_enqueues_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys
    from io import StringIO

    monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", str(tmp_path))
    monkeypatch.setattr("cluxion_runtime.core.harness.collect_resource_snapshot", lambda: _SNAPSHOT)
    prompt = "/loopAuto " + "\n".join(
        f"REQ-{idx}: implement work item and record evidence token {idx}." for idx in range(1500)
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(json.dumps({"prompt": prompt, "work_id": "w-cli-prefix", "clarification_answers": "go"})),
    )

    code = main(["plan", "--json-stdin", "--surface", "codex"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["host_execution"]["queue_required"] is True
    assert "loop_auto" not in payload
    assert "/loopAuto" not in str(payload["item"]["prompt"])
    assert "REQ-0" in str(payload["item"]["prompt"])


def test_run_loop_auto_missing_binary_fails_fast(tmp_path: Path, queued_plan, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", str(tmp_path))
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)

    result = run_loop_auto(LoopAutoOptions(work_id="w-loop-auto", hermes_bin="missing-hermes-for-test"))

    assert result.ok is False
    assert result.status == "preflight_failed"
    assert "hermes binary not found" in result.error
    assert result.segments_processed == 0


def test_run_loop_auto_marker_missing_fails_after_retry_cap(
    tmp_path: Path, queued_plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", str(tmp_path))
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)
    calls = 0

    def runner(_: str) -> HermesSegmentResult:
        nonlocal calls
        calls += 1
        return HermesSegmentResult(stdout="work output without marker", stderr="", returncode=0)

    result = run_loop_auto(
        LoopAutoOptions(work_id="w-loop-auto", segment_runner=runner, max_segment_retries=1)
    )

    assert result.ok is False
    assert result.status == "segment_failed"
    assert result.error.endswith("missing completion marker after 2 attempts")
    assert calls == 2


def test_run_loop_auto_iteration_cap_aborts(tmp_path: Path, queued_plan, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", str(tmp_path))
    persist_dispatch_bundle(queued_plan, dispatch_dir=tmp_path)

    def runner(_: str) -> HermesSegmentResult:
        return HermesSegmentResult(stdout="done\nSEGMENT_COMPLETE\n", stderr="", returncode=0)

    result = run_loop_auto(LoopAutoOptions(work_id="w-loop-auto", segment_runner=runner, max_iterations=0))

    assert result.ok is False
    assert result.status == "iteration_cap_exceeded"
    assert result.segments_processed == 0


def test_run_loop_auto_no_progress_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    from tempfile import TemporaryDirectory

    calls = 0

    def fake_next(_: str) -> dict[str, object]:
        return {
            "ready": True,
            "step": {"step_id": "s1", "segment_id": "seg1", "content": "x", "checksum": "c"},
        }

    def fake_record(*_: object, **__: object) -> dict[str, object]:
        return {"recorded": True}

    def runner(_: str) -> HermesSegmentResult:
        nonlocal calls
        calls += 1
        return HermesSegmentResult(stdout="done\nSEGMENT_COMPLETE\n", stderr="", returncode=0)

    monkeypatch.setattr("cluxion_runtime.core.loop_auto.next_dispatch_step", fake_next)
    monkeypatch.setattr("cluxion_runtime.core.loop_auto.record_dispatch_result", fake_record)

    with TemporaryDirectory() as dispatch_dir:
        monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", dispatch_dir)
        result = run_loop_auto(LoopAutoOptions(work_id="w-stuck", segment_runner=runner, max_iterations=25))

    assert result.ok is False
    assert result.status == "no_progress"
    assert calls == 1


def test_plan_codec_exports_loop_tool(queued_plan) -> None:
    payload = plan_to_dict(queued_plan)
    assert payload["host_execution"]["loop_tool"] == "cluxion_loop_auto"
