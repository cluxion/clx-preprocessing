from __future__ import annotations

import subprocess
from pathlib import Path

from cluxion_agentplugin_preprocessing import hermes_deliver_patch


def _applied_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    (root / "hermes_cli").mkdir(parents=True)
    (root / "tui_gateway").mkdir()
    (root / "hermes_cli" / "plugins.py").write_text(
        'deliver: str = "output"\n"deliver": deliver_mode\n',
        encoding="utf-8",
    )
    (root / "cli.py").write_text(
        'entry.get("deliver") == "agent"\n_pending_input.put(str(result))\n',
        encoding="utf-8",
    )
    (root / "tui_gateway" / "server.py").write_text(
        '"type": "send"\nentry.get("deliver") == "agent"\n'
        "agent-deliver command: use command.dispatch\n",
        encoding="utf-8",
    )
    return root


def _mismatched_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    (root / "hermes_cli").mkdir(parents=True)
    (root / "tui_gateway").mkdir()
    (root / "hermes_cli" / "plugins.py").write_text("new hermes plugin api\n", encoding="utf-8")
    (root / "cli.py").write_text("new hermes cli api\n", encoding="utf-8")
    (root / "tui_gateway" / "server.py").write_text("new hermes gateway api\n", encoding="utf-8")
    return root


def test_patch_status_applied_on_marked_tree(tmp_path: Path) -> None:
    status = hermes_deliver_patch.patch_status(_applied_root(tmp_path))

    assert status.status == "applied"
    assert status.changed is False


def test_ensure_applied_idempotent_on_marked_tree(tmp_path: Path) -> None:
    result = hermes_deliver_patch.ensure_applied(hermes_root=_applied_root(tmp_path))

    assert result.status == "applied"
    assert result.changed is False
    assert result.method == "noop"


def test_ensure_applied_reports_anchor_mismatch(tmp_path: Path) -> None:
    result = hermes_deliver_patch.ensure_applied(hermes_root=_mismatched_root(tmp_path))

    assert result.status == "anchors-mismatch"
    assert result.applied is False


def test_ensure_applied_reports_git_timeout(tmp_path: Path, monkeypatch) -> None:
    root = _mismatched_root(tmp_path)
    (root / ".git").mkdir()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(hermes_deliver_patch.subprocess, "run", timeout)

    result = hermes_deliver_patch.ensure_applied(hermes_root=root)

    assert result.status == "timeout"
    assert result.applied is False
    assert "git rev-parse" in result.detail
    assert "60s" in result.detail
