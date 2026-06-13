"""External agent adapter contract and cluxion-runtime CLI tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cluxion_runtime.adapters import render_adapter_manifest, work_item_from_adapter_payload
from cluxion_runtime.cli import main
from cluxion_runtime.core import AgentSurface, WorkPriority

if TYPE_CHECKING:
    import pytest


def test_adapter_payload_builds_work_item_with_metadata() -> None:
    """Wrapper JSON payloads convert into WorkItem reliably."""
    item = work_item_from_adapter_payload(
        {
            "prompt": "로컬 모델 서버를 점검해줘",
            "priority": "high",
            "model_route": "vllm-mlx/mlx-community/Qwen",
            "cwd": "/tmp/project",
        },
        default_surface=AgentSurface.HERMES,
    )

    assert item.surface == AgentSurface.HERMES
    assert item.priority == WorkPriority.HIGH
    assert item.metadata["cwd"] == "/tmp/project"
    assert item.work_id.startswith("work-")


def test_adapter_manifest_points_to_runtime_cli() -> None:
    """Every official adapter manifest uses the cluxion-runtime plan command."""
    manifest = render_adapter_manifest(AgentSurface.CLAUDE)

    assert manifest["surface"] == "claude"
    assert manifest["command"] == ["cluxion-runtime", "plan", "--json-stdin", "--surface", "claude"]
    assert "input_schema" in manifest


def test_cluxion_runtime_plan_cli_outputs_harness_plan(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI prints harness plan JSON external wrappers can parse directly."""
    code = main(
        [
            "plan",
            "--surface",
            "hermes",
            "--prompt",
            "테스트를 실행해줘",
            "--cwd",
            "/tmp/project",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["item"]["surface"] == "hermes"
    assert payload["resource"]["work_kind"] == "generic"
    assert payload["runtime"]["kind"] == "host_managed"
    assert payload["runtime"]["command"] == []
    assert payload["preprocessing"]["mode"] == "standard"
    assert payload["preprocessing"]["preprocess_required"] is True
    assert payload["preprocessing"]["answer_policy"]["unknown_behavior"] == "say_unknown_if_insufficient_context"
    assert payload["preprocessing"]["answer_policy"]["verification_required"] is True
    assert "tie_claims_to_file_diff_or_command_output" in payload["preprocessing"]["answer_policy"]["required_checks"]


def test_cluxion_runtime_plan_cli_marks_simple_answers(capsys: pytest.CaptureFixture[str]) -> None:
    """Short generic queries are marked so the external adapter can answer immediately."""
    code = main(
        [
            "plan",
            "--surface",
            "hermes",
            "--prompt",
            "이거 가능해?",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["preprocessing"]["mode"] == "simple_answer"
    assert payload["preprocessing"]["preprocess_required"] is False
    assert payload["resource"]["mode"] == "simple_answer"
    assert payload["resource"]["work_kind"] == "simple_answer"
    assert payload["preprocessing"]["segments"] == []
    assert "resource_snapshot_skipped" in payload["resource"]["reason_codes"]
    assert payload["preprocessing"]["answer_policy"]["verification_required"] is False


def test_cluxion_runtime_plan_cli_marks_verification_answers(capsys: pytest.CaptureFixture[str]) -> None:
    """Short latest/current/source questions stay on the fast path but carry a verification contract."""
    code = main(
        [
            "plan",
            "--surface",
            "hermes",
            "--prompt",
            "PyPI hermes-cluxion 최신 버전 확인해줘.",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    answer_policy = payload["preprocessing"]["answer_policy"]

    assert code == 0
    assert payload["preprocessing"]["mode"] == "verification_answer"
    assert payload["preprocessing"]["preprocess_required"] is False
    assert payload["resource"]["mode"] == "verification_answer"
    assert answer_policy["verification_required"] is True
    assert answer_policy["citation_required"] is True
    assert answer_policy["response_contract"] == "verify_and_cite_before_answer"
    assert "verify_current_or_recent_fact" in answer_policy["required_checks"]
    assert "cite_external_source_or_document" in answer_policy["required_checks"]


def test_queued_plan_uses_dispatch_store_without_full_prompt_in_plan(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Long input is processed sequentially via queue tools, not inlined into the initial plan output."""
    monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", str(tmp_path / "dispatch"))
    prompt = "\n".join(f"REQ-{idx}: 배포 전 검증 항목을 상세히 분석해줘." for idx in range(4_000))

    code = main(
        [
            "plan",
            "--surface",
            "hermes",
            "--work-id",
            "work-queue-test",
            "--prompt",
            prompt,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["preprocessing"]["mode"] == "queued"
    assert payload["item"]["prompt_redacted"] is True
    assert payload["item"]["original_prompt_stored"] is True
    assert payload["item"]["prompt"].startswith("[cluxion_queue_required]")
    assert len(payload["item"]["prompt"]) < len(prompt) // 10
    assert payload["host_execution"]["strategy"] == "durable_segment_queue"
    assert payload["host_execution"]["next_tool"] == "cluxion_queue_next"
    assert payload["dispatch_store"]["stored"] is True

    assert main(["queue-next", "--work-id", "work-queue-test"]) == 0
    next_payload = json.loads(capsys.readouterr().out)
    step = next_payload["step"]

    assert next_payload["ready"] is True
    assert step["step_id"].startswith("exec_seg_")
    assert "REQ-0" in step["content"]

    assert (
        main(
            [
                "queue-record",
                "--work-id",
                "work-queue-test",
                "--step-id",
                step["step_id"],
                "--result",
                "segment grounded result",
            ]
        )
        == 0
    )
    recorded = json.loads(capsys.readouterr().out)

    assert recorded["recorded"] is True
    assert recorded["status"] == "succeeded"


def test_queue_brief_waits_until_all_segments_are_recorded(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The final briefing becomes ready only after every segment result is recorded."""
    monkeypatch.setenv("CLUXION_PREPROCESS_DISPATCH_DIR", str(tmp_path / "dispatch"))
    prompt = "\n".join(f"REQ-{idx}: 보안 검증 결과를 정리해줘." for idx in range(3_500))

    assert (
        main(
            [
                "plan",
                "--surface",
                "hermes",
                "--work-id",
                "work-brief-test",
                "--prompt",
                prompt,
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["queue-brief", "--work-id", "work-brief-test"]) == 0
    pending = json.loads(capsys.readouterr().out)

    assert pending["ready"] is False
    assert pending["missing_steps"]


def test_serve_local_dry_run_outputs_openai_compatible_endpoint(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The local model server prepare command prints endpoint info for the host to override."""
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("CLUXION_PREPROCESS_RUNTIME_VENV", str(runtime))
    monkeypatch.setattr("shutil.which", lambda command: "/usr/local/bin/uv" if command == "uv" else None)
    code = main(
        [
            "serve-local",
            "--model",
            "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
            "--host",
            "127.0.0.1",
            "--port",
            "23003",
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["provider"] == "openai-compatible"
    assert payload["runtime"] == "vllm_mlx"
    assert payload["base_url"] == "http://127.0.0.1:23003/v1"
    assert payload["command"][:3] == [
        str(runtime / "bin" / "vllm-mlx"),
        "serve",
        "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
    ]
