---
description: Run Cluxion preprocessing and return the JSON contract.
argument-hint: "<prompt>"
disable-model-invocation: true
---

Run:

```bash
cluxion-runtime plan --surface codex --json-stdin
```

stdin:

```json
{"prompt":"$ARGUMENTS","cwd":"$PWD"}
```

Return the JSON contract to the host flow. Run `loop-auto` only when `loop_auto=true` is present and the plan is queue-required; `/loopAuto` prefix sets that flag but does not force short prompts into the queue. For manual queues, `queue-next --full` only disables truncation for that call; every `queue-next` call still advances to the next unrecorded step.
