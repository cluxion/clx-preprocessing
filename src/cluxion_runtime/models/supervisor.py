"""Supervisor managing local model server processes and health checks."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.request import urlopen

if TYPE_CHECKING:
    from cluxion_runtime.core.types import ModelRuntimeProfile


class ManagedProcess(Protocol):
    """Minimal process contract subprocess.Popen must provide."""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[tuple[str, ...], Path | None, Mapping[str, str] | None], ManagedProcess]
HealthGetter = Callable[[str, float], tuple[int, str]]


@dataclass(frozen=True)
class SupervisorStartResult:
    """Model server start result."""

    started: bool
    pid: int
    reason: str


@dataclass(frozen=True)
class ModelServerHealth:
    """OpenAI-compatible model server health result."""

    reachable: bool
    status_code: int
    reason: str
    models: tuple[str, ...] = ()


class LocalModelSupervisor:
    """Manage the lifecycle of a local model server such as vLLM-MLX."""

    def __init__(
        self,
        profile: ModelRuntimeProfile,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        process_factory: ProcessFactory | None = None,
        health_getter: HealthGetter | None = None,
    ) -> None:
        self._profile = profile
        self._cwd = cwd
        self._env = env
        self._process_factory = _spawn_process if process_factory is None else process_factory
        self._health_getter = _default_health_get if health_getter is None else health_getter
        self._process: ManagedProcess | None = None

    @property
    def profile(self) -> ModelRuntimeProfile:
        """Return the runtime profile under supervision."""
        return self._profile

    def start(self) -> SupervisorStartResult:
        """Start the model server when no process is running."""
        if self.is_running():
            pid = 0 if self._process is None else self._process.pid
            return SupervisorStartResult(False, pid, "already_running")
        if not self._profile.command:
            return SupervisorStartResult(False, 0, "empty_command")
        try:
            self._process = self._process_factory(self._profile.command, self._cwd, self._env)
        except (FileNotFoundError, PermissionError) as exc:
            return SupervisorStartResult(False, 0, f"binary_not_found:{exc}")
        return SupervisorStartResult(True, self._process.pid, "started")

    def is_running(self) -> bool:
        """Return true while the process is alive."""
        return self._process is not None and self._process.poll() is None

    def stop(self, *, timeout_sec: float = 5.0) -> bool:
        """Attempt graceful shutdown, then force-kill on timeout."""
        if self._process is None:
            return False
        process = self._process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout_sec)
        self._process = None
        return True

    def health_check(self, *, timeout_sec: float = 1.0) -> ModelServerHealth:
        """Check the OpenAI-compatible `/v1/models` response."""
        url = _health_url(self._profile)
        try:
            status, body = self._health_getter(url, timeout_sec)
            models = _parse_models(body)
            return ModelServerHealth(status < 500, status, "ok" if status < 500 else "server_error", models)
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            return ModelServerHealth(False, 0, f"health_check_failed:{exc}")


def _spawn_process(command: tuple[str, ...], cwd: Path | None, env: Mapping[str, str] | None) -> ManagedProcess:
    child_env = None if env is None else {**os.environ, **dict(env)}
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _default_health_get(url: str, timeout_sec: float) -> tuple[int, str]:
    with urlopen(url, timeout=timeout_sec) as response:
        return int(response.status), response.read().decode("utf-8")


def _health_url(profile: ModelRuntimeProfile) -> str:
    base = profile.base_url.rstrip("/")
    path = profile.health_path
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path.removeprefix("/v1")
    return base + path


def _parse_models(body: str) -> tuple[str, ...]:
    payload = json.loads(body)
    if not isinstance(payload, dict):
        return ()
    data = payload.get("data")
    if not isinstance(data, list):
        return ()
    ids = [item.get("id") for item in data if isinstance(item, dict)]
    return tuple(str(model_id) for model_id in ids if model_id)


__all__ = ["LocalModelSupervisor", "ManagedProcess", "ModelServerHealth", "SupervisorStartResult"]
