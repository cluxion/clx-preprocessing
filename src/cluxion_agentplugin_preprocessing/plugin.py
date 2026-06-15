"""Universal agent plugin entry point (Hermes, Claude, Codex, Grok Build)."""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path
from typing import TYPE_CHECKING

from cluxion_agentplugin_preprocessing import guard_watch, runner
from cluxion_agentplugin_preprocessing.doctor import render_json, run_doctor
from cluxion_agentplugin_preprocessing.schemas import (
    BOOTSTRAP_SCHEMA,
    BROWSER_CLICK_SCHEMA,
    BROWSER_EXTRACT_SCHEMA,
    BROWSER_OPEN_SCHEMA,
    BROWSER_TYPE_SCHEMA,
    CLARIFY_SCHEMA,
    CONTEXT_COMPRESS_SCHEMA,
    GUARD_SCHEMA,
    HERMES_CONFIG_SCHEMA,
    PLAN_SCHEMA,
    QUEUE_BRIEF_SCHEMA,
    QUEUE_NEXT_SCHEMA,
    QUEUE_RECORD_SCHEMA,
    SERVE_LOCAL_SCHEMA,
    WEB_SEARCH_SCHEMA,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def register(ctx: object) -> None:
    """Register Cluxion preprocessing tools with the host agent."""
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("on_session_start", guard_watch.on_session_start)
        register_hook("post_tool_call", guard_watch.post_tool_call)

    ctx.register_tool(
        name="cluxion_plan",
        toolset="cluxion",
        schema=PLAN_SCHEMA,
        handler=_handle_plan,
        check_fn=_check_runtime_available,
        emoji="🧭",
    )
    ctx.register_tool(
        name="cluxion_clarify",
        toolset="cluxion",
        schema=CLARIFY_SCHEMA,
        handler=_handle_clarify,
        check_fn=_check_runtime_available,
        emoji="❓",
    )
    ctx.register_tool(
        name="cluxion_bootstrap",
        toolset="cluxion",
        schema=BOOTSTRAP_SCHEMA,
        handler=_handle_bootstrap,
        check_fn=_check_runtime_available,
        emoji="🔧",
    )
    ctx.register_tool(
        name="cluxion_serve_local",
        toolset="cluxion",
        schema=SERVE_LOCAL_SCHEMA,
        handler=_handle_serve_local,
        check_fn=_check_runtime_available,
        emoji="🖥️",
    )
    ctx.register_tool(
        name="cluxion_hermes_config",
        toolset="cluxion",
        schema=HERMES_CONFIG_SCHEMA,
        handler=_handle_hermes_config,
        check_fn=_check_runtime_available,
        emoji="🔌",
    )
    ctx.register_tool(
        name="cluxion_queue_next",
        toolset="cluxion",
        schema=QUEUE_NEXT_SCHEMA,
        handler=_handle_queue_next,
        check_fn=_check_runtime_available,
        emoji="➡️",
    )
    ctx.register_tool(
        name="cluxion_queue_record",
        toolset="cluxion",
        schema=QUEUE_RECORD_SCHEMA,
        handler=_handle_queue_record,
        check_fn=_check_runtime_available,
        emoji="🧾",
    )
    ctx.register_tool(
        name="cluxion_queue_brief",
        toolset="cluxion",
        schema=QUEUE_BRIEF_SCHEMA,
        handler=_handle_queue_brief,
        check_fn=_check_runtime_available,
        emoji="📌",
    )
    ctx.register_tool(
        name="cluxion_context_compress",
        toolset="cluxion",
        schema=CONTEXT_COMPRESS_SCHEMA,
        handler=_handle_context_compress,
        check_fn=_check_runtime_available,
        emoji="🗜️",
    )
    ctx.register_tool(
        name="cluxion_guard",
        toolset="cluxion",
        schema=GUARD_SCHEMA,
        handler=_handle_guard,
        check_fn=_check_runtime_available,
        emoji="🛡️",
    )
    ctx.register_tool(
        name="cluxion_web_search",
        toolset="cluxion",
        schema=WEB_SEARCH_SCHEMA,
        handler=_handle_web_search,
        check_fn=_check_browser_tool_available,
        emoji="🌐",
    )
    ctx.register_tool(
        name="cluxion_browser_open",
        toolset="cluxion",
        schema=BROWSER_OPEN_SCHEMA,
        handler=_handle_browser_open,
        check_fn=_check_browser_tool_available,
        emoji="🌐",
    )
    ctx.register_tool(
        name="cluxion_browser_extract",
        toolset="cluxion",
        schema=BROWSER_EXTRACT_SCHEMA,
        handler=_handle_browser_extract,
        check_fn=_check_browser_tool_available,
        emoji="🌐",
    )
    ctx.register_tool(
        name="cluxion_browser_click",
        toolset="cluxion",
        schema=BROWSER_CLICK_SCHEMA,
        handler=_handle_browser_click,
        check_fn=_check_browser_tool_available,
        emoji="🌐",
    )
    ctx.register_tool(
        name="cluxion_browser_type",
        toolset="cluxion",
        schema=BROWSER_TYPE_SCHEMA,
        handler=_handle_browser_type,
        check_fn=_check_browser_tool_available,
        emoji="🌐",
    )
    # doctor tool
    DOCTOR_SCHEMA = {
        "name": "cluxion_doctor",
        "description": "Run the embedded deterministic health checks for this plugin",
        "parameters": {
            "type": "object",
            "properties": {"verbose": {"type": "boolean"}},
            "additionalProperties": False,
        },
    }
    ctx.register_tool(
        name="cluxion_doctor",
        toolset="cluxion",
        schema=DOCTOR_SCHEMA,
        handler=_handle_doctor,
        check_fn=_check_runtime_available,
        emoji="🩺",
    )


def _check_runtime_available() -> bool:
    return runner.runtime_available()


def _check_browser_tool_available() -> bool:
    return True


def _handle_plan(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.plan(args).to_json())


def _handle_clarify(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.plan(args).to_json())


def _handle_bootstrap(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.bootstrap(args).to_json())


def _handle_serve_local(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.serve_local(args).to_json())


def _handle_hermes_config(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.hermes_config(args).to_json())


def _handle_queue_next(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.queue_next(args).to_json())


def _handle_queue_record(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.queue_record(args).to_json())


def _handle_queue_brief(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.queue_brief(args).to_json())


def _handle_context_compress(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.context_compress(args).to_json())


def _handle_guard(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.guard(args).to_json())


def _handle_web_search(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.web_search(args).to_json())


def _handle_browser_open(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.browser_open(args).to_json())


def _handle_browser_extract(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.browser_extract(args).to_json())


def _handle_browser_click(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.browser_click(args).to_json())


def _handle_browser_type(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: runner.browser_type(args).to_json())


def _handle_doctor(args: dict[str, object], **_: object) -> str:
    return _json_result(lambda: _run_doctor(args))


def _run_doctor(args: dict[str, object]) -> str:
    pkg = "cluxion_agentplugin_preprocessing.doctor"
    catalog_path = Path(str(importlib.resources.files(pkg).joinpath("catalog.json")))
    result = run_doctor(
        cwd=Path.cwd(),
        hermes_bin="hermes",
        catalog_path=catalog_path,
        probes=__import__(
            "cluxion_agentplugin_preprocessing.doctor.probes", fromlist=["PROBES"]
        ).PROBES,
        plugin="preprocessing",
        version=__import__("cluxion_agentplugin_preprocessing").__version__,
    )
    return render_json(result)


def _json_result(callback: Callable[[], str]) -> str:
    try:
        return callback()
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True)


__all__ = ["register"]
