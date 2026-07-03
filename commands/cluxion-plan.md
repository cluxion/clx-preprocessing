---
description: Run Cluxion preprocessing and return the JSON contract.
argument-hint: "<prompt>"
---

Run:

```bash
cluxion-runtime plan --surface codex --json-stdin
```

stdin:

```json
{"prompt":"$ARGUMENTS","cwd":"$PWD"}
```

Return the JSON contract to the host flow. Run `loop-auto` only when `loop_auto=true` is present and the plan is queue-required; `/loopAuto` prefix sets that flag but does not force short prompts into the queue.
