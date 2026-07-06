"""Shared cluxion-runtime CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from cluxion_runtime.adapters.contract import work_item_from_adapter_payload
from cluxion_runtime.bootstrap import ensure_local_runtime
from cluxion_runtime.core.dispatch_store import (
    DispatchStoreError,
    build_briefing_payload,
    next_dispatch_step,
    persist_dispatch_bundle,
    record_dispatch_result,
)
from cluxion_runtime.core.harness import build_harness_plan
from cluxion_runtime.core.loop_auto import (
    LoopAutoOptions,
    run_loop_auto,
    should_auto_loop_plan,
    strip_loop_auto_directive,
)
from cluxion_runtime.core.plan_codec import plan_to_dict
from cluxion_runtime.core.types import AgentSurface, ModelRuntimeProfile

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = _build_parser(json_mode=_json_mode_requested(argv))
    try:
        args = parser.parse_args(argv)
        return _dispatch(args, parser)
    except PayloadError as exc:
        return _print_payload_error(exc)
    except ValueError as exc:
        return _print_payload_error(
            PayloadError(
                str(exc),
                "check prompt, surface, priority, and non-negative numeric payload fields",
                exit_code=1,
            )
        )


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "bootstrap":
        return _run_bootstrap(args)
    if args.command == "plan":
        return _run_plan(args)
    if args.command == "serve-local":
        return _run_serve_local(args)
    if args.command == "hermes-local-config":
        return _run_hermes_local_config(args)
    if args.command == "queue-next":
        return _run_queue_next(args)
    if args.command == "queue-record":
        return _run_queue_record(args)
    if args.command == "queue-brief":
        return _run_queue_brief(args)
    if args.command == "loop-auto":
        return _run_loop_auto(args)
    if args.command == "context-compress":
        return _run_context_compress(args)
    if args.command == "guard":
        return _run_guard(args)
    parser.print_help(sys.stderr)
    return 2


def _json_mode_requested(argv: Sequence[str] | None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    return any(arg in {"--json", "--json-stdin"} for arg in args)


class _RuntimeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, json_mode: bool = False, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._json_mode = json_mode

    def error(self, message: str) -> None:
        if self._json_mode:
            raise PayloadError(message, "check the command arguments and JSON flags")
        super().error(message)


def _parser_factory(json_mode: bool):
    return lambda *args, **kwargs: _RuntimeArgumentParser(*args, json_mode=json_mode, **kwargs)


def _build_parser(*, json_mode: bool = False) -> argparse.ArgumentParser:
    parser_class = _parser_factory(json_mode)
    parser = parser_class(prog="cluxion-runtime")
    subparsers = parser.add_subparsers(dest="command", parser_class=parser_class)
    bootstrap = subparsers.add_parser("bootstrap", help="Install or upgrade local model runtime dependencies")
    bootstrap.add_argument(
        "--upgrade", action="store_true", help="Upgrade to the latest version even when already installed"
    )
    bootstrap.add_argument("--dry-run", action="store_true", help="Print the installer command without installing")
    bootstrap.add_argument(
        "--package", action="append", default=None, help="Package spec to install. Default: vllm-mlx"
    )
    plan = subparsers.add_parser("plan", help="Convert an external agent task into a Cluxion harness plan")
    plan.add_argument("--surface", default=AgentSurface.API.value)
    plan.add_argument("--work-id", default="")
    plan.add_argument("--prompt", default="")
    plan.add_argument("--model-route", default="host/default")
    plan.add_argument("--priority", default="normal")
    plan.add_argument("--expected-ram-mb", type=int, default=0)
    plan.add_argument("--context-tokens", type=int, default=0)
    plan.add_argument("--cwd", default="")
    plan.add_argument("--loop-auto", action="store_true")
    plan.add_argument("--json-stdin", action="store_true")
    serve = subparsers.add_parser("serve-local", help="Prepare a vLLM-MLX server endpoint for a local model")
    serve.add_argument("--model", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--max-tokens", type=int, default=128_000)
    serve.add_argument("--dry-run", action="store_true")
    serve.add_argument("--auto-install", action=argparse.BooleanOptionalAction, default=True)
    serve.add_argument("--upgrade-runtime", action="store_true")
    hermes = subparsers.add_parser(
        "hermes-local-config", help="Print a local endpoint patch for a Hermes custom provider"
    )
    hermes.add_argument("--model", required=True)
    hermes.add_argument("--host", default="127.0.0.1")
    hermes.add_argument("--port", type=int, default=8000)
    hermes.add_argument("--max-tokens", type=int, default=128_000)
    hermes.add_argument("--context-length", type=int, default=128_000)
    hermes.add_argument("--provider-key", default="cluxion-local")
    hermes.add_argument("--display-name", default="Cluxion Local")
    queue_next = subparsers.add_parser("queue-next", help="Return the next segment from the stored dispatch queue")
    queue_next.add_argument("--work-id", required=True)
    queue_next.add_argument(
        "--full",
        action="store_true",
        help="Emit complete fields for this queue-next call only; it still advances to the next unrecorded step",
    )
    queue_record = subparsers.add_parser(
        "queue-record", help="Record a segment execution result into the dispatch queue"
    )
    queue_record.add_argument("--work-id", required=True)
    queue_record.add_argument("--step-id", required=True)
    queue_record.add_argument("--result", default="")
    queue_record.add_argument("--error", default="")
    queue_record.add_argument("--failed", action="store_true")
    queue_record.add_argument("--json-stdin", action="store_true")
    queue_brief = subparsers.add_parser(
        "queue-brief", help="Build the final briefing prompt from stored segment results"
    )
    queue_brief.add_argument("--work-id", required=True)
    loop_auto = subparsers.add_parser(
        "loop-auto",
        help="Autonomously drain the dispatch queue via Hermes oneshot calls (/loopAuto)",
    )
    loop_auto.add_argument("--work-id", required=True)
    loop_auto.add_argument("--cwd", default="")
    loop_auto.add_argument("--hermes-bin", default="hermes")
    loop_auto.add_argument("--model", default="")
    loop_auto.add_argument("--timeout-seconds", type=float, default=600.0)
    loop_auto.add_argument("--max-segment-retries", type=int, default=2)
    loop_auto.add_argument("--dry-run", action="store_true")
    loop_auto.add_argument("--json-stdin", action="store_true")
    context_compress = subparsers.add_parser(
        "context-compress",
        help="Deterministically compress conversation context to the target ratio once past the trigger ratio",
    )
    context_compress.add_argument("--json-stdin", action="store_true", required=True)
    guard = subparsers.add_parser(
        "guard",
        help="Real-time resource sampling, ownership scan, daemon control, and opt-in owned-only enforcement",
    )
    guard.add_argument("--json-stdin", action="store_true", required=True)
    return parser


def _run_bootstrap(args: argparse.Namespace) -> int:
    result = ensure_local_runtime(
        packages=tuple(args.package) if args.package else ("vllm-mlx",),
        upgrade=bool(args.upgrade),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


def _run_plan(args: argparse.Namespace) -> int:
    surface = AgentSurface(str(args.surface))
    payload = _payload_from_stdin() if args.json_stdin else _payload_from_args(args)
    payload = _apply_loop_auto_directive(payload)
    payload = _apply_plan_numeric_contract(payload)
    if bool(getattr(args, "loop_auto", False)):
        payload["loop_auto"] = True
    item = work_item_from_adapter_payload(payload, default_surface=surface)
    plan = build_harness_plan(item)
    persisted = persist_dispatch_bundle(plan)
    output = plan_to_dict(plan)
    if persisted is not None:
        item_output = output.get("item")
        if isinstance(item_output, dict):
            item_output["original_prompt_stored"] = True
        output["dispatch_store"] = {"stored": True, "path": str(persisted)}
    loop_auto_flag = payload.get("loop_auto") if isinstance(payload, dict) else None
    if should_auto_loop_plan(output, loop_auto=bool(loop_auto_flag) if loop_auto_flag is not None else None):
        cwd = str(payload.get("cwd", "")) if isinstance(payload, dict) else ""
        loop_result = run_loop_auto(
            LoopAutoOptions(
                work_id=plan.item.work_id,
                cwd=Path(cwd).expanduser() if cwd else Path.cwd(),
                hermes_bin=str(payload.get("hermes_bin", "hermes")) if isinstance(payload, dict) else "hermes",
                model=str(payload.get("model", "")) if isinstance(payload, dict) else "",
                timeout_seconds=float(payload.get("loop_auto_timeout_s", 600)) if isinstance(payload, dict) else 600.0,
                dry_run=bool(payload.get("loop_auto_dry_run", False)) if isinstance(payload, dict) else False,
            )
        )
        output["loop_auto"] = loop_result.to_dict()
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


def _run_loop_auto(args: argparse.Namespace) -> int:
    payload = _payload_from_stdin() if args.json_stdin else {}
    work_id = str(payload.get("work_id", args.work_id))
    cwd_raw = str(payload.get("cwd", args.cwd))
    result = run_loop_auto(
        LoopAutoOptions(
            work_id=work_id,
            cwd=Path(cwd_raw).expanduser() if cwd_raw else Path.cwd(),
            hermes_bin=str(payload.get("hermes_bin", args.hermes_bin)),
            model=str(payload.get("model", args.model)),
            timeout_seconds=float(payload.get("timeout_seconds", args.timeout_seconds)),
            max_segment_retries=int(payload.get("max_segment_retries", args.max_segment_retries)),
            dry_run=bool(payload.get("dry_run", args.dry_run)),
        )
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


QUEUE_PREVIEW_CHARS = 2000


def _bounded_step_payload(payload: dict[str, object], *, full: bool) -> dict[str, object]:
    """Terminal-safe preview: --full disables truncation for this call only."""
    if full:
        return payload
    bounded: dict[str, object] = {}
    truncated = False
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > QUEUE_PREVIEW_CHARS:
            bounded[key] = value[:QUEUE_PREVIEW_CHARS]
            truncated = True
        elif isinstance(value, dict):
            inner = _bounded_step_payload(value, full=False)
            if inner.pop("truncated", False):
                truncated = True
            bounded[key] = inner
        else:
            bounded[key] = value
    if truncated:
        bounded["truncated"] = True
        bounded["truncated_hint"] = "--full disables truncation for the current queue-next call only"
    return bounded


def _run_queue_next(args: argparse.Namespace) -> int:
    try:
        payload = next_dispatch_step(str(args.work_id))
    except DispatchStoreError as exc:
        payload = {"ok": False, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    payload = _bounded_step_payload(payload, full=bool(getattr(args, "full", False)))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_queue_record(args: argparse.Namespace) -> int:
    payload = _payload_from_stdin() if args.json_stdin else {}
    result = str(payload.get("result", args.result))
    error = str(payload.get("error", args.error))
    failed = bool(payload.get("failed", args.failed))
    try:
        recorded = record_dispatch_result(
            str(args.work_id),
            str(args.step_id),
            result=result,
            error=error,
            succeeded=not failed,
        )
    except DispatchStoreError as exc:
        recorded = {"ok": False, "error": str(exc)}
        print(json.dumps(recorded, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(recorded, ensure_ascii=False, sort_keys=True))
    return 0 if recorded.get("ok", True) else 1


def _run_queue_brief(args: argparse.Namespace) -> int:
    try:
        payload = build_briefing_payload(str(args.work_id))
    except DispatchStoreError as exc:
        payload = {"ok": False, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_context_compress(_: argparse.Namespace) -> int:
    from cluxion_runtime.resources import queue_bridge

    payload = _payload_from_stdin()
    try:
        result = queue_bridge.compress_context(payload)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


_VALID_GUARD_ACTIONS = frozenset({"status", "start", "stop", "enforce", "auto-enforce"})


def _guard_action_error(action: str) -> str:
    return f"unknown guard action: {action} (expected: status|start|stop|enforce|auto-enforce)"


def _run_guard(_: argparse.Namespace) -> int:
    from cluxion_runtime.resources import guard_bridge

    payload = _payload_from_stdin()
    action = str(payload.get("action", "status"))
    if action not in _VALID_GUARD_ACTIONS:
        print(json.dumps({"ok": False, "error": _guard_action_error(action)}, ensure_ascii=False, sort_keys=True))
        return 1
    try:
        if action == "start":
            result = guard_bridge.start_daemon(
                interval_ms=_non_negative_int(
                    "interval_ms", payload.get("interval_ms", guard_bridge.DEFAULT_INTERVAL_MS)
                ),
                window=_non_negative_int("window", payload.get("window", guard_bridge.DEFAULT_WINDOW)),
            )
        elif action == "stop":
            result = guard_bridge.stop_daemon()
        elif action == "enforce":
            result = guard_bridge.enforce(
                _pid_list(payload, "owned_roots"),
                cpu_threshold=_non_negative_float(
                    "cpu_threshold", payload.get("cpu_threshold", guard_bridge.DEFAULT_ENFORCE_CPU)
                ),
                rss_threshold_mb=_non_negative_int(
                    "rss_threshold_mb", payload.get("rss_threshold_mb", guard_bridge.DEFAULT_ENFORCE_RSS_MB)
                ),
                grace_seconds=_non_negative_float(
                    "grace_seconds", payload.get("grace_seconds", guard_bridge.DEFAULT_GRACE_SECONDS)
                ),
                dry_run=not bool(payload.get("apply", False)),
                protect=_pid_list(payload, "protect"),
            )
        elif action == "auto-enforce":
            result = guard_bridge.auto_enforce(
                _pid_list(payload, "owned_roots"),
                sustained_cpu=_non_negative_float(
                    "sustained_cpu", payload.get("sustained_cpu", guard_bridge.DEFAULT_SUSTAINED_CPU)
                ),
                ram_floor_mb=_non_negative_int(
                    "ram_floor_mb", payload.get("ram_floor_mb", guard_bridge.DEFAULT_RAM_FLOOR_MB)
                ),
                min_samples=_non_negative_int("min_samples", payload.get("min_samples", guard_bridge.DEFAULT_WINDOW)),
                cpu_threshold=_non_negative_float(
                    "cpu_threshold", payload.get("cpu_threshold", guard_bridge.DEFAULT_ENFORCE_CPU)
                ),
                rss_threshold_mb=_non_negative_int(
                    "rss_threshold_mb", payload.get("rss_threshold_mb", guard_bridge.DEFAULT_ENFORCE_RSS_MB)
                ),
                grace_seconds=_non_negative_float(
                    "grace_seconds", payload.get("grace_seconds", guard_bridge.DEFAULT_GRACE_SECONDS)
                ),
                dry_run=not bool(payload.get("apply", False)),
                protect=_pid_list(payload, "protect"),
            )
        elif action == "status":
            result = {
                "ok": True,
                "sample": guard_bridge.sample(
                    {"cpu_sample_ms": _non_negative_int("cpu_sample_ms", payload.get("cpu_sample_ms", 100))}
                ),
                "daemon": guard_bridge.daemon_status(),
                "state": guard_bridge.read_daemon_state(),
            }
            scan_roots = _pid_list(payload, "owned_roots")
            if scan_roots:
                result["scan"] = guard_bridge.scan(scan_roots)
        else:
            print(json.dumps({"ok": False, "error": _guard_action_error(action)}, ensure_ascii=False, sort_keys=True))
            return 1
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok", False) else 1


def _run_serve_local(args: argparse.Namespace) -> int:
    # Local-model serving pulls in the vllm/mlx dependency chain; import it
    # here so plan/queue/guard commands never pay that startup cost.
    from cluxion_runtime.models import LocalModelSupervisor, build_vllm_mlx_profile

    bootstrap = None
    if args.auto_install and (not args.dry_run or args.upgrade_runtime):
        bootstrap = ensure_local_runtime(upgrade=bool(args.upgrade_runtime))
        if not bootstrap.ok:
            profile = build_vllm_mlx_profile(
                str(args.model),
                host=str(args.host),
                port=int(args.port),
                max_tokens=int(args.max_tokens),
            )
            payload = _serve_payload(profile)
            payload["bootstrap"] = bootstrap.to_dict()
            payload.update({"started": False, "pid": 0, "reason": "bootstrap_failed"})
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 1
    elif args.auto_install and args.dry_run:
        bootstrap = ensure_local_runtime(upgrade=bool(args.upgrade_runtime), dry_run=True)
    executable = _runtime_executable(bootstrap) or "vllm-mlx"
    profile = build_vllm_mlx_profile(
        str(args.model),
        host=str(args.host),
        port=int(args.port),
        max_tokens=int(args.max_tokens),
        executable=executable,
    )
    payload = _serve_payload(profile)
    if bootstrap is not None:
        payload["bootstrap"] = bootstrap.to_dict()
    if not args.dry_run:
        result = LocalModelSupervisor(profile).start()
        payload.update({"started": result.started, "pid": result.pid, "reason": result.reason})
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _runtime_executable(bootstrap: object) -> str | None:
    if bootstrap is None:
        return None
    command_paths = getattr(bootstrap, "command_paths", None) or {}
    value = command_paths.get("vllm-mlx")
    return str(value) if value else None


def _serve_payload(profile: ModelRuntimeProfile) -> dict[str, object]:
    return {
        "provider": "openai-compatible",
        "runtime": profile.kind.value,
        "model": profile.model,
        "base_url": profile.base_url,
        "command": list(profile.command),
        "health_path": profile.health_path,
        "started": False,
        "pid": 0,
    }


def _run_hermes_local_config(args: argparse.Namespace) -> int:
    from cluxion_runtime.adapters.hermes import (
        build_hermes_local_endpoint_patch,
        hermes_config_patch_to_dict,
        hermes_config_set_commands,
        render_hermes_yaml_fragment,
    )
    from cluxion_runtime.models import build_vllm_mlx_profile

    profile = build_vllm_mlx_profile(
        str(args.model),
        host=str(args.host),
        port=int(args.port),
        max_tokens=int(args.max_tokens),
    )
    patch = build_hermes_local_endpoint_patch(
        profile.model,
        profile.base_url,
        provider_key=str(args.provider_key),
        display_name=str(args.display_name),
        context_length=int(args.context_length),
    )
    payload = {
        "provider": patch.provider_id,
        "model": patch.model,
        "base_url": patch.base_url,
        "slash_model": patch.slash_model,
        "serve_command": list(profile.command),
        "config_patch": hermes_config_patch_to_dict(patch),
        "config_set_commands": list(hermes_config_set_commands(patch)),
        "yaml_fragment": render_hermes_yaml_fragment(patch),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


class PayloadError(ValueError):
    """Invalid CLI input that must surface as a JSON error, never a traceback."""

    def __init__(self, message: str, hint: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.hint = hint
        self.exit_code = exit_code


def _print_payload_error(exc: PayloadError) -> int:
    print(
        json.dumps(
            {"ok": False, "error": "invalid_input", "message": str(exc), "hint": exc.hint},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exc.exit_code


def _payload_from_stdin() -> dict[str, object]:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PayloadError(
            f"stdin is not valid JSON: {exc}",
            'pipe a JSON object, e.g. echo \'{"prompt": "..."}\' | cluxion-runtime plan --json-stdin',
        ) from exc
    except RecursionError as exc:
        raise PayloadError(
            "stdin JSON nesting too deep",
            "reduce the nesting depth of the JSON payload",
        ) from exc
    if not isinstance(payload, dict):
        raise PayloadError("stdin JSON must be an object.", "wrap the payload in {...}")
    return dict(payload)


def _payload_from_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "surface": str(args.surface),
        "work_id": str(args.work_id),
        "prompt": str(args.prompt),
        "model_route": str(args.model_route),
        "priority": str(args.priority),
        "expected_ram_mb": _non_negative_int("expected_ram_mb", args.expected_ram_mb),
        "context_tokens": _non_negative_int("context_tokens", args.context_tokens),
        "cwd": str(args.cwd),
        "loop_auto": bool(args.loop_auto),
    }


def _non_negative_int(name: str, value: object, *, parse_exit_code: int = 2) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise PayloadError(
            f"{name} must be an integer, got {value!r}",
            f"pass a non-negative integer for {name} (0 = unspecified)",
            exit_code=parse_exit_code,
        ) from exc
    if parsed < 0:
        raise PayloadError(
            f"{name} must be >= 0, got {parsed}",
            f"pass a non-negative integer for {name} (0 = unspecified)",
        )
    return parsed


def _non_negative_float(name: str, value: object) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        raise PayloadError(
            f"{name} must be a number, got {value!r}",
            f"pass a non-negative number for {name} (0 = unspecified)",
        ) from exc
    if parsed < 0:
        raise PayloadError(
            f"{name} must be >= 0, got {parsed:g}",
            f"pass a non-negative number for {name} (0 = unspecified)",
        )
    return parsed


def _pid_list(payload: dict[str, object], key: str) -> list[int]:
    raw = payload.get(key)
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise PayloadError(
            f"{key} must be a list of process IDs (integers)",
            f'{key} takes root PIDs, e.g. {{"{key}": [1234]}} - not filesystem paths',
        )
    pids: list[int] = []
    for entry in raw:
        try:
            pids.append(int(str(entry)))
        except (TypeError, ValueError) as exc:
            raise PayloadError(
                f"{key} entry {entry!r} is not a process ID",
                f'{key} takes root PIDs (integers), e.g. {{"{key}": [1234]}} - not filesystem paths',
            ) from exc
    return pids


def _apply_loop_auto_directive(payload: dict[str, object]) -> dict[str, object]:
    prompt = str(payload.get("prompt", ""))
    cleaned, had_directive = strip_loop_auto_directive(prompt)
    if not had_directive:
        return payload
    updated = dict(payload)
    updated["prompt"] = cleaned
    updated["loop_auto"] = True
    return updated


def _apply_plan_numeric_contract(payload: dict[str, object]) -> dict[str, object]:
    updated = dict(payload)
    for key in ("expected_ram_mb", "context_tokens"):
        if key in updated:
            updated[key] = _non_negative_int(key, updated[key], parse_exit_code=1)
    return updated


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
