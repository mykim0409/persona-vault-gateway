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

## 검색 응답 규칙

Gateway는 출처를 검색하며 질문의 참·거짓을 판정하지 않습니다.
기존 `answer_state` 필드는 다음 출처 상태만 전달합니다.

- `evidence`: 해석할 출처 후보가 있습니다. 잘못된 질문의 전제를 정정하는 근거도 포함합니다.
- `abstain`: 관련 출처 후보를 찾지 못했습니다. 질문이 거짓이라는 뜻은 아닙니다.
- `review_required`: 선택된 출처 집합에 미해결 충돌 메타데이터가 있습니다. 범위를 확인하고 관련 상반된 주장을 승자 없이 제시합니다.

`evidence`, 단어 일치, 사람이 승인한 정본이라는 사실만으로 질문의 인물·행위·날짜·수치·조건이
성립하지 않습니다. `supported`와 `refuted`는 반환하지 않습니다.
검색 중 LLM 검증이나 언어별 인물·행위 판정, 질문을 이용한 충돌 해제는 하지 않습니다.
문서의 `conflict_state`는 유지하며, 충돌이 있는 장부를 질문에 맞춰 임의로 해제하지 않습니다.
관련성이 약한 다른 후보의 충돌은 해당 문서에 표시하되 가장 강한 검색 결과로 전파하지 않습니다.
명시적으로 같은 주제이거나 반대 관계인 출처는 결과 개수 제한 전에 확인합니다.

응답 전용 `answer_relevance`는 `candidate`, `semantic_candidate`, `lexical_match`,
`related_claim`, `linked_context`로 검색 경로를 구분합니다. 어느 값도 사실의 입증을 뜻하지 않습니다.
키워드는 문단·목록 항목·표 행별로 계산하고 상위 ATX 제목, 목록의 도입부, 표 머리글을 보존합니다.
서로 다른 행과 항목의 주장을 합치지 않습니다. 완전한 CommonMark 파서는 아닙니다.

문서 안에서는 이미 선택한 구간에 없는 검색어를 보충하는 구간을 먼저 선택합니다.
일치 수가 같으면 문서 앞쪽이나 단어 반복 횟수보다 더 적은 구간에 나타나는 검색어를 우선합니다.
약한 일치 행도 빠진 주체를 보여줄 수 있지만 다른 행의 행위를 빌려오지는 않습니다.
이후 여러 검색어가 일치하는 나머지 구간도 유지해 결과·날짜·조건의 차이를 보존합니다.
결과는 문서 단위지만 `passages`에는 여러 구간이 들어갑니다. 각 구간은 섹션, 현재 문서의
0 기반 `block_index` 또는 의미 검색의 `chunk_index`, 검색 라벨을 포함합니다.
기존 `snippet`은 첫 표시 구간입니다. 출처 마커는 숨깁니다.

숫자는 부호·소수·단위·숫자가 포함된 식별자를 온전하게 비교합니다.
동등한 소수·천 단위 표기와 날짜 표기만 정규화하고 원문은 바꾸지 않습니다.
다른 수치는 키워드 일치도를 낮출 수 있지만 정정 근거를 숨기지는 않습니다.
한국어 조사 정규화와 명시적 별칭은 검색 보조이지 인물 귀속·단위 추론·계산·입증이 아닙니다.

단독 기계형 식별자는 keyword·의미 검색 활성화 환경 모두에서 정확 조회합니다.
문법은 `[A-Za-z_][A-Za-z0-9_]*(?:[.-][A-Za-z0-9_]+)*`이며 숫자나 밑줄을 하나 이상 포함해야 합니다.
양끝 공백을 제외한 질의 전체가 맞아야 합니다. `INC-2048`, `task_alpha`는 해당하지만 날짜·수치·
숫자로 시작하는 단위 값·일반 단어·문장은 해당하지 않습니다. 대소문자를 무시하고 ASCII 토큰 전체를
비교하므로 더 긴 식별자의 접두사만으로는 일치하지 않습니다. 현재 view의 Markdown 본문이나 기존
검색 필드 `subject_id`, `subject_aliases`, `error_signatures`, `topics`에 정확한 식별자가 없으면
빈 결과와 `abstain`을 반환합니다. 이 조회에는 질의 임베딩과 벡터 후보 조회를 하지 않으며,
색인 상태와 출처 경고 규칙은 유지합니다. 자연어 바꿔쓰기와 잘못된 전제를 정정하는 근거는 기존
의미 검색으로 계속 반환합니다.

출처 경고 계산 후 가장 강한 키워드 결과에서 `linked_context`를 한 단계 확장합니다.
문서별 첫 두 일치 구간, 인라인 Markdown 링크 두 개, 연결된 섹션당 두 구간까지만 가져옵니다.
`[limits](#limits)`나 `procedure.md#limits`처럼 현재 조회 범위의 문서에서 유일한 ATX 제목을
가리켜야 합니다. 외부 URL·파일 전체·모호한 제목·이미지·코드·위키 및 참조형 링크·재귀 탐색은 제외합니다.
추가 구간은 보조 문맥이지 새 일치나 입증이 아닙니다. 원래 권한과 충돌 메타데이터는 유지합니다.
`index.context_expansion.added`는 제한 전 추가 구간 수, `limited`는 부분 미리보기 여부입니다.

주장과 인물·범위·시점·필수 조건은 같은 문단이나 행에 기록하고 긴 상세만 섹션 링크로 연결합니다.
raw 수집·노트 필드·승인 게이트·원본 Markdown·임베딩 색인은 바꾸지 않습니다.

같은 섹션의 정확한 반복과 화면에 보이는 인접 포함 관계만 합칩니다. 날짜·조건·부정이 다른 주장은
제거하지 않습니다. 문서들이 12,000바이트 UTF-8 구간 예산을 번갈아 사용하며 `limit`은 문서 수입니다.
구간은 최대 520자이고 잘리면 `truncated`가 표시됩니다. `passages_omitted`와
`index.passages_truncated`로 생략을 알립니다. context를 포함한 REST 응답은 60,000바이트 이내이며,
빠진 문서는 `index.results_truncated`에 표시합니다. 표시 제한으로 출처 경고를 해제하지 않습니다.
빠진 세부 정보가 필요할 때만 검색어를 좁힙니다.

MCP·CLI·GPT Actions도 같은 규칙을 사용합니다. 큐레이팅에서는 긍정 판정 대신 출처 경로와
`expect_excerpts`를 검사합니다. 기존 `supported` probe는 재검토 안내와 함께 중단하며 자동 변환하지 않습니다.
잘못된 인물을 질문하더라도 실제 인물의 기록은 정정 근거로 남겨야 합니다.

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
- `doc:<path>#<heading>`는 topic 이관 시 기존 문서를 가리킵니다. 검토된 프로젝트 장부 이관에서는 `doc:<원본 장부 경로>#D-###`와 Git commit에 고정된 `repository_sources` 원본도 함께 사용합니다.

결정 장부의 ID는 유지합니다. 검토된 귀속 이관에서는 원본 행을 비권위적인 `이관됨` 안내로 바꾸고, 경로와 ID로 목적지 행을 가리킵니다. 일반 행의 불변 필드 및 삭제 금지 규칙은 유지합니다. 무손실 분리 형식과 승인 절차는 [CURATOR.md](CURATOR.md#project-boundaries-and-reviewed-ownership-transfers)를 따릅니다. 기존 metadata의 migration은 필요 없습니다.

marker는 주장 줄의 끝에 두며, 표 행에서는 마지막 칸 안에 둡니다. Obsidian 읽기 보기에서는 보이지 않습니다. Markdown 본문의 일부이므로 active 문자 수에 포함되고 색인됩니다. `item:`과 `sess:` token은 Curator 도구가 쓰므로 직접 고치지 않습니다. 직접 쓰는 것은 `doc:` token뿐입니다.
