"""PATH-independent resolution for forgetforge and hermes console scripts."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from cluxion_runtime.core import hybrid_forget, llm_compress


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\necho ok\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def isolated_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp venv bin dir; PATH excludes it so only sys.executable dir can resolve bins."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    return bin_dir


def test_forgetforge_available_via_venv_bin_without_path(
    isolated_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forgetforge = isolated_bin_dir / "forgetforge"
    _write_executable(forgetforge)
    monkeypatch.setattr(hybrid_forget, "_FORGETFORGE_BIN", hybrid_forget._resolve_bin("forgetforge"))

    assert hybrid_forget.forgetforge_available() is True
    assert os.path.isabs(hybrid_forget._FORGETFORGE_BIN)
    assert str(forgetforge) == hybrid_forget._FORGETFORGE_BIN


def test_forgetforge_unavailable_when_missing_everywhere(
    isolated_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hybrid_forget, "_FORGETFORGE_BIN", hybrid_forget._resolve_bin("forgetforge"))
    assert hybrid_forget.forgetforge_available() is False


def test_forgetforge_available_via_path_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "on-path"
    bin_dir.mkdir()
    forgetforge = bin_dir / "forgetforge"
    _write_executable(forgetforge)

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv" / "bin" / "python"))
    monkeypatch.setattr(hybrid_forget, "_FORGETFORGE_BIN", hybrid_forget._resolve_bin("forgetforge"))

    assert hybrid_forget.forgetforge_available() is True
    assert str(forgetforge) == hybrid_forget._FORGETFORGE_BIN


def test_hermes_available_via_venv_bin_without_path(
    isolated_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes = isolated_bin_dir / "hermes"
    _write_executable(hermes)
    monkeypatch.setattr(llm_compress, "_HERMES_BIN", llm_compress._resolve_bin("hermes"))

    assert llm_compress.hermes_available() is True
    assert os.path.isabs(llm_compress._HERMES_BIN)
    assert str(hermes) == llm_compress._HERMES_BIN


def test_hermes_unavailable_when_missing_everywhere(
    isolated_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_compress, "_HERMES_BIN", llm_compress._resolve_bin("hermes"))
    assert llm_compress.hermes_available() is False


def test_hermes_available_via_path_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "on-path"
    bin_dir.mkdir()
    hermes = bin_dir / "hermes"
    _write_executable(hermes)

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv" / "bin" / "python"))
    monkeypatch.setattr(llm_compress, "_HERMES_BIN", llm_compress._resolve_bin("hermes"))

    assert llm_compress.hermes_available() is True
    assert str(hermes) == llm_compress._HERMES_BIN