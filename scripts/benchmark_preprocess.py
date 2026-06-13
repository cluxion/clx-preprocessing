#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from statistics import mean

from cluxion_runtime.core.harness import build_harness_plan
from cluxion_runtime.core.types import WorkItem
from cluxion_runtime.resources.queue_bridge import queue_available


def bench(label: str, prompt: str, repeats: int = 200) -> dict[str, object]:
    item = WorkItem(f"bench-{label}", prompt)
    timings: list[float] = []
    plan = None
    for _ in range(repeats):
        start = time.perf_counter()
        plan = build_harness_plan(item)
        timings.append((time.perf_counter() - start) * 1000)
    assert plan is not None
    return {
        "label": label,
        "mode": plan.preprocessing.mode,
        "clarification_required": plan.clarification_required,
        "queue_backend": plan.queue_backend,
        "ms_avg": round(mean(timings), 4),
        "ms_p95": round(sorted(timings)[int(len(timings) * 0.95) - 1], 4),
    }


def main() -> None:
    cases = [
        ("simple", "Is this possible?"),
        ("verification", "Check latest PyPI version."),
        ("ambiguous", "아마 둘 중 하나로 수정해줘. 어느 쪽인지 모르겠어."),
        ("coding", "Fix the bug in src/foo.py and add tests."),
    ]
    results = [bench(label, prompt) for label, prompt in cases]
    payload = {
        "plugin": "cluxion-agentplugin-preprocessing",
        "rust_queue_available": queue_available(),
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
