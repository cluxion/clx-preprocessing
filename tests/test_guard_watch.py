from __future__ import annotations

from typing import Any

import pytest

from cluxion_agentplugin_preprocessing import guard_watch, plugin


@pytest.fixture(autouse=True)
def reset_guard_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard_watch, "_last_watch_at", None)
    monkeypatch.setattr(guard_watch, "_last_warning_at", None)
    monkeypatch.delenv(guard_watch.AUTOSTART_ENV, raising=False)
    monkeypatch.delenv(guard_watch.AUTO_APPLY_ENV, raising=False)
    monkeypatch.delenv(guard_watch.WATCH_INTERVAL_ENV, raising=False)


class HookContext:
    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}
        self.tools: dict[str, object] = {}

    def register_hook(self, name: str, handler: object) -> None:
        self.hooks[name] = handler

    def register_tool(self, *, name: str, **kwargs: object) -> None:
        self.tools[name] = kwargs


class ToolOnlyContext:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def register_tool(self, *, name: str, **kwargs: object) -> None:
        self.tools[name] = kwargs


def test_register_adds_guard_hooks_when_supported() -> None:
    ctx = HookContext()

    plugin.register(ctx)

    assert ctx.hooks == {
        "on_session_start": guard_watch.on_session_start,
        "post_tool_call": guard_watch.post_tool_call,
    }
    assert "cluxion_guard" in ctx.tools


def test_register_tolerates_context_without_hooks() -> None:
    ctx = ToolOnlyContext()

    plugin.register(ctx)

    assert "cluxion_guard" in ctx.tools


def test_autostart_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_start_daemon() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True, "started": False, "reason": "already_running"}

    monkeypatch.setattr(guard_watch.guard_bridge, "start_daemon", fake_start_daemon)

    guard_watch.on_session_start(session_id="s1", telemetry_schema_version=1)

    assert calls == 1


@pytest.mark.parametrize("value", ["0", "false", "False"])
def test_autostart_env_gate_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    calls = 0

    def fake_start_daemon() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    monkeypatch.setenv(guard_watch.AUTOSTART_ENV, value)
    monkeypatch.setattr(guard_watch.guard_bridge, "start_daemon", fake_start_daemon)

    guard_watch.on_session_start(session_id="s1", telemetry_schema_version=1)

    assert calls == 0


def test_autostart_exception_is_swallowed_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_start_daemon() -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(guard_watch.guard_bridge, "start_daemon", fake_start_daemon)

    guard_watch.on_session_start(session_id="s1", telemetry_schema_version=1)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cluxion guard autostart failed: boom" in captured.err


def test_watch_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    calls: list[dict[str, Any]] = []

    def fake_monotonic() -> float:
        return now

    def fake_auto_enforce(owned_roots: list[int], *, dry_run: bool) -> dict[str, object]:
        calls.append({"owned_roots": owned_roots, "dry_run": dry_run})
        return {"ok": True, "triggered": False, "dry_run": dry_run}

    monkeypatch.setattr(guard_watch.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(guard_watch.guard_bridge, "auto_enforce", fake_auto_enforce)

    guard_watch.post_tool_call(tool_name="terminal", telemetry_schema_version=1)
    guard_watch.post_tool_call(tool_name="terminal", telemetry_schema_version=1)
    now = 131.0
    guard_watch.post_tool_call(tool_name="terminal", telemetry_schema_version=1)

    assert len(calls) == 2
    assert calls[0]["dry_run"] is True
    assert calls[1]["dry_run"] is True


def test_triggered_dry_run_warning_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = 100.0

    def fake_monotonic() -> float:
        return now

    def fake_auto_enforce(owned_roots: list[int], *, dry_run: bool) -> dict[str, object]:
        return {
            "ok": True,
            "triggered": True,
            "dry_run": dry_run,
            "candidates": [{"pid": 123}, {"pid": 456}],
            "trigger_reasons": ["cpu_avg 99.0 >= sustained_cpu 85.0"],
        }

    monkeypatch.setenv(guard_watch.WATCH_INTERVAL_ENV, "0")
    monkeypatch.setattr(guard_watch.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(guard_watch.guard_bridge, "auto_enforce", fake_auto_enforce)

    guard_watch.post_tool_call(tool_name="terminal", telemetry_schema_version=1)
    guard_watch.post_tool_call(tool_name="terminal", telemetry_schema_version=1)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("cluxion guard triggered") == 1
    assert "candidates=123,456" in captured.err
    assert "cpu_avg 99.0 >= sustained_cpu 85.0" in captured.err


def test_auto_apply_passes_dry_run_false(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_auto_enforce(owned_roots: list[int], *, dry_run: bool) -> dict[str, object]:
        calls.append({"owned_roots": owned_roots, "dry_run": dry_run})
        return {"ok": True, "triggered": False, "dry_run": dry_run}

    monkeypatch.setenv(guard_watch.AUTO_APPLY_ENV, "true")
    monkeypatch.setattr(guard_watch.guard_bridge, "auto_enforce", fake_auto_enforce)

    guard_watch.post_tool_call(session_id="s1", telemetry_schema_version=1)

    assert calls == [{"owned_roots": [guard_watch.os.getpid()], "dry_run": False}]
