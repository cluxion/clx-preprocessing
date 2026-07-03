# Documentation

`cluxion-Agentplugin-preprocessing` 공개 문서입니다.

## 처음 읽는 분

**Preprocessing**은 에이전트가 작업을 시작하기 **전**에 방향을 정리하는 **전처리 플러그인**입니다.

| 질문 | 답 |
|------|-----|
| **무엇을 하나요?** | 애매한 요청은 질문하고, 긴 작업은 segment 큐로 나누며, 확인하지 않은 사실은 말하지 않도록 `answer_policy` 계약을 만듭니다. |
| **누가 실행하나요?** | **연결된 AI**가 `cluxion_plan` 등 도구·CLI를 호출하고, JSON 계약에 따라 응답·큐 처리를 합니다. |
| **플러그인이 모델을 부르나요?** | **기본적으로 아니요.** 결정론적 plan·게이트만 반환합니다. completion은 host agent 모델이 수행합니다. |
| **왜 Rust인가요?** | 작업큐·dispatch hot path를 Rust(`cluxion-queue`)로 두고, Python은 plan·adapter·CLI를 담당합니다. |

### 연결된 AI 사용 흐름

1. 사용자 요청 수신 → `cluxion_plan` (또는 `cluxion-runtime plan`)
2. `clarification.required`이면 사용자에게 질문, 작업 보류
3. `queued` 모드: 수동이면 `cluxion_queue_next` → … → `cluxion_queue_brief`. 자동 드레인은 `/loopauto`, `loop_auto: true`, queue 대상 `/loopAuto` prefix, 또는 전용 `loop-auto` 명령으로 실행합니다.
4. `answer_policy.required_checks`를 지키며 응답
5. (선택) `cluxion_queue_brief` 결과를 ForgetForge에 저장: `forgetforge import-brief --source preprocessing`

Skill: [`skills/preprocess/SKILL.md`](../skills/preprocess/SKILL.md)

### 사람(개발자)이 할 일

```bash
pip install cluxion-agentplugin-preprocessing
cluxion-preprocess check
cluxion-preprocess enable   # Hermes
```

## 목차

| 문서 | 내용 |
|------|------|
| [architecture.md](architecture.md) | 패키지 구조, 데이터 흐름, host 경계 |
| [design.md](design.md) | 정직함, 명확화, 성능 |
| [installation.md](installation.md) | 설치, Hermes 활성화, 슬래시, Rust 큐 |
| [tools.md](tools.md) | 도구·슬래시·loopAuto·queued workflow |
| 통합 가이드 | MacBot `Docs/cluxion-plugins-guide.md` §2-A (5종 슬래시 전체) |
| [tools.md](tools.md) | 도구, preprocessing mode, queue |
| [agent-surfaces.md](agent-surfaces.md) | Hermes / Claude / Codex / Grok |
| [rust-architecture.md](rust-architecture.md) | Rust 메인 · Python adapter |

## 이 레포에서 다루지 않는 것

- API 키·OAuth (호스트 에이전트 소유)
- 플러그인이 host 대신 LLM completion을 대체하는 것
- 비공개 인프라·배포 비밀

이슈는 GitHub Issues를 사용해 주세요.
