from __future__ import annotations

import io
import json
import os

import pytest

from cluxion_runtime import cli
from cluxion_runtime.resources import guard_bridge


def _run(argv: list[str], stdin_text: str, capsys, monkeypatch) -> tuple[int, dict, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out.strip()), captured.err


def test_plan_bad_json_stdin_returns_structured_error(capsys, monkeypatch) -> None:
    code, payload, stderr = _run(["plan", "--surface", "codex", "--json-stdin"], "{bad json", capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    assert "hint" in payload


def test_guard_bad_json_stdin_returns_structured_error(capsys, monkeypatch) -> None:
    code, payload, stderr = _run(["guard", "--json-stdin"], "not json at all", capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["error"] == "invalid_input"


def test_guard_owned_roots_paths_rejected_with_hint(capsys, monkeypatch) -> None:
    body = json.dumps({"action": "status", "owned_roots": ["/tmp/some/path"]})
    code, payload, stderr = _run(["guard", "--json-stdin"], body, capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["error"] == "invalid_input"
    assert "PIDs" in payload["hint"]


def test_plan_negative_context_tokens_rejected(capsys, monkeypatch) -> None:
    code, payload, stderr = _run(
        ["plan", "--surface", "codex", "--prompt", "x", "--context-tokens", "-5"], "", capsys, monkeypatch
    )
    assert code == 2
    assert stderr == ""
    assert payload["error"] == "invalid_input"


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": ""},
        {"prompt": "x", "surface": "bogus"},
        {"prompt": "x", "priority": "urgent"},
        {"prompt": "x", "expected_ram_mb": "9" * 5000},
    ],
)
def test_plan_json_domain_value_errors_return_stdout_json(body: dict[str, object], capsys, monkeypatch) -> None:
    code, payload, stderr = _run(["plan", "--surface", "codex", "--json-stdin"], json.dumps(body), capsys, monkeypatch)
    assert code == 1
    assert stderr == ""
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    assert payload["message"]
    assert payload["hint"]


@pytest.mark.parametrize("field", ["expected_ram_mb", "context_tokens"])
def test_plan_json_negative_numbers_are_rejected(field: str, capsys, monkeypatch) -> None:
    code, payload, stderr = _run(
        ["plan", "--surface", "codex", "--json-stdin"],
        json.dumps({"prompt": "x", field: -1}),
        capsys,
        monkeypatch,
    )
    assert code == 2
    assert stderr == ""
    assert payload["error"] == "invalid_input"
    assert field in payload["message"]


@pytest.mark.parametrize(
    ("body", "patch_target"),
    [
        ({"action": "start", "interval_ms": -1}, "start_daemon"),
        ({"action": "start", "window": -1}, "start_daemon"),
        ({"action": "enforce", "owned_roots": [os.getpid()], "cpu_threshold": -1}, "enforce"),
        ({"action": "enforce", "owned_roots": [os.getpid()], "rss_threshold_mb": -1}, "enforce"),
        ({"action": "auto-enforce", "owned_roots": [os.getpid()], "sustained_cpu": -1}, "auto_enforce"),
        ({"action": "auto-enforce", "owned_roots": [os.getpid()], "ram_floor_mb": -1}, "auto_enforce"),
        ({"action": "auto-enforce", "owned_roots": [os.getpid()], "min_samples": -1}, "auto_enforce"),
    ],
)
def test_guard_json_negative_numbers_are_rejected(
    body: dict[str, object], patch_target: str, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(guard_bridge, patch_target, lambda **_: pytest.fail(f"{patch_target} should not run"))
    code, payload, stderr = _run(["guard", "--json-stdin"], json.dumps(body), capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["error"] == "invalid_input"


def test_guard_unknown_action_error_uses_stdout(capsys, monkeypatch) -> None:
    code, payload, stderr = _run(
        ["guard", "--json-stdin"], json.dumps({"action": "explode"}), capsys, monkeypatch
    )
    assert code == 1
    assert stderr == ""
    assert payload["ok"] is False
    assert "unknown guard action" in payload["error"]


def test_guard_start_readonly_store_returns_structured_error(tmp_path, capsys, monkeypatch) -> None:
    store = tmp_path / "guard"
    store.mkdir()
    store.chmod(0o555)
    monkeypatch.setenv(guard_bridge.GUARD_STORE_ENV, str(store))
    monkeypatch.setattr(guard_bridge.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("daemon spawned"))
    try:
        code, payload, stderr = _run(["guard", "--json-stdin"], json.dumps({"action": "start"}), capsys, monkeypatch)
    finally:
        store.chmod(0o755)
    assert code == 1
    assert stderr == ""
    assert payload["error"] == "guard_store_unwritable"
    assert payload["hint"]


def test_queue_next_bounds_long_fields() -> None:
    from cluxion_runtime.cli import _bounded_step_payload

    payload = {"prompt": "x" * 5000, "step": {"content": "y" * 5000}, "id": "w1"}
    bounded = _bounded_step_payload(payload, full=False)
    assert len(bounded["prompt"]) == 2000
    assert bounded["truncated"] is True
    assert len(bounded["step"]["content"]) == 2000
    assert _bounded_step_payload(payload, full=True) == payload


def test_plan_deeply_nested_json_returns_structured_error(capsys, monkeypatch) -> None:
    # adversarial: ~10k nesting must be a clean error, not a raw RecursionError traceback
    deep = "{\"x\":" + "[" * 10000 + "]" * 10000 + "}"
    code, payload, stderr = _run(["plan", "--surface", "codex", "--json-stdin"], deep, capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    assert "nesting too deep" in payload["message"]


@pytest.mark.parametrize(
    ("argv", "body"),
    [
        (["plan", "--surface", "codex", "--json-stdin"], {"prompt": "x", "loop_auto_timeout_s": {"s": 600}}),
        (["loop-auto", "--work-id", "w1", "--json-stdin"], {"timeout_seconds": {"s": 600}}),
        (["loop-auto", "--work-id", "w1", "--json-stdin"], {"max_segment_retries": [2]}),
    ],
)
def test_wrong_typed_numeric_payload_fields_return_structured_error(
    argv: list[str], body: dict[str, object], capsys, monkeypatch
) -> None:
    # regression: dict/list numeric fields raised TypeError past main()'s
    # except clause, violating the JSON error contract with a raw traceback
    code, payload, stderr = _run(argv, json.dumps(body), capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
