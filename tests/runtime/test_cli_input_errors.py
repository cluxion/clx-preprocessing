from __future__ import annotations

import io
import json

from cluxion_runtime import cli


def _run(argv: list[str], stdin_text: str, capsys, monkeypatch) -> tuple[int, dict]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = cli.main(argv)
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


def test_plan_bad_json_stdin_returns_structured_error(capsys, monkeypatch) -> None:
    code, payload = _run(["plan", "--surface", "codex", "--json-stdin"], "{bad json", capsys, monkeypatch)
    assert code == 2
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    assert "hint" in payload


def test_guard_bad_json_stdin_returns_structured_error(capsys, monkeypatch) -> None:
    code, payload = _run(["guard", "--json-stdin"], "not json at all", capsys, monkeypatch)
    assert code == 2
    assert payload["error"] == "invalid_input"


def test_guard_owned_roots_paths_rejected_with_hint(capsys, monkeypatch) -> None:
    body = json.dumps({"action": "status", "owned_roots": ["/tmp/some/path"]})
    code, payload = _run(["guard", "--json-stdin"], body, capsys, monkeypatch)
    assert code == 2
    assert payload["error"] == "invalid_input"
    assert "PIDs" in payload["hint"]


def test_plan_negative_context_tokens_rejected(capsys, monkeypatch) -> None:
    code, payload = _run(
        ["plan", "--surface", "codex", "--prompt", "x", "--context-tokens", "-5"], "", capsys, monkeypatch
    )
    assert code == 2
    assert payload["error"] == "invalid_input"
