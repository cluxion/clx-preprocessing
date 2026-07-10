"""Autonomous queue drain loop — Codex-style idle continuation for Cluxion dispatch.

Bridges Hermes' weak manual queue loop by driving:
  plan (queued) -> next -> hermes segment -> record -> ... -> brief -> final answer

Inspired by Codex /goal `continue_if_idle` + hermes-call `--until-done` completion markers.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cluxion_runtime.core.dispatch_store import (
    DispatchStoreError,
    build_briefing_payload,
    default_dispatch_dir,
    next_dispatch_step,
    record_dispatch_result,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

SEGMENT_COMPLETE_MARKER = "SEGMENT_COMPLETE"
TASK_COMPLETE_MARKER = "TASK_COMPLETE"
WORK_REMAINS_PREFIX = "WORK_REMAINS:"
SEGMENT_COMPLETION_CONTRACT = (
    "\n\n[cluxion_loop_auto_segment_contract]\n"
    "Process ONLY this segment. Ground answers in the segment content.\n"
    "End your final line with exactly one marker:\n"
    f"- {SEGMENT_COMPLETE_MARKER} when this segment is fully handled\n"
    f"- {WORK_REMAINS_PREFIX}<short remainder> if this segment still needs work\n"
)
BRIEFING_COMPLETION_CONTRACT = (
    "\n\n[cluxion_loop_auto_briefing_contract]\n"
    "Synthesize segment results into a concise user-facing answer.\n"
    f"End your final line with exactly {TASK_COMPLETE_MARKER} when done.\n"
)
LOOP_AUTO_ENV = "CLUXION_LOOP_AUTO"
LOOP_AUTO_DEFAULT_ENV = "CLUXION_LOOP_AUTO_DEFAULT"


@dataclass(frozen=True, slots=True)
class HermesSegmentResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class LoopAutoOptions:
    work_id: str
    cwd: Path = field(default_factory=Path.cwd)
    hermes_bin: str = "hermes"
    model: str = ""
    timeout_seconds: float = 600.0
    max_segment_retries: int = 2
    max_iterations: int = 25
    dry_run: bool = False
    segment_runner: Callable[[str], HermesSegmentResult] | None = None


@dataclass(frozen=True, slots=True)
class LoopAutoStepRecord:
    step_id: str
    segment_id: str
    status: str
    attempts: int
    result_preview: str
    error: str = ""


@dataclass(frozen=True, slots=True)
class LoopAutoResult:
    ok: bool
    work_id: str
    status: str
    segments_processed: int
    segments_failed: int
    briefing_answer: str
    steps: tuple[LoopAutoStepRecord, ...]
    duration_ms: int
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "work_id": self.work_id,
            "status": self.status,
            "segments_processed": self.segments_processed,
            "segments_failed": self.segments_failed,
            "briefing_answer": self.briefing_answer,
            "steps": [
                {
                    "step_id": rec.step_id,
                    "segment_id": rec.segment_id,
                    "status": rec.status,
                    "attempts": rec.attempts,
                    "result_preview": rec.result_preview,
                    "error": rec.error,
                }
                for rec in self.steps
            ],
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class SegmentRunner(Protocol):
    def __call__(self, prompt: str) -> HermesSegmentResult: ...


def loop_auto_enabled(payload: Mapping[str, object] | None = None) -> bool:
    """Return whether auto-loop should run after a queued plan."""
    if payload is not None and "loop_auto" in payload:
        return bool(payload.get("loop_auto"))
    default = os.environ.get(LOOP_AUTO_DEFAULT_ENV, "1").strip().lower()
    if default in {"0", "false", "no", "off"}:
        return False
    return os.environ.get(LOOP_AUTO_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def strip_loop_auto_directive(prompt: str) -> tuple[str, bool]:
    """Strip leading /loopAuto directive from prompt."""
    stripped = prompt.strip()
    if not stripped.lower().startswith("/loopauto"):
        return prompt, False
    body = stripped[len("/loopauto") :].lstrip(" :\n")
    return body, True


def should_auto_loop_plan(plan_payload: Mapping[str, object], *, loop_auto: bool | None = None) -> bool:
    host = plan_payload.get("host_execution")
    if not isinstance(host, dict):
        return False
    if not bool(host.get("queue_required")):
        return False
    if loop_auto is not None:
        return loop_auto
    return False


def run_loop_auto(options: LoopAutoOptions) -> LoopAutoResult:
    """Drain the dispatch queue via Hermes oneshot calls until briefing completes."""
    start = time.monotonic()
    records: list[LoopAutoStepRecord] = []
    processed = 0
    failed = 0

    try:
        preflight_error = _preflight_error(options)
        if preflight_error:
            return _fail(options.work_id, "preflight_failed", records, start, error=preflight_error)
        # Deadline only after validation; normalize int/float to finite positive float.
        timeout_seconds = _normalize_timeout_seconds(options.timeout_seconds)
        if timeout_seconds is None:  # pragma: no cover - guarded by _preflight_error
            return _fail(
                options.work_id,
                "preflight_failed",
                records,
                start,
                error="timeout_seconds must be > 0 and finite",
            )
        options = replace(options, timeout_seconds=timeout_seconds)
        deadline = start + timeout_seconds
        runner = options.segment_runner or _default_segment_runner(options)
        previous_state: tuple[object, ...] | None = None
        iterations = 0
        while time.monotonic() < deadline:
            step_payload = next_dispatch_step(options.work_id)
            if not step_payload.get("ready"):
                break
            if iterations >= options.max_iterations:
                return _finalize(
                    options.work_id,
                    status="iteration_cap_exceeded",
                    records=records,
                    processed=processed,
                    failed=failed,
                    briefing="",
                    start=start,
                    error=f"loop_auto exceeded max_iterations={options.max_iterations}",
                )
            step = step_payload.get("step")
            if not isinstance(step, dict):
                return _fail(options.work_id, "invalid_step_payload", records, start, error="invalid step payload")
            state = _queue_state_signature(step_payload)
            if state == previous_state:
                return _finalize(
                    options.work_id,
                    status="no_progress",
                    records=records,
                    processed=processed,
                    failed=failed,
                    briefing="",
                    start=start,
                    error="dispatch queue made no progress between loop_auto iterations",
                )
            previous_state = state
            iterations += 1
            step_id = str(step.get("step_id", ""))
            segment_id = str(step.get("segment_id", ""))
            try:
                prompt = _build_segment_prompt(step, options.work_id)
                attempts = 0
                last_error = ""
                segment_ok = False
                result_text = ""
                while attempts <= options.max_segment_retries and time.monotonic() < deadline:
                    attempts += 1
                    hermes_result = runner(prompt if attempts == 1 else _resume_segment_prompt(prompt, last_error))
                    if hermes_result.timed_out or hermes_result.returncode != 0:
                        last_error = hermes_result.stderr or f"hermes exit {hermes_result.returncode}"
                        continue
                    marker = _parse_segment_marker(hermes_result.stdout)
                    result_text = _strip_markers(hermes_result.stdout)
                    if marker == SEGMENT_COMPLETE_MARKER:
                        segment_ok = True
                        break
                    if marker and marker.startswith(WORK_REMAINS_PREFIX):
                        last_error = marker.removeprefix(WORK_REMAINS_PREFIX).strip()
                        prompt = _build_segment_prompt(step, options.work_id, remainder=last_error)
                        continue
                    last_error = f"missing completion marker after {attempts} attempts"
                if not segment_ok:
                    record_dispatch_result(
                        options.work_id,
                        step_id,
                        result=result_text,
                        error=last_error or "segment_failed",
                        succeeded=False,
                    )
                    failed += 1
                    records.append(
                        LoopAutoStepRecord(
                            step_id=step_id,
                            segment_id=segment_id,
                            status="failed",
                            attempts=attempts,
                            result_preview=_preview(result_text),
                            error=last_error,
                        )
                    )
                    return _finalize(
                        options.work_id,
                        status="segment_failed",
                        records=records,
                        processed=processed,
                        failed=failed,
                        briefing="",
                        start=start,
                        error=f"segment {step_id} failed: {last_error}",
                    )
                record_dispatch_result(options.work_id, step_id, result=result_text, succeeded=True)
                processed += 1
                records.append(
                    LoopAutoStepRecord(
                        step_id=step_id,
                        segment_id=segment_id,
                        status="succeeded",
                        attempts=attempts,
                        result_preview=_preview(result_text),
                    )
                )
            except BaseException as exc:
                # The step is marked 'running' on disk; without this release a
                # crash here wedges the bundle for every subsequent run.
                _release_crashed_step(options.work_id, step_id, exc)
                raise

        brief = build_briefing_payload(options.work_id)
        if not brief.get("ready"):
            missing = brief.get("missing_steps", [])
            return _finalize(
                options.work_id,
                status="briefing_not_ready",
                records=records,
                processed=processed,
                failed=failed,
                briefing="",
                start=start,
                error=f"missing steps: {missing}",
            )
        briefing_prompt = str(brief.get("briefing_prompt", "")) + BRIEFING_COMPLETION_CONTRACT
        brief_result = runner(briefing_prompt)
        if brief_result.timed_out or brief_result.returncode != 0:
            return _finalize(
                options.work_id,
                status="briefing_failed",
                records=records,
                processed=processed,
                failed=failed,
                briefing="",
                start=start,
                error=brief_result.stderr or f"briefing hermes exit {brief_result.returncode}",
            )
        answer = _strip_markers(brief_result.stdout)
        marker = _parse_briefing_marker(brief_result.stdout)
        status = "complete" if marker == TASK_COMPLETE_MARKER else "complete_unmarked"
        return _finalize(
            options.work_id,
            status=status,
            records=records,
            processed=processed,
            failed=failed,
            briefing=answer,
            start=start,
        )
    except DispatchStoreError as exc:
        return _fail(options.work_id, "dispatch_error", records, start, error=str(exc))
    except Exception as exc:
        return _fail(options.work_id, "loop_error", records, start, error=str(exc))


def _release_crashed_step(work_id: str, step_id: str, exc: BaseException) -> None:
    """Park a step whose worker crashed mid-flight in 'retry_wait' for the next run."""
    if not step_id:
        return
    try:
        record_dispatch_result(
            work_id,
            step_id,
            error=f"loop_auto crashed mid-step: {exc}",
            succeeded=False,
            retryable=True,
        )
    except Exception:
        # Best-effort: if the store itself is broken, the stale-running lease
        # in next_dispatch_step reclaims the step instead.
        pass


def _normalize_timeout_seconds(value: object) -> float | None:
    """Normalize int/float timeout to a representable finite positive float.

    Rejects bool (int subclass), non-int/float types, non-representable ints
    (e.g. 10**400 → inf), NaN/Inf, and <= 0. Never raises.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _preflight_error(options: LoopAutoOptions) -> str:
    if _normalize_timeout_seconds(options.timeout_seconds) is None:
        return "timeout_seconds must be > 0 and finite"
    if options.segment_runner is None and not options.dry_run and not shutil.which(options.hermes_bin):
        return f"hermes binary not found: {options.hermes_bin}"
    try:
        dispatch_dir = default_dispatch_dir()
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        probe = dispatch_dir / ".loop-auto-preflight"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"dispatch store not writable: {exc}"
    return ""


def _queue_state_signature(step_payload: Mapping[str, object]) -> tuple[object, ...]:
    step = step_payload.get("step")
    if not isinstance(step, dict):
        return (False,)
    return (
        step_payload.get("remaining"),
        step.get("step_id"),
        step.get("segment_id"),
        step.get("checksum"),
    )


def _build_segment_prompt(step: Mapping[str, object], work_id: str, *, remainder: str = "") -> str:
    content = str(step.get("content", ""))
    instruction = str(step.get("instruction", ""))
    lines = [
        "[cluxion_loop_auto_segment]",
        f"work_id={work_id}",
        f"step_id={step.get('step_id', '')}",
        f"segment_id={step.get('segment_id', '')}",
        f"checksum={step.get('checksum', '')}",
        instruction,
    ]
    if remainder:
        lines.append(f"remaining_within_segment={remainder}")
    lines.extend(["[segment_content]", content, SEGMENT_COMPLETION_CONTRACT])
    return "\n".join(lines)


def _resume_segment_prompt(base_prompt: str, remainder: str) -> str:
    return f"{base_prompt}\n\n[cluxion_loop_auto_resume]\nContinue this segment. Remaining: {remainder}\n"


def _parse_segment_marker(text: str) -> str | None:
    lines = [line.strip() for line in text.rstrip().splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if last == SEGMENT_COMPLETE_MARKER:
        return SEGMENT_COMPLETE_MARKER
    if last.startswith(WORK_REMAINS_PREFIX):
        return last
    return None


def _parse_briefing_marker(text: str) -> str | None:
    lines = [line.strip() for line in text.rstrip().splitlines() if line.strip()]
    if not lines:
        return None
    if lines[-1] == TASK_COMPLETE_MARKER:
        return TASK_COMPLETE_MARKER
    return None


def _strip_markers(text: str) -> str:
    lines = [
        line
        for line in text.rstrip().splitlines()
        if line.strip() not in {SEGMENT_COMPLETE_MARKER, TASK_COMPLETE_MARKER}
        and not line.strip().startswith(WORK_REMAINS_PREFIX)
    ]
    return "\n".join(lines).strip()


def _default_segment_runner(options: LoopAutoOptions) -> SegmentRunner:
    if options.dry_run:
        return _dry_run_runner(options.work_id)

    hermes_bin = options.hermes_bin
    if not shutil.which(hermes_bin):
        raise RuntimeError(f"hermes binary not found: {hermes_bin}")

    def run(prompt: str) -> HermesSegmentResult:
        command = [hermes_bin]
        if options.model:
            command.extend(["-m", options.model])
        command.extend(["-z", prompt])
        try:
            completed = subprocess.run(
                command,
                cwd=str(options.cwd),
                capture_output=True,
                text=True,
                timeout=max(1.0, options.timeout_seconds),
                check=False,
            )
            return HermesSegmentResult(
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return HermesSegmentResult(stdout=stdout, stderr=stderr or "timeout", returncode=124, timed_out=True)

    return run


def _dry_run_runner(work_id: str) -> SegmentRunner:
    counter = {"n": 0}

    def run(prompt: str) -> HermesSegmentResult:
        counter["n"] += 1
        if "[cluxion_final_briefing]" in prompt or "[cluxion_loop_auto_briefing_contract]" in prompt:
            return HermesSegmentResult(
                stdout=f"Dry-run briefing for {work_id}\n{TASK_COMPLETE_MARKER}\n", stderr="", returncode=0
            )
        return HermesSegmentResult(
            stdout=f"Dry-run segment {counter['n']} for {work_id}\n{SEGMENT_COMPLETE_MARKER}\n",
            stderr="",
            returncode=0,
        )

    return run


def _preview(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _finalize(
    work_id: str,
    *,
    status: str,
    records: list[LoopAutoStepRecord],
    processed: int,
    failed: int,
    briefing: str,
    start: float,
    error: str = "",
) -> LoopAutoResult:
    ok = status in {"complete", "complete_unmarked"} and failed == 0 and not error
    return LoopAutoResult(
        ok=ok,
        work_id=work_id,
        status=status,
        segments_processed=processed,
        segments_failed=failed,
        briefing_answer=briefing,
        steps=tuple(records),
        duration_ms=int((time.monotonic() - start) * 1000),
        error=error,
    )


def _fail(
    work_id: str,
    status: str,
    records: list[LoopAutoStepRecord],
    start: float,
    *,
    error: str,
) -> LoopAutoResult:
    return LoopAutoResult(
        ok=False,
        work_id=work_id,
        status=status,
        segments_processed=sum(1 for r in records if r.status == "succeeded"),
        segments_failed=sum(1 for r in records if r.status == "failed"),
        briefing_answer="",
        steps=tuple(records),
        duration_ms=int((time.monotonic() - start) * 1000),
        error=error,
    )


__all__ = [
    "SEGMENT_COMPLETE_MARKER",
    "TASK_COMPLETE_MARKER",
    "HermesSegmentResult",
    "LoopAutoOptions",
    "LoopAutoResult",
    "loop_auto_enabled",
    "run_loop_auto",
    "should_auto_loop_plan",
    "strip_loop_auto_directive",
]
