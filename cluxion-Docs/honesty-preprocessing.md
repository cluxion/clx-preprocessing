# Honesty Preprocessing (legacy reference)

> 최신 문서: [`../Docs/design.md`](../Docs/design.md)

플러그인은 모델을 바꾸지 않습니다. **연결된 AI**가 따라야 하는 `answer_policy` 계약을 만듭니다.

## Goals

- 모르면 모른다고 말하게 함
- 확인하지 않은 실행·파일·환경 상태를 사실처럼 말하지 않게 함
- 확인한 사실 / 추론 / unknown 분리
- 짧은 질문에 heavy preprocessing 비용 최소화

## Modes

| Mode | 동작 |
|------|------|
| `simple_answer` | segment·resource snapshot 없음 |
| `verification_answer` | `verification_required` + `required_checks` |
| `standard` | segment·resource admission·evidence 기반 응답 |
| `queued` | checksum·순서 보존 합성 |

## Required Checks (대표)

- `verify_current_or_recent_fact`
- `cite_external_source_or_document`
- `inspect_runtime_state_before_claiming`
- `run_requested_check_or_state_not_run`
- `tie_claims_to_file_diff_or_command_output`
- `tie_security_claims_to_evidence`
- `preserve_segment_checksums_in_synthesis`

## Hard Boundaries

- 기억·추정만으로 현재 상태 단정 금지
- 실행하지 않은 테스트 통과 주장 금지
- 존재하지 않는 파일·URL·버전 날조 금지
- ambiguous prompt는 확정 표현 자제

검증·실행·응답은 **연결된 AI** 책임입니다.