from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cluxion_runtime.core.types import ModelRuntimeProfile, RuntimeKind
from cluxion_runtime.models.supervisor import LocalModelSupervisor


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0.0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class StubbornProcess(FakeProcess):
    def terminate(self) -> None:
        # Ignores SIGTERM: returncode stays None until kill() lands.
        self.terminated = True


def _profile(command: tuple[str, ...] = ("vllm-mlx", "serve", "demo")) -> ModelRuntimeProfile:
    return ModelRuntimeProfile(
        kind=RuntimeKind.OPENAI_COMPAT,
        model="demo",
        base_url="http://127.0.0.1:8080/v1",
        command=command,
    )


def _supervisor(process: FakeProcess, **kwargs: object) -> LocalModelSupervisor:
    return LocalModelSupervisor(_profile(), process_factory=lambda *_: process, **kwargs)


def test_start_spawns_once_then_reports_already_running() -> None:
    supervisor = _supervisor(FakeProcess())
    first = supervisor.start()
    assert first.started is True and first.pid == 4242
    second = supervisor.start()
    assert second.started is False
    assert second.reason == "already_running"


def test_start_refuses_empty_command() -> None:
    supervisor = LocalModelSupervisor(_profile(command=()), process_factory=lambda *_: FakeProcess())
    result = supervisor.start()
    assert result.started is False
    assert result.reason == "empty_command"


def test_stop_terminates_gracefully() -> None:
    process = FakeProcess()
    supervisor = _supervisor(process)
    supervisor.start()
    assert supervisor.stop() is True
    assert process.terminated is True
    assert process.killed is False
    assert supervisor.is_running() is False


def test_stop_escalates_to_kill_on_timeout() -> None:
    process = StubbornProcess()
    supervisor = _supervisor(process)
    supervisor.start()
    assert supervisor.stop(timeout_sec=0.01) is True
    assert process.killed is True


def test_stop_without_process_is_noop() -> None:
    assert _supervisor(FakeProcess()).stop() is False


def test_health_check_parses_models() -> None:
    body = json.dumps({"data": [{"id": "demo-model"}, {"id": "aux"}]})
    supervisor = LocalModelSupervisor(_profile(), health_getter=lambda url, timeout: (200, body))
    health = supervisor.health_check()
    assert health.reachable is True
    assert health.models == ("demo-model", "aux")


def test_health_check_flags_server_error_and_exceptions() -> None:
    erroring = LocalModelSupervisor(_profile(), health_getter=lambda url, timeout: (503, "{}"))
    assert erroring.health_check().reachable is False
    assert erroring.health_check().reason == "server_error"

    def boom(url: str, timeout: float) -> tuple[int, str]:
        raise OSError("connection refused")

    unreachable = LocalModelSupervisor(_profile(), health_getter=boom)
    health = unreachable.health_check()
    assert health.reachable is False
    assert health.reason.startswith("health_check_failed:")


def test_health_url_does_not_duplicate_v1() -> None:
    seen: list[str] = []

    def capture(url: str, timeout: float) -> tuple[int, str]:
        seen.append(url)
        return 200, "{}"

    LocalModelSupervisor(_profile(), health_getter=capture).health_check()
    assert seen == ["http://127.0.0.1:8080/v1/models"]


def test_start_reports_binary_not_found_instead_of_raising() -> None:
    def missing(*_: object) -> FakeProcess:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'vllm-mlx'")

    supervisor = LocalModelSupervisor(_profile(), process_factory=missing)
    result = supervisor.start()
    assert result.started is False
    assert result.pid == 0
    assert result.reason.startswith("binary_not_found:")
    assert supervisor.is_running() is False


def test_start_reports_binary_not_found_on_permission_error() -> None:
    def denied(*_: object) -> FakeProcess:
        raise PermissionError("[Errno 13] Permission denied: 'vllm-mlx'")

    result = LocalModelSupervisor(_profile(), process_factory=denied).start()
    assert result.started is False
    assert result.reason.startswith("binary_not_found:")


def test_default_factory_missing_binary_returns_clean_result(tmp_path: Path) -> None:
    missing = tmp_path / "vllm-mlx"
    result = LocalModelSupervisor(_profile(command=(str(missing), "serve", "demo"))).start()
    assert result.started is False
    assert result.pid == 0
    assert result.reason.startswith("binary_not_found:")


def test_default_factory_spawns_real_process() -> None:
    supervisor = LocalModelSupervisor(_profile(command=(sys.executable, "-c", "import time; time.sleep(30)")))
    result = supervisor.start()
    try:
        assert result.started is True
        assert result.pid > 0
        assert supervisor.is_running() is True
    finally:
        supervisor.stop()
