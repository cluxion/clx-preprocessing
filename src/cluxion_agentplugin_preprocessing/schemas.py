"""Hermes tool schemas exposed by the Cluxion plugin."""

from __future__ import annotations

BOOTSTRAP_SCHEMA = {
    "name": "cluxion_bootstrap",
    "description": "Install or upgrade local runtime dependencies such as vllm-mlx.",
    "parameters": {
        "type": "object",
        "properties": {
            "upgrade": {"type": "boolean", "default": False},
            "dry_run": {"type": "boolean", "default": False},
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["vllm-mlx"],
            },
        },
    },
}

CLARIFY_SCHEMA = {
    "name": "cluxion_clarify",
    "description": "Assess whether user direction is clear. Ask blocking questions before queueing work.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Task prompt to assess."},
            "clarification_answers": {"type": "string", "default": ""},
            "cwd": {"type": "string", "default": ""},
        },
        "required": ["prompt"],
    },
}

PLAN_SCHEMA = {
    "name": "cluxion_plan",
    "description": "Plan a task through Cluxion honesty preprocessing, clarification, Rust queue, answer policy, and resource admission.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Task prompt to plan."},
            "priority": {
                "type": "string",
                "enum": ["critical", "high", "normal", "low"],
                "default": "normal",
            },
            "model_route": {
                "type": "string",
                "default": "host/default",
                "description": "Use host/default unless an explicit local route is requested.",
            },
            "expected_ram_mb": {"type": "integer", "minimum": 0, "default": 0},
            "context_tokens": {"type": "integer", "minimum": 0, "default": 0},
            "cwd": {"type": "string", "default": ""},
            "clarification_answers": {
                "type": "string",
                "default": "",
                "description": "Answers that resolve a prior clarification gate; required to enqueue an ambiguous or large task.",
            },
            "loop_auto": {
                "type": "boolean",
                "default": False,
                "description": "After a queued plan is stored, autonomously drain segments via Hermes oneshot calls. Explicit opt-in only.",
            },
            "loop_auto_dry_run": {
                "type": "boolean",
                "default": False,
                "description": "Simulate Hermes segment execution without calling the hermes binary.",
            },
            "loop_auto_timeout_s": {
                "type": "number",
                "exclusiveMinimum": 0,
                "default": 600,
                "description": "Maximum seconds for the full autonomous drain loop.",
            },
            "hermes_bin": {
                "type": "string",
                "default": "hermes",
                "description": "Hermes CLI binary used for each segment oneshot.",
            },
            "model": {
                "type": "string",
                "default": "",
                "description": "Optional Hermes model override (-m) for segment oneshots.",
            },
        },
        "required": ["prompt"],
    },
}

SERVE_LOCAL_SCHEMA = {
    "name": "cluxion_serve_local",
    "description": "Prepare or start a Cluxion-managed vLLM-MLX local model endpoint.",
    "parameters": {
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Local model id, usually an mlx-community model."},
            "host": {"type": "string", "default": "127.0.0.1"},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 23003},
            "max_tokens": {"type": "integer", "minimum": 1, "default": 128000},
            "auto_install": {"type": "boolean", "default": True},
            "upgrade_runtime": {"type": "boolean", "default": False},
            "start": {
                "type": "boolean",
                "default": False,
                "description": "False returns the command without starting the heavy model server.",
            },
        },
        "required": ["model"],
    },
}

HERMES_CONFIG_SCHEMA = {
    "name": "cluxion_hermes_config",
    "description": "Render Hermes custom provider config for a Cluxion local OpenAI-compatible endpoint.",
    "parameters": {
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Local model id."},
            "host": {"type": "string", "default": "127.0.0.1"},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 23003},
            "context_length": {"type": "integer", "minimum": 1, "default": 131072},
            "provider_key": {"type": "string", "default": "cluxion-local"},
            "display_name": {"type": "string", "default": "Cluxion Local"},
        },
        "required": ["model"],
    },
}

QUEUE_NEXT_SCHEMA = {
    "name": "cluxion_queue_next",
    "description": "Fetch the next queued Cluxion segment so the current Hermes model can process it.",
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {"type": "string"},
        },
        "required": ["work_id"],
    },
}

QUEUE_RECORD_SCHEMA = {
    "name": "cluxion_queue_record",
    "description": "Record the current Hermes model's result for a queued Cluxion segment.",
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {"type": "string"},
            "step_id": {"type": "string"},
            "result": {"type": "string"},
            "error": {"type": "string", "default": ""},
            "failed": {"type": "boolean", "default": False},
        },
        "required": ["work_id", "step_id", "result"],
    },
}

QUEUE_BRIEF_SCHEMA = {
    "name": "cluxion_queue_brief",
    "description": "Build a final briefing prompt after all queued Cluxion segment results are recorded.",
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {"type": "string"},
        },
        "required": ["work_id"],
    },
}

LOOP_AUTO_SCHEMA = {
    "name": "cluxion_loop_auto",
    "description": (
        "Autonomously drain the Cluxion dispatch queue via Hermes oneshot calls. "
        "Use after cluxion_plan queued a durable work bundle, or pass explicit loop_auto=true on plan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {"type": "string", "description": "Queued work bundle id from cluxion_plan."},
            "cwd": {"type": "string", "default": ""},
            "hermes_bin": {"type": "string", "default": "hermes"},
            "model": {"type": "string", "default": ""},
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "default": 600},
            "max_segment_retries": {"type": "integer", "minimum": 0, "default": 2},
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "Simulate Hermes without calling the binary (tests and diagnostics).",
            },
        },
        "required": ["work_id"],
    },
}

CONTEXT_COMPRESS_SCHEMA = {
    "name": "cluxion_context_compress",
    "description": (
        "Compress conversation context once usage exceeds the trigger ratio (default 70%) "
        "down to the target ratio (default 30%). Deterministic stages run first "
        "(truncate, dedup, digest); pinned messages, the first user message, and recent "
        "turns are preserved. If stages cannot reach the target, the result includes "
        "ai_summary_request telling the host AI which messages to summarize."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "description": "Conversation messages, oldest first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "default": "user"},
                        "content": {"type": "string"},
                        "pinned": {
                            "type": "boolean",
                            "default": False,
                            "description": "Never compress this message (intent, key decisions).",
                        },
                    },
                    "required": ["content"],
                },
            },
            "model": {
                "type": "string",
                "default": "",
                "description": "Model name for context-window lookup (claude/gemini/gpt/llama).",
            },
            "context_limit_tokens": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Explicit context window in tokens; overrides model lookup.",
            },
            "trigger_ratio": {"type": "number", "default": 0.70},
            "target_ratio": {"type": "number", "default": 0.30},
            "keep_recent_turns": {"type": "integer", "minimum": 0, "default": 4},
        },
        "required": ["messages"],
    },
}

WEB_SEARCH_SCHEMA = {
    "name": "cluxion_web_search",
    "description": (
        "Search the web using the user's own Chrome session (logged-in Google/Naver/Perplexity "
        "accounts, cookies, corporate pages). Read the hint when the browser is unreachable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "engine": {
                "type": "string",
                "enum": ["google", "naver", "duckduckgo", "perplexity"],
                "default": "google",
            },
            "max_links": {"type": "integer", "minimum": 0, "default": 25},
            "max_chars": {"type": "integer", "minimum": 0, "default": 8000},
        },
        "required": ["query"],
    },
}

BROWSER_OPEN_SCHEMA = {
    "name": "cluxion_browser_open",
    "description": (
        "Open a URL in the user's own Chrome session (logged-in accounts, cookies). "
        "Read the hint when the browser is unreachable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP or HTTPS URL to open."},
            "max_chars": {"type": "integer", "minimum": 0, "default": 8000},
        },
        "required": ["url"],
    },
}

BROWSER_EXTRACT_SCHEMA = {
    "name": "cluxion_browser_extract",
    "description": (
        "Extract visible text from the current page in the user's own Chrome session. "
        "Read the hint when the browser is unreachable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Optional CSS selector; omit for full-page extraction.",
            },
            "max_chars": {"type": "integer", "minimum": 0, "default": 8000},
        },
        "required": [],
    },
}

BROWSER_CLICK_SCHEMA = {
    "name": "cluxion_browser_click",
    "description": (
        "Click an element on the current page in the user's own Chrome session. "
        "Read the hint when the browser is unreachable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector for the element to click."},
        },
        "required": ["selector"],
    },
}

BROWSER_TYPE_SCHEMA = {
    "name": "cluxion_browser_type",
    "description": (
        "Type into an input on the current page in the user's own Chrome session. "
        "Read the hint when the browser is unreachable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector for the input element."},
            "text": {"type": "string", "description": "Text to enter."},
            "submit": {
                "type": "boolean",
                "default": False,
                "description": "Press Enter after filling.",
            },
        },
        "required": ["selector", "text"],
    },
}

GUARD_SCHEMA = {
    "name": "cluxion_guard",
    "description": (
        "Real-time resource guard. action=status returns a live "
        "RAM/swap/CPU/zombie sample plus daemon state; pass owned_roots PIDs to "
        "also scan process ownership (fail-closed: only lineage reaching a "
        "registered root is owned, everything else is reported as external). "
        "action=start/stop controls the 200ms Rust polling daemon. "
        "action=enforce escalates against runaway OWNED processes only - "
        "dry-run unless apply=true, never signals external processes, the "
        "roots themselves, this process, or the guard daemon. "
        "action=auto-enforce gates the same enforcement behind the daemon's "
        "rolling window: it acts only on sustained pressure (cpu_avg >= "
        "sustained_cpu or min available RAM <= ram_floor_mb) and is "
        "fail-closed when the daemon is absent, stale, or still warming up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "start", "stop", "enforce", "auto-enforce"],
                "default": "status",
            },
            "owned_roots": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Root PIDs this agent owns; used for the ownership scan and required for enforce.",
            },
            "cpu_sample_ms": {"type": "integer", "minimum": 0, "default": 100},
            "interval_ms": {"type": "integer", "minimum": 100, "default": 200},
            "window": {"type": "integer", "minimum": 1, "default": 25},
            "cpu_threshold": {
                "type": "number",
                "default": 90.0,
                "description": "enforce: CPU percent above which an owned process is a runaway candidate.",
            },
            "rss_threshold_mb": {
                "type": "integer",
                "default": 4096,
                "description": "enforce: RSS in MB above which an owned process is a runaway candidate.",
            },
            "grace_seconds": {
                "type": "number",
                "default": 3.0,
                "description": "enforce: seconds between SIGTERM and SIGKILL.",
            },
            "apply": {
                "type": "boolean",
                "default": False,
                "description": "enforce: actually signal candidates. Without this the call is a dry run.",
            },
            "protect": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "enforce: extra PIDs that must never be signalled.",
            },
            "sustained_cpu": {
                "type": "number",
                "default": 85.0,
                "description": "auto-enforce: window cpu_avg at or above this triggers enforcement.",
            },
            "ram_floor_mb": {
                "type": "integer",
                "default": 1024,
                "description": "auto-enforce: window min available RAM at or below this triggers enforcement.",
            },
            "min_samples": {
                "type": "integer",
                "minimum": 1,
                "default": 25,
                "description": "auto-enforce: minimum window samples required before any judgement.",
            },
        },
        "required": [],
    },
}

__all__ = [
    "BOOTSTRAP_SCHEMA",
    "BROWSER_CLICK_SCHEMA",
    "BROWSER_EXTRACT_SCHEMA",
    "BROWSER_OPEN_SCHEMA",
    "BROWSER_TYPE_SCHEMA",
    "CONTEXT_COMPRESS_SCHEMA",
    "GUARD_SCHEMA",
    "HERMES_CONFIG_SCHEMA",
    "LOOP_AUTO_SCHEMA",
    "PLAN_SCHEMA",
    "QUEUE_BRIEF_SCHEMA",
    "QUEUE_NEXT_SCHEMA",
    "QUEUE_RECORD_SCHEMA",
    "SERVE_LOCAL_SCHEMA",
    "WEB_SEARCH_SCHEMA",
]
