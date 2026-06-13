# Design

## 설계 목표

1. **결정론적 전처리** — 추가 LLM preflight 없이 plan 생성
2. **정직함** — 확인하지 않은 사실·도구 결과를 주장하지 않음
3. **명확한 방향** — 애매한 요청은 추측 대신 사용자에게 질문
4. **안정 큐** — Rust 큐 + atomic write, Python fallback
5. **호스트 경계** — provider·OAuth·모델 선택은 host 소유

## 정직함 (Honesty)

모든 plan에는 `answer_policy`가 포함됩니다.

- context 부족 시 **모른다고 말할 것**
- 파일·명령·외부 사실 **날조 금지**
- 실행하지 않은 검사를 **성공한 것처럼 말하지 않을 것**

`required_checks` 예: `verify_current_or_recent_fact`, `inspect_runtime_state_before_claiming`, `tie_claims_to_file_diff_or_command_output`

## 명확화 (Clarification)

`assess_clarification()`은 결정론적으로 검사합니다.

- 의도 confidence 낮음
- 애매한 표현 (`아마`, `둘 중` 등)
- 코딩 요청인데 대상 파일 없음
- 범위 과대

`clarification.required=true`이면 **연결된 AI**가 사용자에게 질문합니다. `metadata.clarification_answers`로 재계획 가능합니다.

## 성능

- `simple_answer` / `verification_answer`: resource snapshot 생략
- queued plan: segment 본문은 out-of-band, plan 출력은 메타데이터만

## 연결된 AI와의 관계

플러그인은 **지시·계약·큐**를 제공합니다.  
해석·질문·segment 처리·최종 응답은 **연결된 AI**가 `answer_policy`와 skill을 따라 수행합니다.