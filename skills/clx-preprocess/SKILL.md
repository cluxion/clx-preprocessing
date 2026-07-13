---
name: clx-preprocess
description: Use Cluxion preprocessing before agent work that needs clarification, queueing, loop_auto, doctor checks, or surface-specific JSON contracts.
---

# Cluxion Preprocess

Call the runtime CLI. The plugin returns JSON contracts; the host agent owns model calls and final answers.

## Plan

```bash
cluxion-runtime plan --surface codex --json-stdin
```

Use `--surface claude` in Claude Code. Minimum stdin:

```json
{"prompt":"<user request>","cwd":"<workspace>"}
```

Rules:

1. If `clarification.required` is true, ask the user before starting work.
2. If `host_execution.queue_required` is true, process the queue with `queue-next`, `queue-record`, and `queue-brief`, or call `loop-auto` only when the user or payload opts in with `loop_auto=true`.
3. A `/loopAuto` prompt prefix is stripped by `plan` and sets `loop_auto=true`; it drains only queue-eligible plans and does not force short fast-path prompts into the queue.
4. Never claim checks were run unless the host actually ran them.

## Queue

```bash
cluxion-runtime queue-next --work-id <work_id>
cluxion-runtime queue-record --work-id <work_id> --step-id <step_id> --json-stdin
cluxion-runtime queue-brief --work-id <work_id>
```

`queue-next --full` only disables field truncation for that call. Every `queue-next` call still advances to the next unrecorded step; it does not re-fetch the previous step.

## Explicit loop_auto

```bash
cluxion-runtime loop-auto --work-id <work_id> --json-stdin
```

## Doctor

```bash
cluxion-preprocess doctor
```
