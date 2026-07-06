"""Runtime dependency bootstrap for local model backends."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

LOCAL_RUNTIME_PACKAGES: tuple[str, ...] = ("vllm-mlx",)
LOCAL_RUNTIME_COMMANDS: tuple[str, ...] = ("vllm-mlx",)
RUNTIME_VENV_ENV = "CLUXION_PREPROCESS_RUNTIME_VENV"
RUNTIME_HOME_ENV = "CLUXION_PREPROCESS_RUNTIME_HOME"
_LEGACY_RUNTIME_VENV_ENV = "HERMES_CLUXION_RUNTIME_VENV"
_LEGACY_RUNTIME_HOME_ENV = "HERMES_CLUXION_RUNTIME_HOME"


@dataclass(frozen=True)
class BootstrapResult:
    """Dependency bootstrap result."""

    ok: bool
    changed: bool
    packages: tuple[str, ...]
    commands: tuple[str, ...]
    install_command: tuple[str, ...]
    runtime_dir: str = ""
    command_paths: dict[str, str] | None = None
    setup_command: tuple[str, ...] = ()
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "packages": list(self.packages),
            "commands": list(self.commands),
            "install_command": list(self.install_command),
            "runtime_dir": self.runtime_dir,
            "command_paths": dict(self.command_paths or {}),
            "setup_command": list(self.setup_command),
            "returncode": self.returncode,
            "stdout": self.stdout[-4_000:],
            "stderr": self.stderr[-4_000:],
            "reason": self.reason,
        }


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def ensure_local_runtime(
    *,
    packages: Sequence[str] = LOCAL_RUNTIME_PACKAGES,
    commands: Sequence[str] = LOCAL_RUNTIME_COMMANDS,
    upgrade: bool = False,
    dry_run: bool = False,
    python: str | None = None,
    runtime_dir: str | Path | None = None,
    timeout_sec: float = 900.0,
    command_runner: CommandRunner | None = None,
) -> BootstrapResult:
    """Install or upgrade packages required by the local model runtime."""
    package_tuple = tuple(_validate_package_name(package) for package in packages)
    command_tuple = tuple(commands)
    runtime_path = _runtime_dir(runtime_dir)
    runtime_python = Path(python) if python else _runtime_python(runtime_path)
    command_paths = _command_paths(command_tuple, runtime_python)
    missing = tuple(command for command, path in command_paths.items() if not Path(path).exists())
    setup_command = _setup_command(runtime_path) if not python and not runtime_python.exists() else ()
    install_command = _install_command(package_tuple, upgrade=upgrade, python=str(runtime_python))
    if not missing and not upgrade:
        return BootstrapResult(
            True,
            False,
            package_tuple,
            command_tuple,
            install_command,
            str(runtime_path),
            command_paths,
            setup_command,
            reason="already_available",
        )
    if dry_run:
        reason = "upgrade_requested" if upgrade else f"missing_commands:{','.join(missing)}"
        return BootstrapResult(
            True,
            False,
            package_tuple,
            command_tuple,
            install_command,
            str(runtime_path),
            command_paths,
            setup_command,
            reason=reason,
        )
    runner = _run_command if command_runner is None else command_runner
    try:
        setup_completed = _completed_ok(setup_command)
        if setup_command:
            setup_completed = runner(setup_command, timeout_sec)
        if setup_completed.returncode != 0:
            return BootstrapResult(
                False,
                False,
                package_tuple,
                command_tuple,
                install_command,
                str(runtime_path),
                command_paths,
                setup_command,
                setup_completed.returncode,
                setup_completed.stdout,
                setup_completed.stderr,
                "runtime_venv_failed",
            )
        completed = runner(install_command, timeout_sec)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return BootstrapResult(
            False,
            False,
            package_tuple,
            command_tuple,
            install_command,
            str(runtime_path),
            command_paths,
            setup_command,
            1,
            "",
            str(exc),
            "install_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "install_failed",
        )
    ok = completed.returncode == 0 and all(Path(path).exists() for path in command_paths.values())
    reason = "installed" if ok and missing else "upgraded" if ok else "install_failed"
    return BootstrapResult(
        ok,
        completed.returncode == 0,
        package_tuple,
        command_tuple,
        install_command,
        str(runtime_path),
        command_paths,
        setup_command,
        completed.returncode,
        _join_output(setup_completed.stdout, completed.stdout),
        _join_output(setup_completed.stderr, completed.stderr),
        reason,
    )


def _runtime_dir(runtime_dir: str | Path | None) -> Path:
    if runtime_dir is not None:
        return Path(runtime_dir).expanduser()
    for env_name in (RUNTIME_VENV_ENV, _LEGACY_RUNTIME_VENV_ENV):
        env_venv = os_environ(env_name)
        if env_venv:
            return Path(env_venv).expanduser()
    home_value = ""
    for env_name in (RUNTIME_HOME_ENV, _LEGACY_RUNTIME_HOME_ENV):
        home_value = os_environ(env_name)
        if home_value:
            break
    home = Path(home_value or Path.home() / ".local" / "share" / "cluxion-agentplugin-preprocessing")
    return home.expanduser() / "runtime-venv"


def _runtime_python(runtime_dir: Path) -> Path:
    return _bin_dir(runtime_dir) / ("python.exe" if sys.platform == "win32" else "python")


def _bin_dir(runtime_dir: Path) -> Path:
    return runtime_dir / ("Scripts" if sys.platform == "win32" else "bin")


def _command_paths(commands: tuple[str, ...], runtime_python: Path) -> dict[str, str]:
    return {command: str(runtime_python.parent / command) for command in commands}


def _setup_command(runtime_dir: Path) -> tuple[str, ...]:
    uv = shutil.which("uv")
    if uv is not None:
        return (uv, "venv", "--seed", str(runtime_dir))
    return (sys.executable, "-m", "venv", str(runtime_dir))


def _install_command(packages: tuple[str, ...], *, upgrade: bool, python: str | None) -> tuple[str, ...]:
    python_executable = python or sys.executable
    if _python_module_available(python_executable, "pip"):
        command = [python_executable, "-m", "pip", "install", "--disable-pip-version-check"]
        if upgrade:
            command.append("--upgrade")
        command.extend(packages)
        return tuple(command)

    uv = shutil.which("uv")
    if uv is not None:
        command = [uv, "pip", "install", "--python", python_executable]
        if upgrade:
            command.append("--upgrade")
        command.extend(packages)
        return tuple(command)

    command = [python_executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if upgrade:
        command.append("--upgrade")
    command.extend(packages)
    return tuple(command)


def _python_module_available(python: str, module: str) -> bool:
    with suppress(OSError, subprocess.TimeoutExpired):
        completed = subprocess.run(
            [python, "-m", module, "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        return completed.returncode == 0
    return False


def _run_command(command: Sequence[str], timeout_sec: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_sec,
    )


def _completed_ok(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, stdout="", stderr="")


def _join_output(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def os_environ(key: str) -> str | None:
    import os

    return os.environ.get(key)


def _validate_package_name(package: str) -> str:
    cleaned = package.strip()
    if not cleaned:
        raise ValueError("package name must not be empty")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-<>=!~")
    if any(ch not in allowed for ch in cleaned):
        raise ValueError(f"unsafe package specifier: {package!r}")
    return cleaned


__all__ = [
    "LOCAL_RUNTIME_COMMANDS",
    "LOCAL_RUNTIME_PACKAGES",
    "RUNTIME_HOME_ENV",
    "RUNTIME_VENV_ENV",
    "BootstrapResult",
    "ensure_local_runtime",
]
