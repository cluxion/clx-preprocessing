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


def test_guard_cpu_sample_ms_u64_overflow_structured_invalid_input(capsys, monkeypatch) -> None:
    def raise_value(_payload=None):
        raise ValueError("cpu_sample_ms exceeds u64")

    monkeypatch.setattr(guard_bridge, "sample", raise_value)
    monkeypatch.setattr(guard_bridge, "daemon_status", lambda **_: {"running": False, "pid": None})
    monkeypatch.setattr(guard_bridge, "read_daemon_state", lambda **_: None)
    body = json.dumps({"action": "status", "cpu_sample_ms": (1 << 64)})
    code, payload, stderr = _run(["guard", "--json-stdin"], body, capsys, monkeypatch)
    assert code == 1
    assert stderr == ""
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    assert "traceback" not in str(payload.get("message", "")).lower()


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
    # (3.12: json.loads RecursionError; 3.14+: iterative depth gate after loads)
    deep = "{\"x\":" + "[" * 10000 + "]" * 10000 + "}"
    code, payload, stderr = _run(["plan", "--surface", "codex", "--json-stdin"], deep, capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    assert "nesting too deep" in payload["message"]
    assert payload["hint"] == "reduce the nesting depth of the JSON payload"


def _dict_nest(depth: int) -> dict[str, object]:
    """Build exactly `depth` nested dict containers (iterative; leaf is a scalar)."""
    node: object = "leaf"
    for _ in range(depth):
        node = {"k": node}
    assert isinstance(node, dict)
    return node


def _mixed_nest(depth: int) -> object:
    """Build exactly `depth` alternating list/dict containers (iterative)."""
    node: object = "leaf"
    for i in range(depth):
        node = [node] if i % 2 == 0 else {"k": node}
    return node


def test_json_container_depth_128_accepted(monkeypatch) -> None:
    body = _dict_nest(128)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(body)))
    got = cli._payload_from_stdin()
    assert isinstance(got, dict)
    assert "k" in got


def test_json_container_depth_129_rejected(monkeypatch) -> None:
    body = _dict_nest(129)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(body)))
    with pytest.raises(cli.PayloadError) as caught:
        cli._payload_from_stdin()
    assert str(caught.value) == "stdin JSON nesting too deep"
    assert caught.value.hint == "reduce the nesting depth of the JSON payload"


def test_json_container_depth_mixed_dict_list_boundary(monkeypatch) -> None:
    # depth 128 mixed containers accepted; 129 rejected (walks only dict/list)
    ok_body = {"root": _mixed_nest(127)}  # root dict + 127 = 128
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ok_body)))
    assert isinstance(cli._payload_from_stdin(), dict)

    bad_body = {"root": _mixed_nest(128)}  # root dict + 128 = 129
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bad_body)))
    with pytest.raises(cli.PayloadError) as caught:
        cli._payload_from_stdin()
    assert str(caught.value) == "stdin JSON nesting too deep"


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


@pytest.mark.parametrize(
    ("argv", "body", "field"),
    [
        (["loop-auto", "--work-id", "w1", "--json-stdin"], {"timeout_seconds": "nan"}, "timeout_seconds"),
        (["loop-auto", "--work-id", "w1", "--json-stdin"], {"timeout_seconds": "inf"}, "timeout_seconds"),
        (["loop-auto", "--work-id", "w1", "--json-stdin"], {"timeout_seconds": "-inf"}, "timeout_seconds"),
        (["loop-auto", "--work-id", "w1", "--json-stdin"], {"timeout_seconds": 0}, "timeout_seconds"),
        (["loop-auto", "--work-id", "w1", "--json-stdin"], {"timeout_seconds": 0.0}, "timeout_seconds"),
        (
            ["plan", "--surface", "codex", "--json-stdin"],
            {"prompt": "x", "loop_auto_timeout_s": "nan"},
            "loop_auto_timeout_s",
        ),
        (
            ["plan", "--surface", "codex", "--json-stdin"],
            {"prompt": "x", "loop_auto_timeout_s": "inf"},
            "loop_auto_timeout_s",
        ),
        (
            ["plan", "--surface", "codex", "--json-stdin"],
            {"prompt": "x", "loop_auto_timeout_s": "-inf"},
            "loop_auto_timeout_s",
        ),
        (
            ["plan", "--surface", "codex", "--json-stdin"],
            {"prompt": "x", "loop_auto_timeout_s": 0},
            "loop_auto_timeout_s",
        ),
    ],
)
def test_loop_auto_timeout_non_finite_or_zero_returns_invalid_input(
    argv: list[str], body: dict[str, object], field: str, capsys, monkeypatch
) -> None:
    # Cycle 97 PP: NaN/Inf/0 must be structured invalid_input (exit 2), not dispatch_error
    code, payload, stderr = _run(argv, json.dumps(body), capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    assert field in payload["message"]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "enforce", "owned_roots": [os.getpid()], "cpu_threshold": "nan"},
        {"action": "enforce", "owned_roots": [os.getpid()], "cpu_threshold": "NaN"},
        {"action": "enforce", "owned_roots": [os.getpid()], "cpu_threshold": "inf"},
        {"action": "enforce", "owned_roots": [os.getpid()], "cpu_threshold": "-inf"},
        {"action": "enforce", "owned_roots": [os.getpid()], "grace_seconds": "nan"},
        {"action": "auto-enforce", "owned_roots": [os.getpid()], "sustained_cpu": "inf"},
    ],
)
def test_guard_non_finite_float_fields_rejected(body: dict[str, object], capsys, monkeypatch) -> None:
    monkeypatch.setattr(guard_bridge, "enforce", lambda *_a, **_k: pytest.fail("enforce should not run"))
    monkeypatch.setattr(guard_bridge, "auto_enforce", lambda *_a, **_k: pytest.fail("auto_enforce should not run"))
    code, payload, stderr = _run(["guard", "--json-stdin"], json.dumps(body), capsys, monkeypatch)
    assert code == 2
    assert stderr == ""
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"


def test_guard_zero_cpu_threshold_and_grace_accepted(capsys, monkeypatch) -> None:
    # 0 remains valid for CPU thresholds and grace (unspecified / disabled semantics)
    calls: list[dict[str, object]] = []

    def _enforce(*_args: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"ok": True, "action": "enforce"}

    monkeypatch.setattr(guard_bridge, "enforce", _enforce)
    code, payload, stderr = _run(
        ["guard", "--json-stdin"],
        json.dumps(
            {
                "action": "enforce",
                "owned_roots": [os.getpid()],
                "cpu_threshold": 0,
                "grace_seconds": 0,
            }
        ),
        capsys,
        monkeypatch,
    )
    assert code == 0
    assert stderr == ""
    assert payload["ok"] is True
    assert calls and calls[0]["cpu_threshold"] == 0.0
    assert calls[0]["grace_seconds"] == 0.0
