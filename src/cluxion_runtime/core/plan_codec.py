"""Convert a HarnessPlan into a JSON object for external adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cluxion_runtime.core.ledger_codec import item_to_dict

if TYPE_CHECKING:
    from cluxion_runtime.core.types import HarnessPlan


def plan_to_dict(plan: HarnessPlan) -> dict[str, object]:
    """Convert only the public fields of a HarnessPlan into a JSON-safe object."""
    return {
        "item": _public_item(plan),
        "queue_position": plan.queue_position,
        "queue_backend": plan.queue_backend,
        "clarification": {
            "required": plan.clarification_required,
            "questions": list(plan.clarification_questions),
        },
        "intent": {
            "category": plan.intent.category,
            "operation": plan.intent.operation,
            "local_model_requested": plan.intent.local_model_requested,
            "direction": plan.intent.direction,
            "confidence": plan.intent.confidence,
            "signals": list(plan.intent.signals),
        },
        "preprocessing": {
            "normalized_prompt": plan.preprocessing.normalized_prompt,
            "token_estimate": plan.preprocessing.token_estimate,
            "split_required": plan.preprocessing.split_required,
            "effort": plan.preprocessing.effort,
            "mode": plan.preprocessing.mode,
            "preprocess_required": plan.preprocessing.preprocess_required,
            "reason_codes": list(plan.preprocessing.reason_codes),
            "answer_policy": {
                "unknown_behavior": plan.preprocessing.answer_policy.unknown_behavior,
                "source_policy": plan.preprocessing.answer_policy.source_policy,
                "scope": plan.preprocessing.answer_policy.scope,
                "response_contract": plan.preprocessing.answer_policy.response_contract,
                "verification_required": plan.preprocessing.answer_policy.verification_required,
                "citation_required": plan.preprocessing.answer_policy.citation_required,
                "uncertainty_level": plan.preprocessing.answer_policy.uncertainty_level,
                "required_checks": list(plan.preprocessing.answer_policy.required_checks),
                "grounding": list(plan.preprocessing.answer_policy.grounding),
                "rules": list(plan.preprocessing.answer_policy.rules),
            },
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "char_start": segment.char_start,
                    "char_end": segment.char_end,
                    "token_estimate": segment.token_estimate,
                    "checksum": segment.checksum,
                    "preview": segment.preview,
                }
                for segment in plan.preprocessing.segments
            ],
            "evidence": list(plan.preprocessing.evidence),
        },
        "resource": {
            "allowed": plan.resource.allowed,
            "mode": plan.resource.mode,
            "reason": plan.resource.reason,
            "recommended_parallel": plan.resource.recommended_parallel,
            "work_kind": plan.resource.work_kind,
            "dispatch_memory_budget_mb": plan.resource.dispatch_memory_budget_mb,
            "reason_codes": list(plan.resource.reason_codes),
        },
        "runtime": {
            "kind": plan.runtime.kind.value,
            "model": plan.runtime.model,
            "base_url": plan.runtime.base_url,
            "command": list(plan.runtime.command),
            "health_path": plan.runtime.health_path,
        },
        "host_execution": {
            "model_owner": plan.execution.model_owner,
            "provider_policy": plan.execution.provider_policy,
            "strategy": plan.execution.strategy,
            "queue_required": plan.execution.queue_required,
            "synthesis_required": plan.execution.synthesis_required,
            "preflight_required": plan.execution.preflight_required,
            "max_extra_model_calls": plan.execution.max_extra_model_calls,
            "next_tool": plan.execution.next_tool,
            "record_tool": plan.execution.record_tool,
            "brief_tool": plan.execution.brief_tool,
            "loop_tool": plan.execution.loop_tool,
            "performance_notes": list(plan.execution.performance_notes),
            "steps": [
                {
                    "step_id": step.step_id,
                    "kind": step.kind,
                    "prompt": step.prompt,
                    "segment_id": step.segment_id,
                    "checksum": step.checksum,
                    "token_estimate": step.token_estimate,
                    "depends_on": list(step.depends_on),
                    "required_checks": list(step.required_checks),
                }
                for step in plan.execution.steps
            ],
        },
    }


def _public_item(plan: HarnessPlan) -> dict[str, object]:
    item = item_to_dict(plan.item)
    if not plan.execution.queue_required:
        item["prompt_redacted"] = False
        return item
    item["prompt"] = plan.preprocessing.normalized_prompt
    item["prompt_redacted"] = True
    item["original_prompt_stored"] = False
    item["original_prompt_out_of_band_required"] = True
    return item


__all__ = ["plan_to_dict"]
