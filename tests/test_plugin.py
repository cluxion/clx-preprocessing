from __future__ import annotations

import json
import sys
from importlib import util
from pathlib import Path

from cluxion_agentplugin_preprocessing import plugin


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, object]] = {}
        self.commands: dict[str, dict[str, object]] = {}

    def register_command(
        self,
        name: str,
        handler: object,
        description: str = "",
        args_hint: str = "",
    ) -> None:
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, object],
        handler: object,
        check_fn: object,
        emoji: str = "",
    ) -> None:
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "emoji": emoji,
        }


def test_register_adds_expected_tools() -> None:
    ctx = FakeContext()

    plugin.register(ctx)

    assert sorted(ctx.tools) == [
        "cluxion_bootstrap",
        "cluxion_browser_click",
        "cluxion_browser_extract",
        "cluxion_browser_open",
        "cluxion_browser_type",
        "cluxion_clarify",
        "cluxion_context_compress",
        "cluxion_doctor",
        "cluxion_guard",
        "cluxion_hermes_config",
        "cluxion_loop_auto",
        "cluxion_plan",
        "cluxion_queue_brief",
        "cluxion_queue_next",
        "cluxion_queue_record",
        "cluxion_serve_local",
        "cluxion_web_search",
    ]
    assert {tool["toolset"] for tool in ctx.tools.values()} == {"cluxion"}
    assert "loopauto" in ctx.commands
    assert "cluxion-doctor" in ctx.commands
    assert ctx.commands["loopauto"]["args_hint"] == "<prompt>"


def test_handler_returns_json_error_for_missing_model() -> None:
    ctx = FakeContext()
    plugin.register(ctx)
    handler = ctx.tools["cluxion_serve_local"]["handler"]

    result = handler({})
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "model is required" in payload["error"]


def test_handler_returns_json_error_for_type_error() -> None:
    ctx = FakeContext()
    plugin.register(ctx)
    handler = ctx.tools["cluxion_serve_local"]["handler"]

    # Passing None for args (as model might) triggers TypeError inside wrapped handler
    result = handler(None)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert isinstance(payload["error"], str)


def test_directory_plugin_wrapper_exports_register() -> None:
    root = Path(__file__).resolve().parents[1]
    module_path = root / "__init__.py"
    spec = util.spec_from_file_location("cluxion_preprocess_directory_wrapper", module_path)
    assert spec is not None
    assert spec.loader is not None

    module = util.module_from_spec(spec)
    old_path = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path

    assert module.register is plugin.register

def test_browser_check_fn_returns_false_when_playwright_missing(monkeypatch):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "playwright" else object())
    from cluxion_agentplugin_preprocessing.plugin import _check_browser_tool_available
    assert _check_browser_tool_available() is False


def test_plan_schema_loop_auto_timeout_has_exclusive_minimum_zero() -> None:
    # Cycle97 second-boundary: public schema contract (numeric exclusiveMinimum)
    from cluxion_agentplugin_preprocessing.schemas import PLAN_SCHEMA

    prop = PLAN_SCHEMA["parameters"]["properties"]["loop_auto_timeout_s"]
    assert prop["type"] == "number"
    assert prop["exclusiveMinimum"] == 0


def test_loop_auto_schema_timeout_seconds_has_exclusive_minimum_zero() -> None:
    from cluxion_agentplugin_preprocessing.schemas import LOOP_AUTO_SCHEMA

    prop = LOOP_AUTO_SCHEMA["parameters"]["properties"]["timeout_seconds"]
    assert prop["type"] == "number"
    assert prop["exclusiveMinimum"] == 0
