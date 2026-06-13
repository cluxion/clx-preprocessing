# Harness Logic (legacy reference)

> 최신 문서: [`../Docs/design.md`](../Docs/design.md), [`../Docs/tools.md`](../Docs/tools.md)

## 1. Adapter Payload

```bash
cluxion-runtime plan --json-stdin --surface <surface>
```

필수: `prompt`  
선택: `work_id`, `priority`, `cwd`, `metadata`, `expected_ram_mb`, `context_tokens`

## 2. Intent 분류

`classify_intent()` — **모델 호출 없음**, 결정론적.

- `category`, `operation`, `direction`, `confidence`, `signals`
- 방향은 host harness (`hermes_harness`, `claude_harness`, …) 또는 `host_managed`

## 3. 전처리 모드

| Mode | 용도 |
|------|------|
| `simple_answer` | 짧은 일반 질문 |
| `verification_answer` | 사실 확인 필요 짧은 질문 |
| `standard` | 코드·테스트·보안·문서 등 실질 작업 |
| `queued` | 긴 입력 segment 분할 |
| `needs_clarification` | 사용자 방향 확정 전 |

모든 모드에 `answer_policy` 포함.

## 4. 작업큐

`AgentWorkQueue` — priority + FIFO. Rust `cluxion-queue` 또는 Python fallback.

## 5. Host Execution

`host_execution` 계약을 **연결된 AI**가 읽고 실행:

- `current_turn_direct_answer`
- `current_turn_verify_then_answer`
- `single_host_task`
- `durable_segment_queue` + `cluxion_queue_*`
- `ask_user_before_queue`

## 6. Resource Admission

`collect_resource_snapshot()` + `capacity_decision()` — fail-closed RAM/CPU 게이트.

Queued segment content는 out-of-band store에 저장됩니다.