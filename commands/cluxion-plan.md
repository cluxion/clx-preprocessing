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

Return the JSON contract to the host flow. Do not run `loop-auto` unless `loop_auto=true` is explicitly present.
