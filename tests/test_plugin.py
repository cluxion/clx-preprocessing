from __future__ import annotations

import json
import sys
from importlib import util
from pathlib import Path

from cluxion_agentplugin_preprocessing import plugin


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, object]] = {}

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
        "cluxion_guard",
        "cluxion_hermes_config",
        "cluxion_plan",
        "cluxion_queue_brief",
        "cluxion_queue_next",
        "cluxion_queue_record",
        "cluxion_serve_local",
        "cluxion_web_search",
    ]
    assert {tool["toolset"] for tool in ctx.tools.values()} == {"cluxion"}


def test_handler_returns_json_error_for_missing_model() -> None:
    ctx = FakeContext()
    plugin.register(ctx)
    handler = ctx.tools["cluxion_serve_local"]["handler"]

    result = handler({})
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "model is required" in payload["error"]


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
