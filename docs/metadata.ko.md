# PersonaVault metadata

[English](metadata.md) | **한국어**

`pv_schema: 1`은 Markdown frontmatter의 공식 형식입니다. Gateway가 새 문서를 쓸 때는 최상위 키를 평면으로 두고, 객체와 목록은 한 줄 JSON-flow 값으로 기록합니다.

Gateway v3의 `POST /gateway/v3/capture`는 `kind=conversation|note`로 입력을 구분하지만
두 종류 모두 `30_Conversations/raw/`의 검토 전 evidence로 저장합니다. Note는
`note_type=observation|proposal|handoff`와 자유 형식 `note_kind`를 함께 사용합니다.
API의 `kind` discriminator와 아래 Markdown의 내용 분류 `kind`는 서로 다른 필드입니다.
Gateway는 새 `40_Agents/` 파일을 만들지 않습니다.

```yaml
---
pv_schema: 1
id: ep_qdrant_rebuild
memory_type: episode
kind: debugging
review_state: unreviewed
temporal_state: point_observation
outcome: failure
observed_at: "2026-07-16T09:30:00Z"
projects: ["PersonaVault"]
topics: ["qdrant", "index-rebuild"]
subject_id: qdrant-index-rebuild
subject_aliases: ["Qdrant reindex", "Qdrant 재색인"]
error_signatures: ["collection not found during index rebuild"]
applicability: {"repository":"persona-vault-gateway","operating_system":"linux"}
provenance_mode: direct_observation
provenance_defaulted: false
evidence_refs: ["run_qdrant_rebuild"]
method_refs: []
derived_from: []
source_refs: ["run_qdrant_rebuild"]
source_hashes: {"run_qdrant_rebuild":"sha256:<content-hash>"}
repository_sources: [{"repo_id":"persona-vault-gateway","path":"gateway/core.py","commit":"<commit>","anchor":"index_vault"}]
relations: {"supports":[],"contradicts":[],"supersedes":[]}
conflict_state: none
retrieval_tier: supporting
privacy: normal
---
```

알 수 없는 값은 꾸며내지 않습니다. 특히 시각을 모르면 `observed_at`, `effective_from`, `effective_to`를 쓰지 않으며 빈 문자열, 현재 시각, 파일 생성 시각으로 대신하지 않습니다.

## 호환 입력

기존 문서를 읽을 때는 같은 의미의 중첩 YAML도 허용합니다. 공식 writer는 항상 위의 평면 형식으로 출력합니다.

```yaml
review:
  state: unreviewed
temporal:
  state: point_observation
  observed_at: 2026-07-16T09:30:00Z
provenance:
  mode: direct_observation
  evidence_refs:
    - run_qdrant_rebuild
applicability:
  repository: persona-vault-gateway
  operating_system: linux
```

중첩 `review.state`, `temporal.*`, `provenance.*`는 각각 평면 `review_state`, `temporal_*`, `provenance_*`로 정규화합니다. JSON-flow 값도 블록 목록·객체로 읽을 수 있습니다. 같은 의미의 값이 서로 충돌하거나 YAML을 안전하게 해석할 수 없으면 권한을 높이지 않고 검토 전 evidence로 취급합니다.

## 권한 규칙

경로와 인증된 작성 주체가 문서의 선언보다 우선합니다.

| 입력 상태 | 보수적 해석 |
| --- | --- |
| 기존 `40_Agents/<agent_id>/` | legacy `episode` 또는 `candidate`, `unreviewed`, 최대 `supporting` |
| `30_Conversations/raw/` | v3 conversation 또는 note인 `transcript`, `unreviewed`, `evidence` |
| `30_Conversations/summaries/` | source ID와 hash를 가진 `derived_view`, 최대 `supporting` |
| `30_Conversations/important/` | 이전 버전 호환용 `derived_view`, 최대 `supporting` |
| `10_User/WORKING_AGREEMENT.md` | 사람이 승인한 전역 협업 규칙인 `canonical`, `primary` |
| `kind: user_profile` 또는 `user_ledger`인 `10_User/` 문서 | 승인된 Curator가 관리하는 사용자 브리프와 관찰 장부인 `canonical`, `primary` |
| `10_User/`의 다른 문서 | 사람이 관리하며 필요할 때 검색하는 사용자 세부 기록 |
| `20_Projects/`, `50_Knowledge/` | 사람 또는 Curator가 승인한 `canonical`, `primary`. 프로젝트의 `BRIEF.md`(`kind: brief`)와 `DECISIONS.md`(`kind: decision_ledger`)가 여기에 있음 |
| 잘못되었거나 해석 불가한 metadata | `unknown`, `unreviewed`, 시간 필드 생략, `evidence` |
| provenance 누락 | `provenance_mode: reported`, `provenance_defaulted: true` |
| outcome 누락 또는 미지원 값 | `outcome: unknown` |

Gateway raw와 legacy agent root는 frontmatter에 `canonical`, `human_accepted`, `current`,
`primary`를 써도 canonical 권한을 얻지 못합니다. Canonical 권한은 사람이 관리하는 root와
검토 정책이 함께 허용할 때만 인정합니다. 공개 `view=all` 검색은 `primary`, `supporting`,
`evidence`, `history`, `archive`를 함께 검색하며, `retrieval_tier`는 검색 우선순위이지 읽기
권한이 아닙니다.

명시적인 `provenance_mode: reported`는 `provenance_defaulted: false`입니다. provenance가 없어서 fallback이 적용된 경우에는 정규화 결과와 사용자 표시에서 반드시 `reported (default)`로 구분하며, 직접 관찰이나 사람 검토로 승격하지 않습니다.

## 필드

| 필드 | 의미 |
| --- | --- |
| `pv_schema` | metadata 계약 버전. 현재 값은 `1` |
| `id` | 문서의 안정적인 식별자 |
| `memory_type` | `transcript`, `episode`, `candidate`, `canonical`, `derived_view` 중 지식 역할 |
| `kind` | `debugging`, `procedure`, `decision`, `lesson`, `handoff` 같은 내용 종류. Curator는 `brief`와 `decision_ledger`(프로젝트 현황판과 결정 장부), `user_profile`과 `user_ledger`(사용자 브리프와 관찰 장부)도 사용 |
| `capture_kind` | v3 raw writer가 기록한 `conversation` 또는 `agent_note` |
| `note_type` | note의 수명주기 역할인 `observation`, `proposal`, `handoff` |
| `note_kind` | note의 더 구체적인 자유 형식 내용 분류. API 입력과 raw context에 보존 |
| `review_state` | `unreviewed`, `human_accepted`, `human_rejected`, `merged` 같은 검토 상태 |
| `temporal_state` | `point_observation`, `proposed_current`, `current`, `historical`, `superseded`, `unknown` |
| `outcome` | `success`, `failure`, `mixed`, `unknown`, `not_applicable` |
| `observed_at` | 사건을 관찰한 시각 |
| `session_id`, `segment_date` | 날짜별 transcript를 같은 agent 세션으로 연결하는 식별자와 현지 날짜 |
| `effective_from`, `effective_to` | 주장이나 결정이 유효한 기간 |
| `projects`, `topics` | 검색 범위와 유연한 주제 표식 |
| `subject_id` | 같은 주장을 묶는 안정적인 subject 식별자 |
| `applicability` | repository, revision, OS, 도구 버전 등 적용 조건 |
| `provenance_mode` | `direct_observation`, `derived`, `reported`, `human_asserted` 중 출처 성격 |
| `provenance_defaulted` | provenance 누락으로 `reported`가 적용됐는지 표시하는 정규화 결과 |
| `evidence_refs`, `method_refs`, `derived_from` | 관찰 근거, 참고한 방법, 파생 원문 ID |
| `relations` | `supports`, `contradicts`, `supersedes` 등 문서 관계 |
| `conflict_state` | `none`, `unresolved`, `resolved` |
| `retrieval_tier` | `primary`, `supporting`, `evidence`, `history`, `archive` |
| `privacy` | 내용 분류 표식. 자체로 접근 제어를 제공하지 않음 |

## P1 검색 필드

| 필드 | 규칙 |
| --- | --- |
| `subject_aliases` | 같은 subject의 언어·표기 변형. 별도 canonical 문서를 만들지 않음 |
| `error_signatures` | 재사용 가능한 짧은 오류 형태. 전체 로그, 임시 ID, token, 개인 host/path는 넣지 않음 |
| `source_refs` | derived view나 주장에 사용한 source ID 목록 |
| `source_hashes` | source ID를 content hash에 연결하는 객체. 모르는 hash는 항목을 만들지 않음 |
| `repository_sources` | repository가 원본인 사실의 `repo_id`, `path`, `commit`과 선택적 `anchor` |

`source_hashes`는 source 변경으로 derived view가 stale인지 판단하는 용도이며 권한을 높이지 않습니다. `repository_sources`에는 원격 credential이나 로컬 절대 경로를 넣지 않습니다.

## Curator 문서와 출처 marker

Curator는 형식이 고정된 문서 네 개를 관리합니다. 파일명이 아니라 `kind`로 찾으며, `kind`는 표시한 디렉토리 아래에 있어야 합니다. 제목과 표 머리글은 한국어이고, [CURATOR.md](CURATOR.md)(영문)가 설명합니다.

| 경로 | `kind` | 형식 |
| --- | --- | --- |
| `20_Projects/<Project>/BRIEF.md` | `brief` | 고정된 `##` 제목을 가진 현황판. 매 정리마다 다시 씀. 본문 최대 8,000자, marker는 세지 않음 |
| `20_Projects/<Project>/DECISIONS.md` | `decision_ledger` | `D-###` 행으로 이루어진 표 하나. 행은 추가만 하고 지우지 않음 |
| `10_User/PROFILE.md` | `user_profile` | 고정된 `##` 제목을 가진 사용자 브리프. 매 정리마다 다시 씀. `BRIEF.md`와 같은 상한 |
| `10_User/OBSERVATIONS.md` | `user_ledger` | `U-###` 행으로 이루어진 표 하나. 행은 추가만 하고 지우지 않음 |

raw에서 가져온 주장은 `<!-- pvg-src: item:<16 hex> -->` 같은 HTML 주석인 출처 marker로 끝납니다. marker 하나에 공백으로 구분한 token 여러 개를 담을 수 있습니다.

- `item:<16 hex>`는 raw item의 경로와 locator의 단방향 hash입니다. 경로나 인용은 담지 않습니다.
- `sess:<8 hex>`는 세션 id의 단방향 hash입니다. 사용자 장부의 행만 가집니다.
- `doc:<path>#<heading>`는 기존 문서를 가리킵니다. 사람이 직접 쓰며, 주제 문서에서 한 번 옮기는 migration에서만 씁니다.

marker는 주장 줄의 끝에 두며, 표 행에서는 마지막 칸 안에 둡니다. Obsidian 읽기 보기에서는 보이지 않습니다. Markdown 본문의 일부이므로 active 문자 수에 포함되고 색인됩니다. `item:`과 `sess:` token은 Curator 도구가 쓰므로 직접 고치지 않습니다. 직접 쓰는 것은 `doc:` token뿐입니다.
