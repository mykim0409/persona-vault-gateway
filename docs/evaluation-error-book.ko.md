# 평가 오류 장부

[English](evaluation-error-book.md) | **한국어**

이 장부는 PersonaVault 검색·사용 benchmark가 기대 동작을 어긴 사례를 재현하고 회귀를 막기 위한 기록입니다. 성공 실행은 집계 지표로 관리하고, 실패만 한 행씩 남깁니다.

## 기록 템플릿

| run_id | scenario_id | bundle | category | sanitized_query | expected | actual | evidence_ref | cause_or_hypothesis | next_action | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `20260716-01` | `cross-agent-01` | `experiences` | `retrieval_miss` | `재색인 실패 복구 방법` | `ep_qdrant_rebuild` 반환 | 관련 episode 없음 | `benchmark/runs/20260716-01` | error signature 불일치 | alias/signature 정규화 후 재실행 | `open` |

`status`는 `open`, `fixed`, `accepted` 중 하나를 사용합니다. 원인을 아직 모르면 추측을 사실처럼 쓰지 말고 `cause_or_hypothesis`에 가설임을 표시합니다.

## 실패 분류

| category | 기준 |
| --- | --- |
| `retrieval_miss` | 필요한 문서가 허용 순위 안에 없음 |
| `authority_leak` | agent·raw·derived 자료가 canonical처럼 제시됨 |
| `stale_current` | superseded 또는 오래된 주장이 현재 사실처럼 반환됨 |
| `negative_transfer` | 다른 환경·repository의 해결책을 적용 가능성 확인 없이 사용함 |
| `conflict_hidden` | 상충하는 근거가 누락되거나 근거 없이 한쪽이 선택됨 |
| `provenance_error` | 같은 source의 반복을 독립 검증으로 세거나 fallback provenance를 명시값처럼 표시함 |
| `unsupported_answer` | 근거가 부족한데 답을 단정하여 abstention에 실패함 |
| `duplicate_subject` | 같은 subject가 동등한 최상위 답으로 중복됨 |
| `cross_scope_miss` | alias, error signature, 다국어 또는 cross-project 연결을 놓침 |
| `metadata_error` | metadata 파싱·정규화·source hash 판정이 잘못됨 |
| `harness_error` | 제품 동작이 아니라 benchmark fixture, index 준비, runner가 실패함 |

## 운영 규칙

1. 기대값, 실제값, 근거를 재현 가능한 ID와 repository 상대 경로로 기록합니다.
2. 한 행에는 주된 실패 분류 하나를 쓰고, 연쇄 증상은 `actual`에 적습니다.
3. query와 출력은 최소한으로 발췌하고 token, credential, 개인 host, 사용자명, 로컬 절대 경로를 제거합니다.
4. 원본 transcript나 전체 tool log를 복사하지 말고 접근 통제된 benchmark artifact의 참조만 남깁니다.
5. 수정 후 같은 scenario를 다시 실행하고 새 run을 추가한 뒤 기존 행을 `fixed`로 바꿉니다. 과거 실패 내용은 지우지 않습니다.
6. 일시적인 help 출력, smoke-test 출력, 설치 확인, 단발성 네트워크 오류는 Vault에 저장하지 않습니다. 필요하면 Vault 밖의 CI artifact나 benchmark run log에 둡니다.
7. 실패가 다른 작업에도 재사용할 만하고 실제 관찰·적용 조건·근거가 갖춰졌을 때만 별도의 `episode`로 기록합니다. benchmark 장부 자체를 canonical 또는 candidate로 승격하지 않습니다.
