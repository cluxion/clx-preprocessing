---
description: Explicitly drain a queued Cluxion work bundle.
argument-hint: "<work_id>"
---

Run:

```bash
cluxion-runtime loop-auto --work-id "$ARGUMENTS" --json-stdin
```

stdin:

```json
{"work_id":"$ARGUMENTS"}
```
