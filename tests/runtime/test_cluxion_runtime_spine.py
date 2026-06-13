"""Contract tests for the cluxion_runtime harness core."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cluxion_runtime.adapters import default_adapter_specs
from cluxion_runtime.core import (
    AgentSurface,
    AgentWorkQueue,
    WorkItem,
    WorkPriority,
    build_harness_plan,
    classify_intent,
    preprocess_work,
)
from cluxion_runtime.core.types import ResourceSnapshot, RuntimeKind
from cluxion_runtime.models import build_vllm_mlx_profile
from cluxion_runtime.resources import capacity_decision, collect_resource_snapshot, evaluate_pressure

if TYPE_CHECKING:
    import pytest


def test_preprocess_splits_long_work_into_evidence_segments() -> None:
    """Long input reaches the model as segment evidence, never the full original text."""
    prompt = "\n".join(f"REQ-{idx}: 작업 항목을 구현하고 evidence={idx}를 남겨라." for idx in range(30))
    item = WorkItem("w-long", prompt, surface=AgentSurface.HERMES, priority=WorkPriority.HIGH)

    result = preprocess_work(item, max_segment_chars=180, max_segment_tokens=120)

    assert result.split_required is True
    assert len(result.segments) > 1
    assert "[cluxion_queue_required]" in result.normalized_prompt
    assert all(segment.checksum for segment in result.segments)
    assert len(result.evidence) == len(result.segments)
    assert result.effort == "high"
    assert result.mode == "queued"
    assert result.preprocess_required is True
    assert result.answer_policy.unknown_behavior == "say_unknown_if_insufficient_context"


def test_short_general_question_uses_simple_answer_policy() -> None:
    """Short generic queries get only the unknown-safe answer contract, no queue preprocessing."""
    item = WorkItem("w-simple", "Hermes에서는 어떻게 봐?")

    result = preprocess_work(item)

    assert result.mode == "simple_answer"
    assert result.preprocess_required is False
    assert result.effort == "simple"
    assert result.segments == ()
    assert result.evidence == ()
    assert "short_prompt" in result.reason_codes
    assert result.answer_policy.source_policy == "do_not_invent_sources_or_facts"
    assert result.answer_policy.verification_required is False
    assert result.answer_policy.response_contract == "direct_answer_with_uncertainty_boundary"


def test_short_current_fact_question_uses_verification_answer_policy() -> None:
    """Even short latest/version/external-source questions never route to unverified simple answers."""
    item = WorkItem("w-verify", "PyPI hermes-cluxion 최신 버전 뭐야?")

    result = preprocess_work(item)

    assert result.mode == "verification_answer"
    assert result.preprocess_required is False
    assert result.effort == "simple"
    assert "verification_required" in result.reason_codes
    assert result.answer_policy.verification_required is True
    assert result.answer_policy.citation_required is True
    assert result.answer_policy.response_contract == "verify_and_cite_before_answer"
    assert "verify_current_or_recent_fact" in result.answer_policy.required_checks
    assert "cite_external_source_or_document" in result.answer_policy.required_checks


def test_long_prompt_uses_bounded_signal_scan_without_verification_fast_path() -> None:
    """Long prompts are handled by work size, not the fast path, even with fact-finding signals."""
    prompt = ("A" * 6_000) + "\n현재 설치 상태 확인"
    item = WorkItem("w-bounded", prompt)

    result = preprocess_work(item)

    assert result.mode == "standard"
    assert result.preprocess_required is True
    assert "prompt_token_threshold" in result.reason_codes
    assert result.answer_policy.verification_required is True
    assert "verify_current_or_recent_fact" in result.answer_policy.required_checks
    assert "inspect_runtime_state_before_claiming" in result.answer_policy.required_checks


def test_short_engineering_question_still_uses_harness_preprocessing() -> None:
    """Code/test/patch signals block the simple-answer bypass even for short prompts."""
    item = WorkItem("w-engineering", "테스트를 실행하고 실패를 수정해줘.")

    result = preprocess_work(item)

    assert result.mode == "standard"
    assert result.preprocess_required is True
    assert result.segments
    assert "intent_engineering" in result.reason_codes


def test_agent_work_queue_preserves_priority_and_evicts_lower_priority() -> None:
    """A full queue evicts lower-priority work for new important work."""
    queue = AgentWorkQueue(max_size=2)
    low = WorkItem("low", "나중 작업", priority=WorkPriority.LOW)
    normal = WorkItem("normal", "일반 작업", priority=WorkPriority.NORMAL)
    critical = WorkItem("critical", "즉시 작업", priority=WorkPriority.CRITICAL)

    assert queue.enqueue(low).accepted is True
    assert queue.enqueue(normal).accepted is True
    admitted = queue.enqueue(critical)

    assert admitted.accepted is True
    assert admitted.evicted_work_id == "low"
    assert queue.peek_order() == ("critical", "normal")
    assert queue.dequeue().work_id == "critical"


def test_rust_pressure_blocks_emergency_snapshot() -> None:
    """The Rust pressure boundary blocks new work under critical memory pressure."""
    snapshot = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=2_000, swap_used_mb=0, cpu_percent=30.0)

    decision = evaluate_pressure(snapshot, requested_parallel=4)

    assert decision.allowed is False
    assert decision.mode == "emergency_stop"
    assert decision.recommended_parallel == 0


def test_capacity_decision_uses_rust_envelope_for_local_model() -> None:
    """Local model work is admitted into the qwen slot by the Rust capacity envelope."""
    snapshot = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=40_000, swap_used_mb=0, cpu_percent=20.0)

    decision = capacity_decision("qwen", snapshot, expected_ram_mb=24_000)

    assert decision.allowed is True
    assert decision.work_kind == "qwen"
    assert decision.dispatch_memory_budget_mb >= 24_000


def test_capacity_decision_uses_rust_grok_slot() -> None:
    """Grok Build work is admitted in a Rust slot separate from Codex."""
    snapshot = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=40_000, swap_used_mb=0, cpu_percent=20.0)

    decision = capacity_decision("grok", snapshot, expected_ram_mb=512, active_codex=3)

    assert decision.allowed is True
    assert decision.work_kind == "grok"
    assert decision.recommended_parallel == 1


def test_resource_snapshot_uses_non_blocking_cpu_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resource snapshot adds no fixed sleep to the preprocessing path."""

    class Memory:
        total = 48_000 * 1_048_576
        available = 40_000 * 1_048_576

    class Swap:
        used = 0

    seen: dict[str, object] = {}

    def fake_cpu_percent(*, interval: float | None = None) -> float:
        seen["interval"] = interval
        return 12.5

    monkeypatch.setattr("psutil.virtual_memory", lambda: Memory())
    monkeypatch.setattr("psutil.swap_memory", lambda: Swap())
    monkeypatch.setattr("psutil.cpu_percent", fake_cpu_percent)

    snapshot = collect_resource_snapshot()

    assert snapshot.cpu_percent == 12.5
    assert seen["interval"] is None


def test_vllm_mlx_profile_wraps_apple_local_runtime() -> None:
    """The vLLM-MLX profile builds an OpenAI-compatible server command."""
    profile = build_vllm_mlx_profile("mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit", port=23003)

    assert profile.kind == RuntimeKind.VLLM_MLX
    assert profile.base_url == "http://127.0.0.1:23003/v1"
    assert profile.command[:3] == ("vllm-mlx", "serve", "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit")
    assert "--continuous-batching" in profile.command
    assert "--enable-prefix-cache" in profile.command


def test_grok_build_surface_does_not_set_cluxion_default_model() -> None:
    """Grok Build is just a host surface, not the Cluxion product default model."""
    specs = {spec.surface: spec for spec in default_adapter_specs()}

    assert specs[AgentSurface.GROK_BUILD].transport == "local_override"
    assert "grok-composer" not in specs[AgentSurface.GROK_BUILD].install_hint


def test_intent_classifier_detects_direction_without_model_call() -> None:
    """User intent and execution direction are classified deterministically before any model call."""
    item = WorkItem(
        "w-intent",
        "보안 취약점과 테스트 실패를 점검해줘.",
        surface=AgentSurface.HERMES,
    )

    intent = classify_intent(item)

    assert intent.category == "security"
    assert intent.operation == "review_risk"
    assert intent.direction == "host_managed"
    assert "security" in intent.signals


def test_host_surface_plan_stays_host_managed_without_local_route() -> None:
    """The host default model stays host-managed; Cluxion never builds Grok commands."""
    item = WorkItem(
        "w-plan",
        "작업: 작은 버그를 고치고 테스트를 실행해줘.",
        surface=AgentSurface.GROK_BUILD,
        metadata={"cwd": "/tmp/project"},
    )
    snapshot = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=40_000, swap_used_mb=0, cpu_percent=20.0)

    plan = build_harness_plan(item, snapshot=snapshot)

    assert plan.preprocessing.normalized_prompt == item.prompt
    assert plan.intent.direction == "grok_build_harness"
    assert plan.resource.allowed is True
    assert plan.resource.work_kind == "grok"
    assert plan.runtime.kind == RuntimeKind.HOST_MANAGED
    assert plan.runtime.model == "host/default"
    assert plan.runtime.command == ()


def test_simple_answer_plan_skips_resource_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The simple-answer fast path never pays the psutil resource snapshot cost."""

    def fail_snapshot() -> ResourceSnapshot:
        raise AssertionError("simple answer must not collect resource snapshot")

    monkeypatch.setattr("cluxion_runtime.core.harness.collect_resource_snapshot", fail_snapshot)
    item = WorkItem("w-simple-plan", "Hermes에서는 skill과 tool 밖에 안 보여?", surface=AgentSurface.HERMES)

    plan = build_harness_plan(item)

    assert plan.preprocessing.mode == "simple_answer"
    assert plan.preprocessing.preprocess_required is False
    assert plan.resource.mode == "simple_answer"
    assert plan.resource.work_kind == "simple_answer"
    assert "resource_snapshot_skipped" in plan.resource.reason_codes
    assert plan.execution.model_owner == "hermes_current_model"
    assert plan.execution.strategy == "current_turn_direct_answer"
    assert plan.execution.preflight_required is False
    assert plan.execution.max_extra_model_calls == 0


def test_verification_answer_plan_skips_resource_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short fact-finding queries build only a verification contract, without psutil snapshot cost."""

    def fail_snapshot() -> ResourceSnapshot:
        raise AssertionError("verification answer must not collect resource snapshot")

    monkeypatch.setattr("cluxion_runtime.core.harness.collect_resource_snapshot", fail_snapshot)
    item = WorkItem("w-verify-plan", "현재 설치된 hermes-cluxion 버전 확인해줘.")

    plan = build_harness_plan(item)

    assert plan.preprocessing.mode == "verification_answer"
    assert plan.preprocessing.preprocess_required is False
    assert plan.preprocessing.answer_policy.verification_required is True
    assert "inspect_runtime_state_before_claiming" in plan.preprocessing.answer_policy.required_checks
    assert plan.resource.mode == "verification_answer"
    assert plan.resource.work_kind == "verification_answer"
    assert "resource_snapshot_skipped" in plan.resource.reason_codes
    assert plan.execution.strategy == "current_turn_verify_then_answer"
    assert plan.execution.preflight_required is False
    assert plan.execution.max_extra_model_calls == 0


def test_local_model_route_uses_vllm_mlx_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cluxion plans a vLLM-MLX server only when a local model route is explicit."""
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    item = WorkItem(
        "w-local",
        "로컬 모델로 처리해줘.",
        surface=AgentSurface.HERMES,
        model_route="vllm-mlx/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
    )
    snapshot = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=40_000, swap_used_mb=0, cpu_percent=20.0)

    plan = build_harness_plan(item, snapshot=snapshot)

    assert plan.resource.work_kind == "qwen"
    assert plan.runtime.kind == RuntimeKind.VLLM_MLX
    assert plan.runtime.command[:3] == ("vllm-mlx", "serve", "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit")
    assert plan.preprocessing.preprocess_required is True
    assert "local_model_route" in plan.preprocessing.reason_codes
    assert "all_completion_calls_still_belong_to_hermes" in plan.execution.provider_policy


def test_local_model_route_falls_back_off_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    """On non-Mac runners the same local route degrades to a portable OpenAI-compatible plan."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    item = WorkItem(
        "w-local-linux",
        "로컬 모델 endpoint만 준비해줘.",
        surface=AgentSurface.HERMES,
        model_route="vllm-mlx/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
    )
    snapshot = ResourceSnapshot(total_ram_mb=48_000, available_ram_mb=40_000, swap_used_mb=0, cpu_percent=20.0)

    plan = build_harness_plan(item, snapshot=snapshot)

    assert plan.resource.work_kind == "qwen"
    assert plan.runtime.kind == RuntimeKind.OPENAI_COMPAT
    assert plan.runtime.command[:3] == ("vllm", "serve", "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit")
