from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cluxion_runtime.bootstrap import RUNTIME_VENV_ENV, ensure_local_runtime

if TYPE_CHECKING:
    from collections.abc import Sequence


def test_bootstrap_dry_run_reports_install_command(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv(RUNTIME_VENV_ENV, str(runtime))
    monkeypatch.setattr("shutil.which", lambda command: "/usr/local/bin/uv" if command == "uv" else None)

    result = ensure_local_runtime(dry_run=True)

    assert result.ok is True
    assert result.changed is False
    assert result.reason == "missing_commands:vllm-mlx"
    assert result.install_command[-1] == "vllm-mlx"
    assert result.setup_command == ("/usr/local/bin/uv", "venv", "--seed", str(runtime))
    assert result.command_paths == {"vllm-mlx": str(runtime / "bin" / "vllm-mlx")}


def test_bootstrap_runs_installer_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    calls: list[list[str]] = []

    def fake_which(command: str) -> str | None:
        if command == "uv":
            return "/usr/local/bin/uv"
        return None

    def fake_runner(command: Sequence[str], timeout_sec: float) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        assert timeout_sec == 900.0
        if command[:2] == ("/usr/local/bin/uv", "venv"):
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin" / "python").write_text("", encoding="utf-8")
        if command[:3] == ("/usr/local/bin/uv", "pip", "install"):
            (runtime / "bin").mkdir(parents=True, exist_ok=True)
            (runtime / "bin" / "vllm-mlx").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setenv(RUNTIME_VENV_ENV, str(runtime))
    monkeypatch.setattr("cluxion_runtime.bootstrap._python_module_available", lambda _python, _module: False)

    result = ensure_local_runtime(command_runner=fake_runner)

    assert result.ok is True
    assert result.changed is True
    assert result.reason == "installed"
    assert calls == [
        ["/usr/local/bin/uv", "venv", "--seed", str(runtime)],
        ["/usr/local/bin/uv", "pip", "install", "--python", str(runtime / "bin" / "python"), "vllm-mlx"],
    ]


def test_bootstrap_skips_install_when_runtime_command_exists(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "python").write_text("", encoding="utf-8")
    (runtime / "bin" / "vllm-mlx").write_text("", encoding="utf-8")

    monkeypatch.setenv(RUNTIME_VENV_ENV, str(runtime))
    monkeypatch.setattr("cluxion_runtime.bootstrap._python_module_available", lambda _python, _module: True)

    result = ensure_local_runtime(dry_run=False)

    assert result.ok is True
    assert result.changed is False
    assert result.reason == "already_available"
    assert result.command_paths == {"vllm-mlx": str(runtime / "bin" / "vllm-mlx")}


def test_bootstrap_falls_back_to_uv_when_pip_module_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    calls: list[list[str]] = []

    def fake_which(command: str) -> str | None:
        if command == "uv":
            return "/usr/local/bin/uv"
        return None

    def fake_runner(command: Sequence[str], timeout_sec: float) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command[:2] == ("/usr/local/bin/uv", "venv"):
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin" / "python").write_text("", encoding="utf-8")
        if command[:3] == ("/usr/local/bin/uv", "pip", "install"):
            (runtime / "bin" / "vllm-mlx").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setenv(RUNTIME_VENV_ENV, str(runtime))
    monkeypatch.setattr("cluxion_runtime.bootstrap._python_module_available", lambda _python, _module: False)

    result = ensure_local_runtime(command_runner=fake_runner)

    assert result.ok is True
    assert result.reason == "installed"
    assert calls[-1] == ["/usr/local/bin/uv", "pip", "install", "--python", str(runtime / "bin" / "python"), "vllm-mlx"]


def test_bootstrap_rejects_unsafe_package_name() -> None:
    with pytest.raises(ValueError, match="unsafe package"):
        ensure_local_runtime(packages=("vllm-mlx;rm -rf /",), dry_run=True)
