"""Subprocess bridge to an installed cluxion-runtime command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    CommandRunner = Callable[[Sequence[str], str | None], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RuntimeResult:
    """Normalized result returned to Hermes tools."""

    ok: bool
    command: tuple[str, ...]
    payload: dict[str, object]

    def to_json(self) -> str:
        """Return a Hermes tool-compatible JSON string."""
        return json.dumps(
            {"ok": self.ok, "command": list(self.command), **self.payload}, ensure_ascii=False, sort_keys=True
        )


def runtime_available(binary: str | None = None) -> bool:
    """Check whether cluxion-runtime is visible to Hermes."""
    configured_binary = binary or os.environ.get("CLUXION_RUNTIME_BIN")
    if configured_binary:
        return shutil.which(configured_binary) is not None
    return shutil.which("cluxion-runtime") is not None or _runtime_module_available()


def plan(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Run cluxion-runtime plan for a Hermes task."""
    command = (_runtime_binary(None), "plan", "--json-stdin", "--surface", "hermes")
    stdin = json.dumps(dict(payload), ensure_ascii=False)
    return _execute_json(command, stdin, command_runner)


def bootstrap(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Run cluxion-runtime bootstrap for local dependencies."""
    command = [_runtime_binary(None), "bootstrap"]
    if bool(payload.get("upgrade", False)):
        command.append("--upgrade")
    if bool(payload.get("dry_run", False)):
        command.append("--dry-run")
    packages = payload.get("packages", [])
    if isinstance(packages, list):
        for package in packages:
            command.extend(("--package", str(package)))
    return _execute_json(tuple(command), None, command_runner)


def serve_local(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Run cluxion-runtime serve-local with dry-run by default."""
    model = _required_str(payload, "model")
    port = _port(payload, "port", 23003)
    command = [
        _runtime_binary(None),
        "serve-local",
        "--model",
        model,
        "--host",
        _str(payload, "host", "127.0.0.1"),
        "--port",
        str(port),
        "--max-tokens",
        str(_int(payload, "max_tokens", 128_000)),
    ]
    if not bool(payload.get("auto_install", True)):
        command.append("--no-auto-install")
    if bool(payload.get("upgrade_runtime", False)):
        command.append("--upgrade-runtime")
    if not bool(payload.get("start", False)):
        command.append("--dry-run")
    return _execute_json(tuple(command), None, command_runner)


def hermes_config(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Render Hermes config patch for a local endpoint."""
    model = _required_str(payload, "model")
    port = _port(payload, "port", 23003)
    command = (
        _runtime_binary(None),
        "hermes-local-config",
        "--model",
        model,
        "--host",
        _str(payload, "host", "127.0.0.1"),
        "--port",
        str(port),
        "--context-length",
        str(_int(payload, "context_length", 131_072)),
        "--provider-key",
        _str(payload, "provider_key", "cluxion-local"),
        "--display-name",
        _str(payload, "display_name", "Cluxion Local"),
    )
    return _execute_json(command, None, command_runner)


def queue_next(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Return the next queued segment for Hermes model execution."""
    command = (_runtime_binary(None), "queue-next", "--work-id", _required_str(payload, "work_id"))
    return _execute_json(command, None, command_runner)


def queue_record(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Record the current Hermes model's result for a queued segment."""
    command = (
        _runtime_binary(None),
        "queue-record",
        "--work-id",
        _required_str(payload, "work_id"),
        "--step-id",
        _required_str(payload, "step_id"),
        "--json-stdin",
    )
    stdin = json.dumps(
        {
            "result": str(payload.get("result", "")),
            "error": str(payload.get("error", "")),
            "failed": bool(payload.get("failed", False)),
        },
        ensure_ascii=False,
    )
    return _execute_json(command, stdin, command_runner)


def queue_brief(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Build the final briefing prompt from recorded queued segment results."""
    command = (_runtime_binary(None), "queue-brief", "--work-id", _required_str(payload, "work_id"))
    return _execute_json(command, None, command_runner)


def loop_auto(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Autonomously drain the dispatch queue via Hermes oneshot calls (/loopAuto)."""
    command = (
        _runtime_binary(None),
        "loop-auto",
        "--work-id",
        _required_str(payload, "work_id"),
        "--json-stdin",
    )
    stdin = json.dumps(dict(payload), ensure_ascii=False)
    return _execute_json(command, stdin, command_runner)


def context_compress(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Compress conversation context deterministically once it exceeds the trigger ratio."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be a list of objects")
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or not isinstance(m.get("content"), str):
            raise ValueError(f"message[{i}] must be an object with string content")
    command = (_runtime_binary(None), "context-compress", "--json-stdin")
    stdin = json.dumps(dict(payload), ensure_ascii=False)
    return _execute_json(command, stdin, command_runner)


def guard(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Real-time resource guard: sampling, fail-closed ownership scan, daemon control."""
    command = (_runtime_binary(None), "guard", "--json-stdin")
    stdin = json.dumps(dict(payload), ensure_ascii=False)
    return _execute_json(command, stdin, command_runner)


def web_search(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Search the web through the in-process browser bridge."""
    del command_runner
    from cluxion_runtime.web import browser_bridge

    result = browser_bridge.search(
        _required_str(payload, "query"),
        engine=_str(payload, "engine", "google"),
        max_links=_non_negative_int(payload, "max_links", 25),
        max_chars=_non_negative_int(payload, "max_chars", 8000),
    )
    return _browser_result(("browser", "web_search"), result)


def browser_open(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Open a URL through the in-process browser bridge."""
    del command_runner
    from cluxion_runtime.web import browser_bridge

    result = browser_bridge.open_url(
        _required_str(payload, "url"),
        max_chars=_non_negative_int(payload, "max_chars", 8000),
    )
    return _browser_result(("browser", "open_url"), result)


def browser_extract(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Extract page text through the in-process browser bridge."""
    del command_runner
    from cluxion_runtime.web import browser_bridge

    selector = payload.get("selector")
    selector_value = str(selector).strip() if selector is not None else None
    if selector_value == "":
        selector_value = None
    result = browser_bridge.extract(
        selector=selector_value,
        max_chars=_non_negative_int(payload, "max_chars", 8000),
    )
    return _browser_result(("browser", "extract"), result)


def browser_click(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Click an element through the in-process browser bridge."""
    del command_runner
    from cluxion_runtime.web import browser_bridge

    result = browser_bridge.click(_required_str(payload, "selector"))
    return _browser_result(("browser", "click"), result)


def browser_type(payload: Mapping[str, object], *, command_runner: CommandRunner | None = None) -> RuntimeResult:
    """Type into an input through the in-process browser bridge."""
    del command_runner
    from cluxion_runtime.web import browser_bridge

    result = browser_bridge.type_text(
        _required_str(payload, "selector"),
        _required_str(payload, "text"),
        submit=bool(payload.get("submit", False)),
    )
    return _browser_result(("browser", "type_text"), result)


def _browser_result(command: tuple[str, ...], result: dict[str, object]) -> RuntimeResult:
    return RuntimeResult(bool(result.get("ok")), command, {"result": result})


def _execute_json(command: Sequence[str], stdin: str | None, command_runner: CommandRunner | None) -> RuntimeResult:
    # In-process is the default fast path: a subprocess pays full interpreter
    # startup (~400ms) per tool call. The subprocess route is taken only on
    # explicit binary override (CLUXION_RUNTIME_BIN) or when the runtime
    # module is not importable; command_runner is the executor for that route.
    try:
        if _can_run_inprocess(command[0]):
            completed = _run_inprocess(command, stdin)
        elif shutil.which(command[0]) is not None:
            completed = (command_runner or _run_command)(command, stdin)
        else:
            return RuntimeResult(False, tuple(command), {"error": "cluxion-runtime not found in PATH"})
    except OSError as exc:
        return RuntimeResult(False, tuple(command), {"error": str(exc)})
    if completed.returncode != 0:
        error = "cluxion-runtime failed"
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        for payload_text in (stderr, stdout):
            if not payload_text:
                continue
            try:
                parsed_err = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed_err, dict) and parsed_err.get("error"):
                error = str(parsed_err["error"])
                break
        return RuntimeResult(
            False,
            tuple(command),
            {"error": error, "stderr": stderr, "returncode": completed.returncode},
        )
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return RuntimeResult(False, tuple(command), {"error": f"invalid JSON from cluxion-runtime: {exc}"})
    if not isinstance(parsed, dict):
        return RuntimeResult(False, tuple(command), {"error": "cluxion-runtime returned non-object JSON"})
    return RuntimeResult(True, tuple(command), {"result": parsed})


def _run_command(command: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_inprocess(command: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    from cluxion_runtime.cli import main as runtime_main

    old_stdin = sys.stdin
    stdout = StringIO()
    stderr = StringIO()
    try:
        sys.stdin = StringIO(stdin or "")
        with redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = runtime_main(list(command[1:]))
    except Exception as exc:
        return subprocess.CompletedProcess(list(command), 1, stdout=stdout.getvalue(), stderr=str(exc))
    finally:
        sys.stdin = old_stdin
    return subprocess.CompletedProcess(
        list(command), int(returncode), stdout=stdout.getvalue(), stderr=stderr.getvalue()
    )


def _runtime_binary(binary: str | None) -> str:
    return binary or os.environ.get("CLUXION_RUNTIME_BIN", "cluxion-runtime")


def _runtime_module_available() -> bool:
    try:
        import cluxion_runtime.cli
    except ImportError:
        return False
    return cluxion_runtime.cli is not None


def _can_run_inprocess(binary: str) -> bool:
    return binary == "cluxion-runtime" and "CLUXION_RUNTIME_BIN" not in os.environ and _runtime_module_available()


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _str(payload: Mapping[str, object], key: str, default: str) -> str:
    value = str(payload.get(key, default)).strip()
    return value or default


def _int(payload: Mapping[str, object], key: str, default: int) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _port(payload: Mapping[str, object], key: str, default: int) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError):
        value = default
    if not (1 <= value <= 65535):
        raise ValueError(f"{key} must be between 1 and 65535")
    return value


def _non_negative_int(payload: Mapping[str, object], key: str, default: int) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


__all__ = [
    "RuntimeResult",
    "bootstrap",
    "browser_click",
    "browser_extract",
    "browser_open",
    "browser_type",
    "context_compress",
    "guard",
    "hermes_config",
    "loop_auto",
    "plan",
    "queue_brief",
    "queue_next",
    "queue_record",
    "runtime_available",
    "serve_local",
    "web_search",
]
