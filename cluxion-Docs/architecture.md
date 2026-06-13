# Architecture (legacy reference)

> 최신 문서: [`../Docs/architecture.md`](../Docs/architecture.md)

## 패키지 구성

| 패키지 | 역할 |
|--------|------|
| `cluxion_agentplugin_preprocessing` | Hermes plugin, CLI, tool schema |
| `cluxion_runtime` | `cluxion-runtime plan`, harness 엔진 |
| `rust/cluxion_queue` | SQLite 작업큐 + dispatch (선택) |

## Host vs Cluxion

**Host agent (연결된 AI)**

- OAuth, provider, **모델 선택**
- tool 권한, completion, 최종 응답
- queued segment 처리·synthesis

**Cluxion preprocessing**

- `WorkItem` 정규화, intent 분류
- `answer_policy` · `host_execution` 계약
- 명확화·segment 큐·resource admission
- **추가 LLM 호출 없음**

## 등록 도구 (Hermes `cluxion` toolset)

| Tool | 용도 |
|------|------|
| `cluxion_plan` | 전처리·방향·큐·리소스 plan |
| `cluxion_clarify` | 명확화 질문 |
| `cluxion_queue_next` / `record` / `brief` | segment 큐 |

모델·provider 변경은 host agent 책임입니다. 전처리 플러그인은 실행 **전** 계약만 제공합니다.