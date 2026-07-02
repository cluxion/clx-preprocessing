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


def test_queue_next_bounds_long_fields() -> None:
    from cluxion_runtime.cli import _bounded_step_payload

    payload = {"prompt": "x" * 5000, "step": {"content": "y" * 5000}, "id": "w1"}
    bounded = _bounded_step_payload(payload, full=False)
    assert len(bounded["prompt"]) == 2000
    assert bounded["truncated"] is True
    assert len(bounded["step"]["content"]) == 2000
    assert _bounded_step_payload(payload, full=True) == payload
