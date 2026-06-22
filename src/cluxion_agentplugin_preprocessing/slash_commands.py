"""In-session slash commands for Hermes (/loopauto, /cluxion-doctor)."""

from __future__ import annotations

import json
from pathlib import Path

from cluxion_agentplugin_preprocessing import runner
from cluxion_agentplugin_preprocessing.doctor import render_text, run_doctor
from cluxion_agentplugin_preprocessing.doctor.probes import PROBES

LOOPAUTO_HELP = """\
/loopauto <prompt> — Autonomous queue drain (loopAuto)

Plans the task, stores segment content in the Rust queue, then loops:
  queue_next → hermes -z → queue_record → … → briefing

Examples:
  /loopauto implement every REQ line and record evidence
  /loopauto refactor auth module with tests

Notes:
  - Equivalent to prefixing a prompt with /loopAuto on cluxion_plan
  - Disable auto-loop: export CLUXION_LOOP_AUTO=0
  - Diagnostics only: add loop_auto_dry_run via cluxion_plan tool
"""

CLUXION_DOCTOR_HELP = "/cluxion-doctor — Run preprocessing plugin health checks (doctor)."


def register_slash_commands(ctx: object) -> None:
    register = getattr(ctx, "register_command", None)
    if not callable(register):
        return
    register(
        "loopauto",
        handle_loopauto,
        description="Autonomous queue drain (loopAuto) — plan + Hermes oneshot segment loop",
        args_hint="<prompt>",
    )
    register(
        "cluxion-doctor",
        handle_cluxion_doctor,
        description="Run cluxion preprocessing plugin doctor checks",
    )


def handle_loopauto(raw_args: str) -> str:
    prompt = raw_args.strip()
    if not prompt or prompt.lower() in {"help", "-h", "--help"}:
        return LOOPAUTO_HELP
    try:
        result = runner.plan(
            {
                "prompt": f"/loopAuto {prompt}",
                "cwd": str(Path.cwd()),
                "clarification_answers": "confirmed via /loopauto slash command",
            }
        )
        return _format_plan_response(result.to_json())
    except Exception as exc:
        return f"loopauto error: {exc}"


def handle_cluxion_doctor(raw_args: str) -> str:
    if raw_args.strip().lower() in {"help", "-h", "--help"}:
        return CLUXION_DOCTOR_HELP
    import importlib.resources

    from cluxion_agentplugin_preprocessing import __version__
    from cluxion_agentplugin_preprocessing.doctor.framework import load_catalog

    pkg = "cluxion_agentplugin_preprocessing.doctor"
    catalog_path = Path(str(importlib.resources.files(pkg).joinpath("catalog.json")))
    result = run_doctor(
        cwd=Path.cwd(),
        hermes_bin="hermes",
        catalog_path=catalog_path,
        probes=PROBES,
        plugin="preprocessing",
        version=__version__,
    )
    return render_text(result, load_catalog(catalog_path))


def _format_plan_response(raw_json: str) -> str:
    payload = json.loads(raw_json)
    if not payload.get("ok"):
        return f"loopauto failed: {payload.get('error', 'unknown error')}"
    body = payload.get("result", payload)
    if not isinstance(body, dict):
        return str(body)
    loop = body.get("loop_auto")
    if isinstance(loop, dict):
        lines = [
            f"[loopauto] ok={loop.get('ok')} status={loop.get('status')}",
            f"segments_processed={loop.get('segments_processed')} "
            f"segments_failed={loop.get('segments_failed')} "
            f"duration_ms={loop.get('duration_ms')}",
        ]
        if loop.get("error"):
            lines.append(f"error: {loop['error']}")
        briefing = str(loop.get("briefing_answer", "")).strip()
        if briefing:
            lines.extend(["", briefing])
        return "\n".join(lines)
    host = body.get("host_execution")
    if isinstance(host, dict) and host.get("queue_required"):
        return (
            "Plan queued but loop_auto did not run. "
            "Check CLUXION_LOOP_AUTO=1 and hermes on PATH (cluxion-preprocess doctor)."
        )
    return json.dumps(body, ensure_ascii=False, indent=2)[:8000]


__all__ = [
    "LOOPAUTO_HELP",
    "handle_cluxion_doctor",
    "handle_loopauto",
    "register_slash_commands",
]