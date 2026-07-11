---
description: Run Cluxion preprocessing and return the JSON contract.
argument-hint: "<prompt>"
disable-model-invocation: true
---

Choose the current host surface explicitly (no runtime autodetection):

- Claude Code: `--surface claude`
- Codex: `--surface codex`

Run exactly one matching command:

```bash
# Claude Code
cluxion-runtime plan --surface claude --json-stdin

# Codex
cluxion-runtime plan --surface codex --json-stdin
```

stdin:

```json
{"prompt":"$ARGUMENTS","cwd":"$PWD"}
```

Return the JSON contract to the host flow. Run `loop-auto` only when `loop_auto=true` is present and the plan is queue-required; `/loopAuto` prefix sets that flag but does not force short prompts into the queue. For manual queues, `queue-next --full` only disables truncation for that call; every `queue-next` call still advances to the next unrecorded step.
