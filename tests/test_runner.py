from __future__ import annotations

import json
import os
import stat
import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING

from cluxion_agentplugin_preprocessing import runner

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path


def test_plan_invokes_cluxion_runtime_json_stdin(tmp_path: Path) -> None:
    with _fake_runtime_on_path(tmp_path):
        seen: dict[str, object] = {}

        def command_runner(command: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            seen["command"] = list(command)
            seen["stdin"] = stdin
            return subprocess.CompletedProcess(command, 0, stdout='{"runtime":{"kind":"host_managed"}}', stderr="")

        result = runner.plan({"prompt": "hello"}, command_runner=command_runner)

    payload = json.loads(result.to_json())

    assert result.ok is True
    assert seen["command"] == ["cluxion-runtime", "plan", "--json-stdin", "--surface", "hermes"]
    assert json.loads(str(seen["stdin"])) == {"prompt": "hello"}
    assert payload["result"]["runtime"]["kind"] == "host_managed"


def test_serve_local_defaults_to_dry_run(tmp_path: Path) -> None:
    with _fake_runtime_on_path(tmp_path):

        def command_runner(command: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            assert stdin is None
            assert "--dry-run" in command
            assert "--no-auto-install" not in command
            return subprocess.CompletedProcess(command, 0, stdout='{"base_url":"http://127.0.0.1:23003/v1"}', stderr="")

        result = runner.serve_local({"model": "mlx-community/Qwen"}, command_runner=command_runner)

    assert result.ok is True


def test_serve_local_can_disable_auto_install(tmp_path: Path) -> None:
    with _fake_runtime_on_path(tmp_path):

        def command_runner(command: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            assert stdin is None
            assert "--no-auto-install" in command
            return subprocess.CompletedProcess(command, 0, stdout='{"ok":true}', stderr="")

        result = runner.serve_local(
            {"model": "mlx-community/Qwen", "auto_install": False}, command_runner=command_runner
        )

    assert result.ok is True


def test_bootstrap_passes_upgrade_and_package(tmp_path: Path) -> None:
    with _fake_runtime_on_path(tmp_path):

        def command_runner(command: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            assert stdin is None
            assert list(command) == [
                "cluxion-runtime",
                "bootstrap",
                "--upgrade",
                "--dry-run",
                "--package",
                "vllm-mlx",
            ]
            return subprocess.CompletedProcess(command, 0, stdout='{"ok":true,"changed":false}', stderr="")

        result = runner.bootstrap(
            {"upgrade": True, "dry_run": True, "packages": ["vllm-mlx"]}, command_runner=command_runner
        )

    assert result.ok is True


def test_queue_tools_bridge_to_runtime_cli(tmp_path: Path) -> None:
    with _fake_runtime_on_path(tmp_path):
        seen: list[tuple[list[str], str | None]] = []

        def command_runner(command: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            seen.append((list(command), stdin))
            return subprocess.CompletedProcess(command, 0, stdout='{"ok":true}', stderr="")

        assert runner.queue_next({"work_id": "work-1"}, command_runner=command_runner).ok is True
        assert (
            runner.queue_record(
                {"work_id": "work-1", "step_id": "exec_seg_000", "result": "done"},
                command_runner=command_runner,
            ).ok
            is True
        )
        assert runner.queue_brief({"work_id": "work-1"}, command_runner=command_runner).ok is True

    assert seen[0] == (["cluxion-runtime", "queue-next", "--work-id", "work-1"], None)
    assert seen[1][0] == [
        "cluxion-runtime",
        "queue-record",
        "--work-id",
        "work-1",
        "--step-id",
        "exec_seg_000",
        "--json-stdin",
    ]
    assert json.loads(str(seen[1][1])) == {"error": "", "failed": False, "result": "done"}
    assert seen[2] == (["cluxion-runtime", "queue-brief", "--work-id", "work-1"], None)


def test_missing_runtime_binary_falls_back_to_inprocess_runtime() -> None:
    with _isolated_path(""):
        result = runner.plan({"prompt": "hello", "surface": "hermes"}, command_runner=_unused_runner)

    payload = json.loads(result.to_json())

    assert result.ok is True
    assert payload["result"]["item"]["surface"] == "hermes"


def test_missing_configured_runtime_returns_error() -> None:
    with _isolated_path(""), _isolated_env("CLUXION_RUNTIME_BIN", "/missing/cluxion-runtime"):
        result = runner.plan({"prompt": "hello"}, command_runner=_unused_runner)

    payload = json.loads(result.to_json())

    assert result.ok is False
    assert payload["error"] == "cluxion-runtime not found in PATH"


def _unused_runner(command: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"unexpected command: {command}, stdin={stdin}")


@contextmanager
def _fake_runtime_on_path(tmp_path: Path) -> Iterator[None]:
    # CLUXION_RUNTIME_BIN forces the subprocess route: without it the runner
    # prefers the in-process fast path and the injected command_runner —
    # the subject of these bridge tests — would never be exercised.
    binary = tmp_path / "cluxion-runtime"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    with (
        _isolated_path(f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"),
        _isolated_env("CLUXION_RUNTIME_BIN", "cluxion-runtime"),
    ):
        yield


@contextmanager
def _isolated_path(value: str) -> Iterator[None]:
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = value
    try:
        yield
    finally:
        os.environ["PATH"] = old_path


@contextmanager
def _isolated_env(key: str, value: str) -> Iterator[None]:
    old_value = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if old_value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old_value
