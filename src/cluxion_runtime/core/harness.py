"""Harness tying preprocessing, the work queue, Rust admission, and local model profiles together."""

from __future__ import annotations

from cluxion_runtime.core.clarification import assess_clarification
from cluxion_runtime.core.intent import classify_intent
from cluxion_runtime.core.preprocess import preprocess_work
from cluxion_runtime.core.types import (
    AgentSurface,
    HarnessPlan,
    HostExecutionPlan,
    HostExecutionStep,
    ModelRuntimeProfile,
    PreprocessResult,
    ResourceDecision,
    ResourceSnapshot,
    RuntimeKind,
    WorkItem,
)
from cluxion_runtime.models.vllm_mlx import select_mac_local_profile
from cluxion_runtime.resources.queue_bridge import queue_available
from cluxion_runtime.resources.rust_bridge import capacity_decision, collect_resource_snapshot


def build_harness_plan(
    item: WorkItem,
    *,
    snapshot: ResourceSnapshot | None = None,
    queue_position: int = 0,
) -> HarnessPlan:
    """Convert an external agent request into a Cluxion internal execution plan."""
    intent = classify_intent(item)
    clarification = assess_clarification(item, intent)
    if clarification.required:
        preprocessed = preprocess_work(
            item,
            intent_category=intent.category,
            local_model_requested=intent.local_model_requested,
            force_mode="needs_clarification",
        )
        runtime = _runtime_profile_for(item)
        return HarnessPlan(
            item=item,
            intent=intent,
            preprocessing=preprocessed,
            resource=_fast_answer_decision("needs_clarification"),
            runtime=runtime,
            execution=_clarification_execution_plan(item, clarification),
            queue_position=queue_position,
            clarification_required=True,
            clarification_questions=tuple(question.prompt for question in clarification.questions),
            queue_backend="rust" if queue_available() else "python",
        )
    work_kind = _work_kind_for(item, intent_category=intent.category)
    preprocessed = preprocess_work(
        item,
        intent_category=intent.category,
        local_model_requested=intent.local_model_requested,
    )
    runtime = _runtime_profile_for(item)
    resource = (
        _fast_answer_decision(preprocessed.mode)
        if _uses_fast_answer_resource_path(preprocessed.mode, runtime.kind)
        else _capacity_decision_for(item, preprocessed.token_estimate, work_kind=work_kind, snapshot=snapshot)
    )
    return HarnessPlan(
        item=item,
        intent=intent,
        preprocessing=preprocessed,
        resource=resource,
        runtime=runtime,
        execution=_host_execution_plan_for(item, preprocessed, runtime),
        queue_position=queue_position,
        clarification_required=False,
        queue_backend="rust" if queue_available() else "python",
    )


def _clarification_execution_plan(item: WorkItem, clarification: object) -> HostExecutionPlan:
    questions = getattr(clarification, "questions", ())
    question_lines = "\n".join(f"- {question.prompt}" for question in questions)
    prompt = (
        "[cluxion_needs_clarification]\n"
        "Do not guess or start work yet. Ask the user these questions and wait for answers.\n"
        "If you still do not know after available context, say you do not know.\n"
        f"{question_lines}"
    )
    return HostExecutionPlan(
        model_owner="host_current_model",
        provider_policy="ask_user_before_queue; do_not_enqueue_until_direction_is_clear",
        strategy="ask_user_before_queue",
        queue_required=False,
        synthesis_required=False,
        preflight_required=False,
        max_extra_model_calls=0,
        steps=(
            HostExecutionStep(
                "clarify",
                "ask_user_questions",
                prompt,
                required_checks=("say_unknown_if_insufficient_context", "do_not_start_work_without_user_direction"),
            ),
        ),
        performance_notes=("clarification_blocks_queue_until_user_answers",),
    )


def _host_execution_plan_for(
    item: WorkItem,
    preprocessed: PreprocessResult,
    runtime: ModelRuntimeProfile,
) -> HostExecutionPlan:
    mode = preprocessed.mode
    policy = preprocessed.answer_policy
    if runtime.kind == RuntimeKind.HOST_MANAGED:
        provider_policy = (
            "use_current_hermes_model_and_oauth; cluxion_must_not_call_or_configure_cloud_model_provider"
            if item.surface == AgentSurface.HERMES
            else "use_current_host_agent_model; cluxion_only_returns_execution_contract"
        )
    else:
        provider_policy = (
            "start_or_verify_cluxion_local_endpoint_then_switch_hermes_to_custom_provider; "
            "all_completion_calls_still_belong_to_hermes"
        )
    base_notes = (
        "simple_and_verification_modes_skip_resource_snapshot",
        "do_not_add_extra_model_call_unless_queue_requires_segment_processing",
    )
    if mode == "simple_answer":
        return HostExecutionPlan(
            model_owner="hermes_current_model" if item.surface == AgentSurface.HERMES else "host_current_model",
            provider_policy=provider_policy,
            strategy="current_turn_direct_answer",
            queue_required=False,
            synthesis_required=False,
            preflight_required=False,
            max_extra_model_calls=0,
            steps=(
                HostExecutionStep(
                    "answer",
                    "direct_answer",
                    item.prompt,
                    required_checks=policy.required_checks,
                    token_estimate=preprocessed.token_estimate,
                ),
            ),
            performance_notes=base_notes,
        )
    if mode == "verification_answer":
        return HostExecutionPlan(
            model_owner="hermes_current_model" if item.surface == AgentSurface.HERMES else "host_current_model",
            provider_policy=provider_policy,
            strategy="current_turn_verify_then_answer",
            queue_required=False,
            synthesis_required=False,
            preflight_required=False,
            max_extra_model_calls=0,
            steps=(
                HostExecutionStep(
                    "verify",
                    "verify_then_answer",
                    item.prompt,
                    required_checks=policy.required_checks,
                    token_estimate=preprocessed.token_estimate,
                ),
            ),
            performance_notes=base_notes,
        )
    if mode == "queued":
        segment_steps = tuple(
            HostExecutionStep(
                f"exec_{segment.segment_id}",
                "execute_segment",
                "Fetch this step through cluxion_queue_next, process it with the current Hermes model, "
                "then store the result with cluxion_queue_record.",
                segment_id=segment.segment_id,
                checksum=segment.checksum,
                token_estimate=segment.token_estimate,
                required_checks=policy.required_checks,
            )
            for segment in preprocessed.segments
        )
        final_step = HostExecutionStep(
            "brief",
            "synthesize_briefing",
            "After all segment steps are recorded, call cluxion_queue_brief and answer from its briefing_prompt.",
            depends_on=tuple(step.step_id for step in segment_steps),
            required_checks=policy.required_checks,
            token_estimate=preprocessed.token_estimate,
        )
        return HostExecutionPlan(
            model_owner="hermes_current_model" if item.surface == AgentSurface.HERMES else "host_current_model",
            provider_policy=provider_policy,
            strategy="durable_segment_queue",
            queue_required=True,
            synthesis_required=True,
            preflight_required=False,
            max_extra_model_calls=len(segment_steps) + 1,
            steps=(*segment_steps, final_step),
            next_tool="cluxion_queue_next",
            record_tool="cluxion_queue_record",
            brief_tool="cluxion_queue_brief",
            performance_notes=(
                *base_notes,
                "queued_plan_stores_segment_content_out_of_band",
                "initial_plan_returns_metadata_not_full_segment_payload",
            ),
        )
    return HostExecutionPlan(
        model_owner="hermes_current_model" if item.surface == AgentSurface.HERMES else "host_current_model",
        provider_policy=provider_policy,
        strategy="single_host_task",
        queue_required=False,
        synthesis_required=False,
        preflight_required=False,
        max_extra_model_calls=0,
        steps=(
            HostExecutionStep(
                "execute",
                "execute_task",
                preprocessed.normalized_prompt,
                required_checks=policy.required_checks,
                token_estimate=preprocessed.token_estimate,
            ),
        ),
        performance_notes=base_notes,
    )


def _capacity_decision_for(
    item: WorkItem,
    token_estimate: int,
    *,
    work_kind: str,
    snapshot: ResourceSnapshot | None,
) -> ResourceDecision:
    current = collect_resource_snapshot() if snapshot is None else snapshot
    return capacity_decision(
        work_kind,
        current,
        expected_ram_mb=_expected_ram(item, token_estimate),
    )


def _uses_fast_answer_resource_path(mode: str, runtime_kind: RuntimeKind) -> bool:
    return mode in {"simple_answer", "verification_answer"} and runtime_kind == RuntimeKind.HOST_MANAGED


def _fast_answer_decision(mode: str) -> ResourceDecision:
    reason = "verification_required" if mode == "verification_answer" else "preprocess_not_required"
    return ResourceDecision(
        True,
        mode,
        reason,
        1,
        mode,
        0,
        ("fast_path", "resource_snapshot_skipped"),
    )


def _work_kind_for(item: WorkItem, *, intent_category: str) -> str:
    route = item.model_route.lower()
    if _explicit_local_route(route):
        return "qwen"
    if intent_category == "security":
        return "security"
    if item.surface == AgentSurface.CODEX:
        return "codex"
    if item.surface == AgentSurface.GROK_BUILD:
        return "grok"
    if item.surface == AgentSurface.CLAUDE:
        return "claude"
    return "generic"


def _expected_ram(item: WorkItem, token_estimate: int) -> int:
    if item.expected_ram_mb > 0:
        return item.expected_ram_mb
    if not _explicit_local_route(item.model_route.lower()):
        return 512
    route = item.model_route.lower()
    if "35b" in route or "32b" in route:
        return 24_000
    if "14b" in route or "13b" in route:
        return 12_000
    if "7b" in route or "8b" in route:
        return 8_000
    return 2_000 + min(8_000, token_estimate // 2)


def _model_name(model_route: str) -> str:
    for prefix in ("local/", "mlx/", "vllm-mlx/", "vllm_mlx/"):
        if model_route.startswith(prefix):
            return model_route.removeprefix(prefix)
    return model_route


def _runtime_profile_for(item: WorkItem) -> ModelRuntimeProfile:
    if not _explicit_local_route(item.model_route.lower()):
        return _host_managed_profile(item)
    return select_mac_local_profile(_model_name(item.model_route))


def _host_managed_profile(item: WorkItem) -> ModelRuntimeProfile:
    return ModelRuntimeProfile(
        kind=RuntimeKind.HOST_MANAGED,
        model=item.model_route,
        base_url="",
        command=(),
        health_path="",
    )


def _explicit_local_route(route: str) -> bool:
    return route.startswith(("local/", "mlx/", "vllm-mlx/", "vllm_mlx/")) and route != "local/default"


__all__ = ["build_harness_plan"]
