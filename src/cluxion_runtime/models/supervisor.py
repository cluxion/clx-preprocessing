"""Supervisor managing local model server processes and health checks."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.error import HTTPError
from urllib.request import urlopen

# Bounded pre-spawn health and post-spawn early-exit observation windows.
_PRE_SPAWN_HEALTH_TIMEOUT_SEC = 1.0
_EARLY_EXIT_OBSERVE_SEC = 0.2

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
        """Start the model server when no process is running.

        Before spawn, probe ``/v1/models`` once (bounded): matching expected
        model => idempotent ``already_running``; reachable endpoint without
        that model => ``endpoint_in_use`` and no spawn. After spawn, observe a
        short early-exit window so an immediately dead child is reported as
        ``process_exited:<rc>`` rather than ``started``.
        """
        if self.is_running():
            pid = 0 if self._process is None else self._process.pid
            return SupervisorStartResult(False, pid, "already_running")
        preflight = self._pre_spawn_endpoint_check()
        if preflight is not None:
            return preflight
        if not self._profile.command:
            return SupervisorStartResult(False, 0, "empty_command")
        try:
            self._process = self._process_factory(self._profile.command, self._cwd, self._env)
        except OSError as exc:
            return SupervisorStartResult(False, 0, f"binary_not_found:{exc}")
        return self._observe_early_exit()

    def _pre_spawn_endpoint_check(self) -> SupervisorStartResult | None:
        """Classify configured endpoint before spawn.

        - Transport/connection failure → ``None`` (caller may spawn).
        - Any HTTP response (malformed JSON, wrong shape, non-2xx, HTTPError) →
          ``endpoint_in_use`` (never spawn a duplicate).
        - Valid expected health body with the profile model → ``already_running``.

        Does not alter public :meth:`health_check` reachability semantics.
        """
        url = _health_url(self._profile)
        try:
            status, body = self._health_getter(url, _PRE_SPAWN_HEALTH_TIMEOUT_SEC)
        except HTTPError:
            # urllib raises HTTPError (OSError subclass) for 4xx/5xx — occupied.
            return SupervisorStartResult(False, 0, "endpoint_in_use")
        except UnicodeDecodeError:
            # HTTP response arrived but its body is incompatible — occupied.
            return SupervisorStartResult(False, 0, "endpoint_in_use")
        except (OSError, TimeoutError):
            # Connection refused / timeout / other transport failure → spawn.
            return None
        except HTTPException:
            # A non-HTTP or otherwise malformed response still proves occupancy.
            return SupervisorStartResult(False, 0, "endpoint_in_use")
        try:
            models = _parse_models(body)
        except (json.JSONDecodeError, TypeError, ValueError):
            return SupervisorStartResult(False, 0, "endpoint_in_use")
        if not (200 <= int(status) < 300):
            return SupervisorStartResult(False, 0, "endpoint_in_use")
        expected = self._profile.model.strip()
        if expected and expected in models:
            return SupervisorStartResult(False, 0, "already_running")
        return SupervisorStartResult(False, 0, "endpoint_in_use")

    def _observe_early_exit(self) -> SupervisorStartResult:
        process = self._process
        if process is None:
            return SupervisorStartResult(False, 0, "process_exited:unknown")
        deadline = time.monotonic() + _EARLY_EXIT_OBSERVE_SEC
        while True:
            code = process.poll()
            if code is not None:
                self._process = None
                return SupervisorStartResult(False, 0, f"process_exited:{code}")
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return SupervisorStartResult(True, process.pid, "started")

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
        status = int(response.status)
        try:
            body = response.read().decode("utf-8")
        except (OSError, HTTPException, UnicodeDecodeError):
            # Status already arrived: the endpoint is occupied even if its body
            # is reset, malformed, or undecodable.
            body = ""
        return status, body


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
