---
pv_schema: 1
id: kn_persona_vault_curator_protocol_v1
subject_id: persona-vault.curator-protocol
memory_type: canonical
kind: procedure
review_state: human_accepted
temporal_state: current
outcome: not_applicable
projects: ["PersonaVault"]
topics: ["curation", "knowledge-management"]
provenance_mode: human_asserted
relations: {"supports":[],"contradicts":[],"supersedes":[]}
retrieval_tier: primary
privacy: normal
protocol_version: 22
---

# PersonaVault Curator Protocol

목표는 raw를 지식으로 통합하고 active Markdown을 줄이는 것이다. 감사 문서, 계획 설명이나
처리한 session 수는 성과가 아니다. Vault root에 이 파일을 `CURATOR.md`로 둔다.

```text
active_markdown_characters_after < active_markdown_characters_before
and every retired source item has a final disposition
and all critical validation probes pass
```

문자 수는 `.git/`, `.obsidian/`, `.tmp/`를 제외한 검색 범위의 Markdown 본문 합계다.
전체 맥락 보존은 원문 표현의 반복 보존이 아니라 귀속·근거·부정·예외·철회·충돌·시점·적용 조건의 보존이다.

운영 성과는 처리한 batch 수가 아니라 유입량 대비 실제 raw 제거량과 active Markdown 순감소다.
최종 보고에 적용된 감소량과 실제 측정한 읽기·통합·검토·기계 검사·색인 시간을 짧게 남긴다.
사용자 승인 대기 시간은 처리 시간과 구분한다. 측정할 수 있는 경우에만
토큰 사용량을 비교하고, 계정 사용량 %나 CLI 실행 시간을 전체 curation 토큰·시간으로 환산하지 않는다.
정리 속도가 유입을 따라가지 못하면 반복 읽기·계획 출력·승인 단위부터 조정한다.
지연 때문에 의미 보존·승인·삭제 게이트를 건너뛰거나 큰 batch를 읽지 않은 채 완료 처리하지 않는다.

## 1. 경계와 수명주기

- Curator task는 이 파일이 있는 Vault를 작업 root로 시작한다. 다른 root에서 Vault 경로만 편집하는 세션은 Curator가 아니다.
- 위 `id`와 파일명은 고정한다. 신뢰된 plugin은 이 ID를 감지하면 SessionStart 주입과 자동 대화 수집을 생략한다.
- Markdown과 Git이 원본이며 Qdrant와 SQLite `rag_index_meta`는 재생성 가능한 검색 projection이다.
- 승인 전 tracked 파일을 수정하지 않는다. 계획·초안은 Git에서 제외한 `.tmp/curating/`에만 둔다.
- `.git/`, `.obsidian/` 내부를 직접 읽거나 수정하지 않는다. 복구와 상태 확인은 Git 명령으로 한다.
- Secret을 원문 인용·요약에 복사하지 않는다. Privacy purge와 history rewrite는 별도 승인 대상이다.
- 사용자가 subject를 지정하지 않아도 Curator가 다음 대상을 고른다.

| 경로 | 역할 |
| --- | --- |
| `30_Conversations/raw/` | 날짜별 whole-file retirement 대상. 줄 편집·축약·이동은 하지 않는다. |
| `40_Agents/` | 기존 evidence. 새로 만들지 않으며 같은 coverage·승인 gate로 정리한다. |
| `10_User/` | 갱신 가능한 사용자 이해와 명시적으로 승인된 협업 규칙. |
| `20_Projects/` | 수정 가능한 프로젝트 현재 상태·결정·제약과 필요한 scoped history. |
| `50_Knowledge/` | 프로젝트를 넘어 재사용할 수 있는 지식. |
| `30_Conversations/summaries/` | 대화 순서와 기각한 대안 자체가 필요할 때만 사용. |
| `90_Private/` | 사용자 승인 범위에서만 수정. |

기존 소유 문서를 우선 갱신한다. 적절한 문서가 없고 독립적인 검색 가치가 있을 때만 새 문서를 만든다.
월별·session별 요약, index stub, 성향 점수, curation 설명 문서를 자동 생성하지 않는다.
`60_Curation/`의 과거 receipt는 active lifecycle로 다시 만들거나 유지하지 않는다.
이번 작업과 무관한 기존 문서를 정리한다는 이유로 수정·삭제 범위를 넓히지 않는다.

## 2. 한 번 선택하고 필요한 범위만 읽기

1. `git pull --ff-only` 후 full HEAD를 base로 고정한다. Dirty tracked Markdown이 있으면 중지한다.
2. 아래 명령으로 새 계획을 만든다. 기본 출력은 요약이며 전체 graph·item ID·hash는 JSON에 보관한다.
3. 선택 source와 관련 기존 target을 읽고, 같은 내용·hash는 작업 안에서 다시 읽거나 설명하지 않는다.
4. Source·target hash, base, 검토 범위와 probe를 계획에 묶는다. 새 근거·반례나 관련 파일 변경이 있으면 영향받은 판단을 다시 검토한다.

```bash
uv run pvg-wiki --vault . compact-plan --output .tmp/curating/plan.json
```

`--output`은 Vault의 `.tmp/curating/` 아래 새 JSON만 생성하며 기존 계획을 덮어쓰지 않는다.
다음 실행에는 다른 파일명을 쓴다. 옵션을 생략하면 파일도 만들지 않는다.
`--full`은 전체 출력이 필요한 진단용이다. 전체 JSON을 매번 대화에 붙여 넣지 않는다.
Production Qdrant가 내부 network에만 있으면 Gateway container에서 실행한다. DB·Qdrant·tracked Markdown은 수정하지 않는다.

### Queue와 읽기 예산

- Effective date는 dated raw path → `observed_at` → `created_at` → dated source path → root raw의 Git 최초 추가일 순이다. 날짜를 알 수 없으면 제외한다.
- 날짜는 정렬과 당일 raw 제외용이지 provenance나 승인 근거가 아니다. Plan은 `date_basis`를 보존한다.
- Commit된 source 중 당일·미래 raw와 유효한 보류를 제외한다. 가장 오래된 component가 시작점이다.
- 같은 시작 날짜라면 그 날짜의 source 문자 수가 큰 component, 이어 stable ID 순으로 고른다.
- Project 안에서 subject, error signature, topic 순으로 묶고 없으면 project, project도 없으면 독립 source로 둔다.
- 선택 component는 날짜를 넘어 오래된 순서로 읽되 기본 8파일·60,000자까지다. `--max-sources`, `--max-characters`로 조정할 수 있다. 다른 component는 다음 선택 때 다시 오래된 순서를 적용한다.
- 첫 파일 하나가 예산보다 크면 `oversized_source=true`로 알리고 파일 전체를 선택한다. 자동 절단·부분 삭제하거나 건너뛰지 않는다. 읽기는 나눠도 모든 item을 검토하기 전 파일을 retire하지 않는다.
- 예산은 source 본문 기준이며 토큰 한도가 아니다. Target과 reviewer context가 크면 다음 계획의 예산을 줄인다.

Graph는 source·item·기존 문서의 위치를 연결하는 작업 도구다. 같은 프로젝트 문서는 Qdrant 없이도
수정 후보로 찾는다. Current Qdrant는 기존 point ID로 후보만 보강하며 새 embedding을 만들지 않는다.
Stale·오류 시 structural 후보를 사용한다. 같은 component나 높은 score가 같은 의미·truth·승자·삭제 허가를 뜻하지 않는다.

### 개별 보류

해석 불가능한 파일 하나 때문에 전체 queue를 반복 감사하지 않는다. 그 파일만 유지하고 다음 후보를 처리한다.
기존 `.tmp/curating/deferred.json`에 아래 목록을 두고 `--deferrals .tmp/curating/deferred.json`으로 전달한다.

```json
[
  {
    "path": "30_Conversations/raw/YYYY/MM/DD/source.md",
    "sha256": "<source SHA-256>",
    "reason": "지시 주체를 구분할 수 없음",
    "revisit_when": "관련 원문 변경 또는 사용자 해명",
    "dependencies": {"20_Projects/Project/current.md": "<판단에 사용한 문서 SHA-256>"}
  }
]
```

Source·dependency가 바뀌거나 사라지면 CLI가 보류를 무효화한다. Curator는 `invalidated_deferrals`를
목록에서 제거하고 재판정한다. Hash 변화 없는 사용자 해명은 Curator가 확인한다.
조건이 그대로인 보류를 매번 다시 분석하지 않는다. 목록을 주지 않으면 다시 후보가 된다.
모두 보류면 조건만 보고 종료한다. Dirty/base/승인 문제는 개별 보류로 우회하지 않는다.

## 3. 한 번 해석하고 판정 묶기

User request → agent 행동 → 사용자 반응·정정 → 결과를 연결해 읽는다. 원본 user, agent delegation,
subagent result와 제공 자료의 귀속은 구분한다. Session 전체를 한 결론으로 만들지 않는다.

| Disposition | 의미 |
| --- | --- |
| `merge` | 기존 지식에 필요한 내용을 통합한다. |
| `replace` | 낡거나 잘못된 설명을 수정한다. |
| `already-covered` | 현재 지식이 같은 의미·조건을 충분히 보존한다. |
| `discard` | 진행 중계·반복·임시 오류·help/smoke·단순 복사 등 지속 가치가 없다. |
| `hold` | 귀속·의미·적용 범위를 안전하게 판단할 수 없다. |

같은 판정·대상·근거인 item들은 `review.items`의 한 항목으로 기록할 수 있다.
`id` 하나 또는 `ids` 목록 중 하나를 사용한다. 각 ID는 전체 검토에서 정확히 한 번 포함돼야 한다.
기본 판정·wildcard·“나머지는 discard”로 coverage를 채우지 않는다. 요지를 inventory·요약·보고서에 반복 복사하지 않는다.
ID·hash·문자 수는 도구가 만든 값을 사용하고 에이전트가 다시 생성하거나 손으로 나열하지 않는다.
동일 판정의 ID 목록도 계획에서 프로그램으로 선택하되, 어떤 item을 묶을지는 원문 검토로 판단한다.

```json
{"ids":["item:...","item:..."],"disposition":"already-covered","targets":["20_Projects/Project/current.md"],"reason":"같은 조건의 동일 결론이 해당 문서에 보존됨"}
```

`merge/replace/already-covered`는 실제 target을 지정한다. 판정이 다르거나 부정·철회·조건이 다른
item은 한 이유로 뭉개지 않는다. 제공 자료의 단순 복사는 버릴 수 있어도 그 자료를 사용한 결정·새 결과는 별도 판단한다.
중요한 숫자·날짜·버전, 재사용할 성공·실패·원인, 사용자 제약, 다음 행동을 바꾸는 결과와 미해결 질문은 보존한다.

- 사용자 지시는 원본 main conversation의 user 발화에서만 인정한다. Subagent 위임문은 agent가 쓴 지시다.
- 사용자가 붙인 문서·인용문은 제공 자료이지 사용자의 선호·주장·정체성이 아니다.
- 사용자는 자신의 선호·제약의 authority지만 기술 사실·외부 상태는 evidence로 판단한다.
- 충돌은 주체·근거·시점·조건과 미해결 상태를 보존할 수 있으면 통합한다. 미해결이라는 이유만으로 raw를 영구 보류하지 않는다.
- 미해결 conflict는 canonical에 `conflict_state: unresolved`와 양쪽 주장·확인 조건을 남긴다. 검색은 `review_required`여야 하며 압축을 이유로 resolved로 바꾸지 않는다.

## 4. 기존 프로젝트 지식도 다시 쓰기

`human_accepted`는 현재 버전이 승인됐다는 뜻이지 내용이 영원히 정확하거나 파일이 불변이라는 뜻이 아니다.
Raw를 기존 프로젝트 문서 뒤에 덧붙이는 데 그치지 않고, 새 근거에 맞춰 현재 설명을 재작성한다.

- 같은 사실의 반복 문장은 합치고, 잘못된 설명은 고치며, 더 이상 현재가 아닌 지시는 현재 설명에서 제거한다.
- 기존 target에만 있는 유효한 제약·예외·반례·미해결 사항은 새 source에 없다는 이유로 지우지 않는다.
- 과거 상태가 이후 판단에 필요하면 시점·적용 범위를 짧게 보존한다. 변경 때마다 새 history 파일이나 수정 일지를 만들 필요는 없다. 원문 diff는 Git에 남는다.
- 더 최신이라는 이유나 기존 canonical에 적혀 있다는 이유만으로 참을 결정하지 않는다. 같은 범위의 반대 근거가 남으면 충돌로 보존한다.
- Target의 기존 의미와 새 evidence를 함께 검토하고, 문장 통합·교체·제거가 포함된 exact patch를 승인받는다. 기존 승인 상태를 새 patch 승인으로 재사용하지 않는다.
- 이 CLI의 `delete_paths`는 raw/legacy source용이다. Canonical 파일 자체의 삭제·rename을 자동 허용하지 않는다.

예: 기존 “항상 A를 사용”을 새 evidence가 “X 조건에서는 B가 필요”로 정정했다면,
기존 문장을 불변으로 남긴 채 주석을 누적하지 말고 “일반 조건 A, X 조건 B”로 고친다.
다른 상황의 유효한 예외와 남은 불확실성은 유지한다.

### 사용자 이해

같은 읽기 과정에서 관련 `10_User/`·프로젝트 규칙을 새 근거와 비교한다. 전체 사용자 프로필을 매번 다시
만들거나 반드시 변경하지 않는다. 새 의미가 없으면 `review.user_knowledge`에 `unchanged`와 이유만 남긴다.

- 불만뿐 아니라 선택한 대안·명시적 만족·승인을 함께 본다. 강한 표현을 감정·성격으로 단정하지 말고 재발 방지 조건을 찾는다.
- 직접 설명, 조건부 관찰, 추론 가설을 구분한다. 침묵·“진행해”·agent의 성공 선언은 지속 선호의 승인이 아니다.
- 명시적 지속 선호는 한 번의 evidence로 검토할 수 있다. 추론한 전역 선호는 독립적인 세 session 이상의 패턴이 필요하다. 재전송·인용·agent 반복은 독립 evidence가 아니며 이 문턱도 확정이나 승인을 뜻하지 않는다.
- 새 근거·반례로 기존 이해를 좁히거나 수정·철회한다. 현재 설명, 조건, 대표 근거·반례의 요지와 검토 시점을 간결히 남긴다.
- 관련 기존 문서를 갱신하고 필요할 때만 `10_User/PROFILE.md`로 시작한다. 프로필은 검색할 설명이지 행동 지시가 아니다.
- `10_User/WORKING_AGREEMENT.md`에는 명시적으로 승인된 전역 규칙만 8,000자 이하로 둔다. 프로젝트 규칙은 해당 프로젝트에 둔다.
- 프로필 수정과 agreement 변경은 같은 plan에서도 구분한다. 프로필 승인으로 가설이 사실이 되거나 전역 지시가 승인되는 것은 아니다.
- 사용자 지식도 의미 보존 후 같은 retirement gate를 적용한다. 반복 원문이나 삭제 source의 live reference를 누적하지 않는다.

## 5. 한 승인 단위로 끝내기

1. **준비:** base·source·target을 고정하고 한 번 읽는다. 기존 plan JSON과 canonical patch가 작업 산출물이며 중복 계획 문서를 만들지 않는다.
2. **통합:** item 판정과 최소 patch를 함께 만든다. 결과를 보고 맞추기 전에 critical 의미·귀속·정정·예외·충돌을 검증할 probe를 정한다.
3. **검토·승인:** 작성자가 아닌 read-only context가 원문 근거와 target 전후 patch를 독립 검토한다. 요약문만 재검토하지 않는다. 검토 후 `compact-review --record`로 hash를 기록하고 exact 승인을 받는다.
4. **적용·완료 검사:** exact 승인을 확인하고 적용한 뒤 `compact-finish`를 한 번 실행한다. 실패로 patch가 달라지면 필요한 재검토·재승인 후 다시 검사한다.
5. **Commit:** 최종 검사·검색이 통과하면 승인된 commit을 만든다. Item·파일마다 검사·색인·commit을 반복하지 않는다.

한 plan이 한 승인·완료 단위다. 여러 소묶음은 그 plan의 source를 나눠 읽고 검토하는 방식으로 처리한다.
독립적인 plan이나 서로 다른 승인을 자동 합치지 않는다. `compact-finish` 전에 `compact-check`,
`health`, `conflicts list`, 별도 reindex를 정상 절차로 반복 실행하지 않는다.

검토 결과는 source·target hash, patch hash와 probe에 묶어 재사용한다. 변경 없는 의미 판정을 다시 서술하지 않는다.
Reviewer에는 해당 source·target·patch·probe와 필요한 규칙만 전달한다. 전체 부모 대화나 이미 끝난
batch의 계획·검토를 다시 전달하지 않는다. 새 감사 문서 대신 기존 계획에 결함과 통과 여부만 남긴다.
새 근거·반례·patch 변경에 영향받은 판단은 재검토하고, base drift나 승인 변경은 아래 게이트를 따른다.
이는 검토 결과 재사용이지 이전 작업의 승인을 재사용하는 권한이 아니다.

### 검토 checkpoint

기존 `review`에 `drafts`(변경 target → Vault-relative 초안 경로), `probes`를 추가한다.
변경 없는 target은 `drafts`에서 생략한다. 추가로 판단에 사용한 evidence·policy는 `dependencies`에
path를 기록한다. 전체 source context·기존 후보·root CURATOR는 기본적으로 hash에 묶인다.

```json
{
  "drafts": {"20_Projects/Project/current.md": ".tmp/curating/current.md"},
  "dependencies": ["10_User/WORKING_AGREEMENT.md"],
  "probes": [
    {"query": "Project의 현재 선택", "bundle": "current", "expect_paths": ["20_Projects/Project/current.md"], "answer_state": "supported"},
    {"query": "실제로 존재하지 않는 결정", "bundle": "current", "answer_state": "abstain"},
    {"query": "잘못된 사람에게 귀속한 결정", "bundle": "current", "forbid_paths": ["20_Projects/Project/current.md"], "answer_state": "abstain"}
  ]
}
```

이는 `review`에 덧붙일 필드 예시이며 `items/delete_paths/user_knowledge`를 대체하지 않는다.
Probe는 실제 원문과 질문에 맞춰 작성한다. `expect_paths`는 모두 검색되어야 할 경로이고,
`forbid_paths`는 반환되면 안 되는 경로다. `bundle`과 기대 `answer_state`는 명시한다(최대 20개).

```bash
# 승인 전: tracked 파일은 그대로 두고 초안의 기계 검사와 재검토할 ID만 확인
uv run pvg-wiki --vault . compact-review .tmp/curating/plan.json
# 독립 의미 검토가 실제로 끝난 뒤에만 기록
uv run pvg-wiki --vault . compact-review .tmp/curating/plan.json --record
```

`--record`만 로컬 plan의 `review_checkpoint`를 갱신한다. LLM 검토를 수행하거나 승인을 생성하는
명령이 아니다. CLI는 원문/target 전후 hash·판정·probe를 비교하며, 바뀌지 않은 item은 재사용한다.
같은 base/plan의 일부 변경을 이미 적용했더라도 새 초안은 Git base와 비교해 재검토할 수 있다.
Raw를 복구하거나 먼저 새 초안을 적용할 필요는 없다. 계획 밖 변경·남은 raw의 내용 수정은 차단한다.
재검토 기록이 있어도 새 초안 적용에는 별도 exact 승인이 필요하고, 적용 내용이 초안과 다르면 finish가 거부한다.
독립 target 초안 하나가 바뀌면 그 target을 쓰는 판정만 무효화한다. 공유 source context·규칙·probe가
바뀌면 보수적으로 함께 재검토한다. 파일 변경 없는 새 사용자 해명·반례도 이유·dependency·probe에
반영해 검토를 무효화한다. 관계없는 의미 판단까지 자동 재실행하는 모델 호출은 없다.

출력의 `patch_sha256`은 literal patch 파일이 아니라 변경 경로와 Markdown 전후 hash의 binding이다.
`scope_sha256`은 이 binding과 plan의 결정 내용을 묶고, 실행 기록인 두 checkpoint만 제외한다.
승인은 이 scope와 검토 가능한 실제 diff에 대해 받는다. Checkpoint 저장만으로 승인 scope가 달라지지
않지만 source·target·base·판정·probe·계획이 바뀌면 새 승인이 필요하다.

Probe는 모든 critical 의미를 덮되 같은 질문으로 여러 item을 검증할 수 있다.
귀속·정정·충돌·시간·적용 조건과 사용자 선호의 과잉 일반화는 해당 변경에서 확인하고,
존재하지 않는 사실과 잘못된 사람 귀속의 negative control 두 개를 포함한다.
프로필 변경은 가설·반례를 보존하고 행동 지시로 오인하지 않는지 확인한다.
의미 검토는 source와 patch를 비교하고, 적용 후에는 그 patch가 적용됐는지 기계적으로 확인한다.
검색 probe는 최종 index의 발견 가능성과 answer_state를 검증하는 별도 책임이다.

```bash
# 승인된 exact patch 적용 후, Gateway와 같은 Vault/DB/Qdrant 설정에서 실행
uv run pvg-wiki --vault . compact-finish .tmp/curating/plan.json
```

`review.delete_paths`에 승인된 source 삭제 목록, `review.user_knowledge`에 `updated|unchanged`와 이유를 둔다.
선택됐지만 보존하는 source도 item 판정은 마치고 내용은 그대로 둔다.
이 명령은 Git base 대비 coverage·중복 ID·보류 보존·target·변경 범위·문자 감소를 검사하고,
전후 Wiki 분석으로 새 metadata/reference/cycle 오류와 conflict 변화를 한 번에 보고한다.
적용 내용이 검토한 초안과 같은지 확인한 뒤 기존 증분 색인을 호출하고, 삭제 path의 Qdrant point가
0인지와 모든 검색 probe의 경로·answer_state를 검사한다. 각 검색은 `refresh=false`다.
이미 최신인 index는 재색인하지 않고, 같은 입력으로 완료된 재실행은 저장된 검색 결과도 재사용한다.
`finish_checkpoint`는 plan 안에만 저장한다. 실패·quota·stale/fallback·변경 감지 시 완료 기록을 쓰지 않는다.
Quota가 풀린 뒤 같은 명령으로 재시도하며, 새 DB 초기화·모델 변경·collection 재생성은 이 명령의 범위가 아니다.
기존 Gateway DB와 호환되는 index를 사용해야 하며 CLI/server의 색인은 공통 잠금으로 중복 실행을 거부한다.
DB·index를 사용하는 다른 Gateway 설정이나 다른 Vault checkout에 임의로 연결하지 않는다.

검사 실패는 `blocked`와 종료 코드 1이다. 실행 시간은 `timings`에 최초 Vault 읽기·기계 검사·색인/검색을 나눠 표시한다.
명령은 승인·의미 보존을 자동 증명하지 않으며 `approval=not_checked`를 유지한다.
자동 apply/DELETE/commit/push도 하지 않는다. `compact-check`는 색인 없는 read-only 진단이 필요할 때만 쓴다.
전체 Markdown link의 live inbound reference와 삭제될 source의 전송 checkpoint도 따로 확인한다.

## 6. 승인·삭제 게이트

현재 대화의 exact plan에 대한 승인을 받는다. 전체 graph 대신 아래 요약과 검토 가능한 patch를 제시한다.

```text
Base commit: <full commit id>
Source/target paths and SHA-256: <plan JSON>
Plan scope SHA-256 / target diff binding: <scope_sha256> / <patch_sha256>
Delete exactly: <paths and SHA-256>
Active Markdown characters: <before> -> <after> (<delta>, <ratio>)
Validation probes: <passed>/<total>
Requested external actions: <none|commit|push>
```

Source·target hash, base, path, patch나 외부 action이 달라지면 승인은 무효다.
“계속해”, “정리해”는 새로운 삭제·commit·push 승인으로 보지 않는다.

Retire할 raw/legacy source는 모두 다음을 만족해야 한다.
- Commit되어 있고 raw는 현재 날짜가 아니며, whole-file 전체 item이 merge/replace/already-covered/discard로 판정됨.
- Hold가 하나라도 있는 파일은 보존하고, durable 의미는 승인 target에서 source support와 함께 검증됨.
- 활성 Codex·Claude client가 날짜별 payload checkpoint를 지원하고 해당 fragment 전송이 성공함. 확인할 수 없으면 재전송 위험 때문에 유지함.
- Exact 삭제 path·hash가 일치하고 live inbound reference가 없으며 active Markdown 문자 수가 감소함.
- 새 무결성 오류가 없고 critical probe가 통과하며, 최종 Qdrant fingerprint가 Vault와 일치하고 삭제 source가 검색되지 않음.

Cloudflare quota나 Qdrant 오류로 최종 검색을 검증하지 못하면 destructive 작업을 완료하거나 commit하지 않는다.

주의: raw의 날짜는 hook 실행 PC의 현지 날짜다. 전날 기록이 늦게 재전송되어 파일이 바뀔 수 있다.
적용 전에 전송 확인이 없거나 source hash가 계획과 달라지면 승인된 plan을 조용히 고쳐 해당 파일만
빼지 않는다. 적용을 멈추고 plan을 다시 만들어 새 승인을 받는다(제외하려면 새 승인이 필요하다). 삭제 직전의
exact hash 확인은 동시 쓰기에 대한 원자적 보장이 아니며, 여러 writer 사이의 분산 일관성을 주장하지 않는다.

기존의 관계없는 경고를 매번 해결하느라 범위를 넓히지 않되 새 오류와 이번 변경의 의미 손실은 막는다.
Conflict 해결은 canonical 상태·본문과 exact contradicts edge에 반영하고, reopen 때 해당 edge를 복원한다.
새 receipt/conflict event를 만들지 않는다.

## 7. 감사·복구·중지

Target 변경과 source 삭제는 승인된 single-parent compaction commit에 함께 담고 다음을 남긴다.

```text
PVG-Curation-Base: <full parent commit>
PVG-Curation-Plan-SHA256: <scope_sha256>
PVG-Active-Characters: <before> -> <after>
PVG-Validation: <passed>/<total>
```

삭제 원문은 `git show <base-commit>:<path>`로 복구한다. 이는 active-store eviction이지 Git history의
개인정보 완전 삭제가 아니다. Main force-push를 하지 않고 full-repository backup 하나를 유지한다.
새 DB·graph service·scheduler·audit 저장소·tag·archive branch·Git note를 도입하지 않는다.

Dirty tracked Markdown, base/upstream drift, 승인 불일치, coverage·의미·검색 검증 실패, secret/purge나
범위 밖 변경이 필요하면 적용을 중지한다. 승인 전에는 tracked 파일을 수정하지 않고,
적용 후 실패면 commit·push하지 않으며 복구도 승인 범위에서만 한다.
개별 해석 불가능 source는 보류할 수 있지만 이미 승인된 patch에서 빼거나 다른 batch로 바꾸려면 새 승인이 필요하다.
