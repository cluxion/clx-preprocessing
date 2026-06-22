---
name: cluxion-preprocess
description: Run Cluxion preprocessing before agent work. You (the connected AI) call cluxion tools/CLI. Use for task planning, long work queueing, or unclear user intent. Enforces honesty and clarification before queueing.
---

# Cluxion Preprocessing — 연결된 AI 지시문

작업 전에 **당신(연결된 AI)** 이 plan·큐 도구를 호출합니다. 플러그인은 JSON 계약만 반환합니다.

## Plan 호출

```bash
cluxion-runtime plan --surface claude --json-stdin
```

stdin: `{"prompt": "<user request>", "cwd": "<workspace>"}`

Hermes에서는 `cluxion_plan` 도구를 동일 목적으로 사용합니다.

Hermes 슬래시 (0.3.23+): `/loopauto <prompt>` — plan + 자동 큐 드레인. `/cluxion-doctor` — 점검.

## 규칙

1. `clarification.required`이 true이면 사용자에게 질문하고 작업을 시작하지 말 것.
2. context가 부족하면 모른다고 말할 것 — 사실을 지어내지 말 것.
3. `queued` 모드: 수동이면 `cluxion_queue_next` → segment 처리 → `cluxion_queue_record` → `cluxion_queue_brief`.
   긴 작업은 Hermes에서 `/loopauto` 또는 plan `loop_auto: true`(기본 on)로 자동 드레인.
4. 세션 맥락 보존이 필요하면 brief를 ForgetForge에 넘길 것: `forgetforge import-brief --source preprocessing --brief "<cluxion_queue_brief output>"` (또는 Hermes `forgetforge_import_brief`).
5. `answer_policy.required_checks`를 응답 전에 따를 것.
6. 플러그인이 대신 completion을 생성하지 않음 — **당신**이 계약에 맞게 응답할 것.

## 설치 확인

```bash
cluxion-preprocess check
```