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

[English](https://github.com/mykim0409/persona-vault-gateway/blob/main/docs/CURATOR.md) | **한국어**

> 이 문서는 사람이 읽기 위한 한국어 번역이며 별도의 권위 있는 protocol이 아닙니다. Agent가 실제로 따르는 규칙이자 Vault 루트에 `CURATOR.md`로 두는 파일은 원본 영어 [CURATOR.md](https://github.com/mykim0409/persona-vault-gateway/blob/main/docs/CURATOR.md)입니다. 이 번역을 Vault에 복사하거나 agent에게 넘기지 마세요. 두 문서가 다르면 영어 원본이 우선합니다. 아래 frontmatter 식별자, 명령, JSON, 경로는 번역하지 않고 원본과 같게 유지합니다.

# PersonaVault Curator Protocol

목표는 raw 기록을 지식으로 통합하고 active Markdown을 줄이는 것입니다. 감사 문서, 계획 설명, 처리한 세션 수는
결과가 아닙니다. 이 파일은 Vault 루트에 `CURATOR.md`로 둡니다.

```text
active_markdown_characters_after < active_markdown_characters_before
and every retired source item has a final disposition
and all critical validation probes pass
```

문자 수는 `.git/`, `.obsidian/`, `.tmp/`를 제외한, 검색 범위 안의 Markdown 본문 전체 크기입니다.
전체 맥락을 보존한다는 것은 원문 표현을 반복해서 보존한다는 뜻이 아닙니다. 귀속(attribution), 근거, 부정, 예외, 철회, 충돌, 시점, 적용 조건을 보존한다는 뜻입니다.

운영 결과는 처리한 batch 수가 아니라 유입량 대비 실제로 제거한 raw의 양과 active Markdown의 순감소로 측정합니다.
최종 보고에는 적용한 감소량과, 읽기·통합·검토·기계적 점검·색인에 실제로 측정한 시간을 간단히 적습니다.
사용자 승인 대기 시간은 처리 시간과 분리합니다. token 사용량은 측정할 수 있을 때만 비교하고,
계정 사용 비율이나 CLI 실행 시간을 curation 전체의 token이나 시간으로 환산하지 않습니다.
curation 속도가 유입을 따라가지 못하면 먼저 반복 읽기, 계획 출력, 승인 단위를 조정합니다.
지연을 이유로 의미 보존, 승인, 삭제 gate를 건너뛰지 않으며, 읽지 않은 대규모 batch를 완료로 표시하지 않습니다.

## 1. 경계와 수명주기

- Curator 작업은 이 파일이 있는 Vault를 작업 루트로 시작합니다. 다른 루트에서 시작해 Vault 경로만 수정하는 세션은 Curator가 아닙니다.
- 위의 `id`와 파일 이름은 고정입니다. 신뢰된 plugin이 이 ID를 감지하면 SessionStart 주입과 자동 대화 capture를 건너뜁니다.
- Markdown과 Git이 source of truth입니다. Qdrant와 SQLite `rag_index_meta`는 다시 만들 수 있는 검색 projection입니다.
- 승인 전에는 tracked 파일을 수정하지 않습니다. 계획과 초안은 Git에서 제외되는 `.tmp/curating/`에만 둡니다.
- `.git/`이나 `.obsidian/` 내부를 직접 읽거나 수정하지 않습니다. 복구와 상태 확인에는 Git 명령을 씁니다.
- secret을 원문 인용이나 요약에 복사하지 않습니다. 개인정보 purge와 history rewrite는 별도 승인이 필요합니다.
- 사용자가 주제를 지정하지 않아도 다음 대상은 Curator가 고릅니다.

| 경로 | 역할 |
| --- | --- |
| `30_Conversations/raw/` | 날짜별 파일 전체를 폐기하는 대상. 줄을 고치거나, 줄이거나, 옮기지 않습니다. |
| `40_Agents/` | 기존 evidence. 새 파일을 만들지 않으며, 같은 coverage·승인 gate 아래에서 통합합니다. |
| `10_User/` | 갱신 가능한 사용자 이해와 명시적으로 승인된 협업 규칙. |
| `20_Projects/` | 수정 가능한 현재 프로젝트 상태, 결정, 제약, 필요한 범위의 이력. |
| `50_Knowledge/` | 프로젝트 간에 재사용할 수 있는 지식. |
| `30_Conversations/summaries/` | 대화의 순서와 기각된 대안 자체가 필요할 때만 사용. |
| `90_Private/` | 사용자가 승인한 범위 안에서만 수정. |

기존 소유 문서를 먼저 갱신합니다. 적합한 문서가 없고 독립적인 검색 가치가 있을 때만 새 문서를 만듭니다.
월별·세션별 요약, index stub, 성격 점수, curation 설명 문서를 자동으로 만들지 않습니다.
`60_Curation/`의 과거 receipt를 active 수명주기로 다시 만들거나 유지하지 않습니다.
이번 작업과 무관한 기존 문서를 정리한다는 이유로 수정·삭제 범위를 넓히지 않습니다.

## 2. 한 번 선택하고 필요한 것만 읽기

1. `git pull --ff-only` 후 전체 HEAD를 base로 고정합니다. dirty tracked Markdown이 있으면 중단합니다.
2. 아래 명령으로 새 계획을 만듭니다. 기본 출력은 요약이며 전체 graph, item ID, hash는 JSON에 보관합니다.
3. 선택된 source와 관련된 기존 target을 읽습니다. 같은 내용이나 hash를 작업 안에서 다시 읽거나 다시 설명하지 않습니다.
4. source와 target hash, base, 검토 범위, probe를 계획에 묶습니다. 새 evidence, 반례, 관련 파일의 변경이 생기면 영향받는 판단을 다시 검토합니다.

```bash
uv run pvg-wiki --vault . compact-plan --output .tmp/curating/plan.json
```

`--output`은 Vault의 `.tmp/curating/` 아래에 새 JSON 파일만 만들며 기존 계획을 덮어쓰지 않습니다.
다음 실행에는 다른 파일 이름을 쓰세요. 옵션을 생략하면 파일을 만들지 않습니다.
`--full`은 전체 출력이 필요한 진단용입니다. 전체 JSON을 매번 대화에 붙여 넣지 마세요.
운영 Qdrant가 내부 네트워크에만 있으면 Gateway 컨테이너에서 실행하세요. DB, Qdrant, tracked Markdown은 수정하지 않습니다.

### 대기열과 읽기 예산

- 유효 날짜는 다음 순서를 따릅니다: 날짜가 있는 raw 경로 -> `observed_at` -> `created_at` -> 날짜가 있는 source 경로 -> 루트 raw의 Git 최초 추가 날짜. 날짜를 정할 수 없으면 그 source는 제외합니다.
- 날짜는 정렬과 같은 날 raw 제외에 쓰며, provenance나 승인 근거가 아닙니다. 계획은 `date_basis`를 보존합니다.
- commit된 source 중 같은 날과 미래의 raw, 유효한 deferral은 제외합니다. 가장 오래된 component가 시작점입니다.
- 시작 날짜가 같으면 그 날짜의 source 문자 수가 더 큰 component를 고르고, 그다음은 안정적인 ID 순입니다.
- 한 프로젝트 안에서는 subject, 그다음 error signature, 그다음 topic으로 묶습니다. 없으면 프로젝트로 묶고, 프로젝트도 없으면 source를 독립으로 봅니다.
- 선택된 component는 날짜를 가로질러 오래된 것부터 읽으며 기본값은 파일 8개와 60,000자까지입니다. `--max-sources`와 `--max-characters`로 조정합니다. 다른 component는 다음 선택 때 다시 오래된 순서를 적용합니다.
- 첫 파일 하나만으로 예산을 넘으면 `oversized_source=true`로 표시하고 파일 전체를 선택합니다. 자동으로 자르거나, 일부만 삭제하거나, 건너뛰지 않습니다. 읽기를 나누더라도 그 파일의 모든 item을 검토하기 전에는 파일을 폐기하지 않습니다.
- 예산은 source 본문 크기 기준이며 token 한도가 아닙니다. target과 reviewer의 context가 크면 다음 계획의 예산을 줄이세요.

graph는 source, item, 기존 문서의 위치를 연결하는 작업 도구입니다. 같은 프로젝트의 문서는 Qdrant가 없어도
수정 후보로 찾습니다. 최신 Qdrant는 기존 point ID를 사용해 후보만 보충하며 새 embedding을 만들지 않습니다.
최신이 아니거나 오류가 나면 구조 기반 후보를 씁니다. 같은 component나 높은 점수는 같은 의미, 진실, 승자, 삭제 허가를 뜻하지 않습니다.

### 개별 deferral

해석할 수 없는 파일 하나 때문에 전체 대기열을 반복해서 감사하지 않습니다. 그 파일만 보류하고 다음 후보를 처리합니다.
아래 목록을 기존 `.tmp/curating/deferred.json`에 넣고 `--deferrals .tmp/curating/deferred.json`으로 전달합니다.

```json
[
  {
    "path": "30_Conversations/raw/YYYY/MM/DD/source.md",
    "sha256": "<source SHA-256>",
    "reason": "Cannot tell who issued the instruction",
    "revisit_when": "The related source changes or the user clarifies",
    "dependencies": {"20_Projects/Project/current.md": "<SHA-256 of the document used for the judgment>"}
  }
]
```

source나 dependency가 바뀌거나 사라지면 CLI가 deferral을 무효화합니다. Curator는 목록에서 `invalidated_deferrals`를 제거하고
다시 판단합니다. hash 변경 없이 생긴 사용자 해명은 Curator가 확인합니다.
조건이 바뀌지 않은 deferral은 매번 다시 분석하지 않습니다. 목록을 주지 않으면 그 파일들은 다시 후보가 됩니다.
전부 보류되면 조건만 보고하고 끝냅니다. dirty/base/승인 문제를 개별 deferral로 우회하지 않습니다.

## 3. 한 번 해석하고 판단을 묶기

사용자 요청 -> agent 행동 -> 사용자 반응이나 정정 -> 결과의 연결로 읽습니다. 원 사용자의 귀속, agent 위임,
subagent 결과, 제공된 자료를 구분합니다. 세션 전체를 하나의 결론으로 만들지 않습니다.

| Disposition | 의미 |
| --- | --- |
| `merge` | 필요한 내용을 기존 지식에 통합합니다. |
| `replace` | 오래되었거나 틀린 설명을 바로잡습니다. |
| `already-covered` | 현재 지식이 같은 의미와 조건을 이미 충분히 보존하고 있습니다. |
| `discard` | 진행 서술, 반복, 일시적 오류, help/smoke 출력, 단순 복사처럼 지속적 가치가 없습니다. |
| `hold` | 귀속, 의미, 적용 범위를 안전하게 판단할 수 없습니다. |

판단, target, 근거가 같은 item은 `review.items`에 하나의 항목으로 기록할 수 있습니다.
단일 `id`나 `ids` 목록 중 하나를 씁니다. 각 ID는 전체 review에서 정확히 한 번만 포함해야 합니다.
기본 판단, wildcard, "나머지는 discard"로 coverage를 채우지 않습니다. 요지를 inventory, 요약, 보고서에 반복해서 복사하지 않습니다.
도구가 만든 ID, hash, 문자 수를 사용하며, agent가 이를 다시 만들거나 손으로 나열해서는 안 됩니다.
같은 판단의 ID 목록도 계획에서 프로그램적으로 고르되, 어떤 item을 묶을지는 source를 검토해 결정합니다.

```json
{"ids":["item:...","item:..."],"disposition":"already-covered","targets":["20_Projects/Project/current.md"],"reason":"The same conclusion under the same conditions is preserved in that document"}
```

`merge/replace/already-covered`는 실제 target을 지정합니다. 판단이 다르거나 부정, 철회, 조건이 다른 item을
하나의 이유로 뭉뚱그리지 않습니다. 제공된 자료의 단순 복사는 discard할 수 있지만, 그 자료를 사용해 내린 결정과 새 결과는 따로 판단합니다.
중요한 숫자, 날짜, 버전, 재사용 가능한 성공·실패와 원인, 사용자 제약, 다음 행동을 바꾸는 결과, 미해결 질문을 보존합니다.

- 사용자 지시는 원 main 대화에서 사용자가 한 발화만 인정합니다. subagent에게 보낸 위임 메시지는 agent가 쓴 지시입니다.
- 사용자가 붙여 넣은 문서나 인용은 제공된 자료이며 사용자의 선호, 주장, 정체성이 아닙니다.
- 사용자는 자신의 선호와 제약에 대한 권위자이지만, 기술적 사실과 외부 상태는 근거로 평가합니다.
- 주장 주체, 근거, 시점, 조건, 미해결 상태를 보존할 수 있다면 충돌도 통합합니다. 충돌이 미해결이라는 이유만으로 raw를 영구히 보류하지 않습니다.
- 미해결 충돌은 canonical에 두 주장과 이를 확인할 조건과 함께 `conflict_state: unresolved`를 남깁니다. 검색은 `review_required`를 반환해야 하며, compaction을 이유로 resolved로 바꾸지 않습니다.

## 4. 기존 프로젝트 지식도 다시 쓰기

`human_accepted`는 현재 버전이 승인되었다는 뜻입니다. 내용이 영원히 정확하다거나 파일이 불변이라는 뜻이 아닙니다.
기존 프로젝트 문서 뒤에 raw를 덧붙이는 데서 멈추지 말고, 새 evidence에 맞게 현재 설명을 다시 씁니다.

- 같은 사실을 반복하는 문장을 합치고, 틀린 설명을 고치고, 더 이상 현재가 아닌 지시를 현재 설명에서 제거합니다.
- 기존 target에만 있는 유효한 제약, 예외, 반례, 미해결 항목을 새 source에 없다는 이유로 삭제하지 않습니다.
- 이후 판단에 과거 상태가 필요하면 그 시점과 적용 범위를 짧게 보존합니다. 변경마다 새 이력 파일이나 change log를 만들 필요는 없습니다. 원본의 diff는 Git에 남습니다.
- 더 최신이라거나 기존 canonical에 쓰여 있다는 이유만으로 진실을 정하지 않습니다. 같은 범위의 반대 evidence가 남아 있으면 충돌로 보존합니다.
- target의 기존 의미를 새 evidence와 함께 검토하고, 문장 병합, 교체, 제거를 포함한 정확한 patch에 대해 승인을 받습니다. 기존 승인 상태를 새 patch의 승인으로 재사용하지 않습니다.
- 이 CLI의 `delete_paths`는 raw/legacy source용입니다. canonical 파일 자체의 삭제나 이름 변경은 자동으로 허용되지 않습니다.

예: 새 evidence가 기존의 "항상 A를 사용"을 "조건 X에서는 B가 필요"로 정정한다면,
기존 문장을 불변으로 둔 채 주석을 쌓지 말고 "일반 조건에서는 A, 조건 X에서는 B"로 다시 씁니다.
다른 상황에 대한 유효한 예외와 남은 불확실성은 유지합니다.

### 문체

위와 같이 통합할 때는 기본적으로 STE(Simplified Technical English)에서 영감을 받은 문체를 씁니다. ASD-STE100 준수는 필요하지 않으며,
별도의 단계, 체크리스트, LLM, 도구, schema를 추가하지 않습니다. 같은 patch와 기존 의미 검토·승인 안에서 끝냅니다.

- 중복된 진술은 한 곳에 모으고, 지속적 가치가 없는 진행 서술은 제거합니다.
- 한 문장에 하나의 주장이나 하나의 행동만 씁니다. 행위자와 조건을 밝힙니다.
- 하나의 일관된 용어를 씁니다. 검색에 도움이 되면 원어나 약어를 처음에만 함께 적습니다.
- source 귀속, 부정, 불확실성, 예외, 날짜와 적용 범위, 근거의 강도를 줄이지 않습니다. 관찰을 사실이나 정책으로,
  검토만 한 선택지를 채택된 것으로, 맥락에 한정된 선호를 보편적 경향으로 바꾸지 않습니다.
- 한국어 문서에 영어, 승인된 영어 사전, 영어 단어 수 제한을 강제하지 않습니다.
- raw를 제자리에서 다시 쓰지 않습니다. 문체만을 이유로 기존 문서를 일괄 수정하지 않습니다.

비교, 분기, 의존, 상태, 순서가 prose보다 표나 Mermaid로 읽기 쉬울 때만 표나 Mermaid를 고릅니다. 모든 문서에 필요하지는 않습니다.
긴 설명을 대체할 때만 쓰고, 같은 내용을 텍스트와 diagram에 중복하지 않습니다. 검색, 접근성, Mermaid를 지원하지 않는 renderer를 위해
핵심 관계와 조건은 짧은 문장으로 유지합니다. evidence가 뒷받침하지 않는 node, edge, 인과를 만들어 내지 않습니다.
diagram은 텍스트와 같은 patch에서 같은 승인을 받습니다. 별도 승인, diagram 서비스, graph DB, 자동 생성 pipeline을 추가하지 않습니다.
token 절감이나 renderer 이식성을 약속하지 않습니다. 의미를 보존하며, 기존의 active Markdown 순감소 gate도 그대로 적용됩니다.

예: "Deployment runs after approval. The approver is the user. Before approval, only drafts are created."

### 사용자 이해

같은 읽기 과정에서 관련 `10_User/`와 프로젝트 규칙을 새 evidence와 비교합니다. 사용자 profile 전체를 매번 다시 만들거나
변경을 요구하지 않습니다. 새로운 의미가 없으면 `review.user_knowledge`에 `unchanged`와 이유만 남깁니다.

- 불만뿐 아니라 선택한 대안, 명시적 만족, 승인도 봅니다. 강한 표현에서 감정이나 성격을 추정하지 말고, 재발을 막는 조건을 찾으세요.
- 직접 진술, 조건부 관찰, 추론된 가설을 구분합니다. 침묵, "go ahead", agent의 성공 선언은 지속적 선호의 승인이 아닙니다.
- 명시적인 지속적 선호는 evidence 하나로도 검토할 수 있습니다. 추론한 전역 선호에는 독립된 세션 세 개 이상에 걸친 패턴이 필요합니다. 재전송, 인용, agent의 반복은 독립 evidence가 아니며, 이 기준 역시 확정이나 승인을 뜻하지 않습니다.
- 새 evidence나 반례에 따라 기존 이해를 좁히거나 정정하거나 철회합니다. 현재 설명, 조건, 대표 evidence와 반례의 요지, 검토 시점을 간단히 기록합니다.
- 관련된 기존 문서를 갱신하며, 필요할 때만 `10_User/PROFILE.md`부터 시작합니다. profile은 검색되는 설명이지 행동 지시가 아닙니다.
- 명시적으로 승인된 전역 규칙만 `10_User/WORKING_AGREEMENT.md`에 8,000자 이하로 둡니다. 프로젝트 규칙은 해당 프로젝트에 둡니다.
- 같은 계획 안에서도 profile 수정과 agreement 변경을 분리합니다. profile 승인이 가설을 사실로 만들거나 전역 지시를 승인하지 않습니다.
- 의미가 보존된 뒤에는 사용자 지식에도 같은 폐기 gate를 적용합니다. 반복된 raw 텍스트나 삭제된 source에 대한 살아 있는 참조를 쌓지 않습니다.

## 5. 하나의 승인 단위로 끝내기

1. **준비:** base, source, target을 고정하고 한 번 읽습니다. 기존 계획 JSON과 canonical patch가 작업 산출물이며, 중복된 계획 문서를 만들지 않습니다.
2. **통합:** item 판단과 최소 patch를 함께 만듭니다. 결과에 맞춰 조정하기 전에, 핵심 의미, 귀속, 정정, 예외, 충돌을 검증할 probe를 정의합니다.
3. **검토와 승인:** 작성자가 아닌 읽기 전용 context가 source evidence와 target의 변경 전후 patch를 독립적으로 검토합니다. 요약만 다시 검토하지 않습니다. 검토 후 `compact-review --record`로 hash를 기록하고 정확한 승인을 받습니다.
4. **적용과 완료 점검:** 정확한 승인을 확인하고 적용한 뒤 `compact-finish`를 한 번 실행합니다. 실패로 patch가 바뀌면 필요한 재검토와 재승인 후 점검을 다시 실행합니다.
5. **Commit:** 최종 점검과 검색이 통과하면 승인된 commit을 만듭니다. item이나 파일마다 점검, 색인, commit을 반복하지 않습니다.

하나의 계획이 하나의 승인·완료 단위입니다. 작은 그룹이 여러 개면 그 계획의 source를 나누어 검토합니다.
독립된 계획이나 서로 다른 승인을 자동으로 합치지 않습니다. `compact-finish` 전에는 `compact-check`,
`health`, `conflicts list`, 별도의 reindex를 일반 절차로 반복 실행하지 않습니다.

source와 target hash, patch hash, probe에 묶인 검토 결과를 재사용합니다. 바뀌지 않은 의미 판단은 다시 서술하지 않습니다.
reviewer에게는 관련 source, target, patch, probe, 필요한 규칙만 전달합니다. 상위 대화 전체나 이미 끝난 batch의 계획과
검토를 전달하지 않습니다. 새 audit 문서 대신 기존 계획에 결함과 pass/fail만 남깁니다.
새 evidence, 반례, patch 변경의 영향을 받는 판단은 다시 검토하고, base drift나 승인 변경은 아래 gate를 따릅니다.
이것은 검토 결과의 재사용이며 이전 작업의 승인을 재사용할 권한이 아닙니다.

### 검토 checkpoint

기존 `review`에 `drafts`(변경된 target -> Vault 상대 초안 경로)와 `probes`를 추가합니다.
변경되지 않은 target은 `drafts`에서 생략합니다. 판단에 추가로 사용한 evidence와 정책은 `dependencies`에 경로로 기록합니다.
기본적으로 전체 source context, 기존 후보, 루트 CURATOR가 hash에 묶입니다.

```json
{
  "drafts": {"20_Projects/Project/current.md": ".tmp/curating/current.md"},
  "dependencies": ["10_User/WORKING_AGREEMENT.md"],
  "probes": [
    {"query": "The Project's current choice", "bundle": "current", "expect_paths": ["20_Projects/Project/current.md"], "answer_state": "supported"},
    {"query": "A decision that does not actually exist", "bundle": "current", "answer_state": "abstain"},
    {"query": "A decision attributed to the wrong person", "bundle": "current", "forbid_paths": ["20_Projects/Project/current.md"], "answer_state": "abstain"}
  ]
}
```

이것은 `review`에 추가할 필드의 예이며 `items/delete_paths/user_knowledge`를 대체하지 않습니다.
probe는 실제 source와 질문에 맞게 씁니다. `expect_paths`는 모두 검색되어야 하는 경로이고,
`forbid_paths`는 반환되면 안 되는 경로입니다. `bundle`과 기대 `answer_state`를 명시합니다(최대 20개).

```bash
# Before approval: leave tracked files untouched and check only the mechanical checks of the drafts and the IDs to re-review
uv run pvg-wiki --vault . compact-review .tmp/curating/plan.json
# Record only after the independent semantic review has actually finished
uv run pvg-wiki --vault . compact-review .tmp/curating/plan.json --record
```

`--record`만 로컬 계획의 `review_checkpoint`를 갱신합니다. LLM 검토를 수행하거나 승인을 생성하는 명령이 아닙니다.
CLI는 source/target의 변경 전후 hash, 판단, probe를 비교하며 바뀌지 않은 item을 재사용합니다.
같은 base/plan의 일부 변경이 이미 적용되었더라도, 새 초안은 Git base와 비교해 다시 검토할 수 있습니다.
raw를 복원하거나 새 초안을 먼저 적용할 필요는 없습니다. 계획 밖의 변경과 남은 raw의 내용 수정은 차단됩니다.
재검토 기록이 있어도 새 초안을 적용하려면 별도의 정확한 승인이 필요하며, 적용된 내용이 초안과 다르면 finish가 거부합니다.
독립된 target 초안 하나가 바뀌면 그 target을 쓰는 판단만 무효화됩니다. 공유 source context, 규칙, probe가
바뀌면 보수적으로 함께 다시 검토합니다. 파일 변경이 없는 새 사용자 해명이나 반례도
이유, dependency, probe에 반영해 검토가 무효화되도록 합니다. 관련 없는 의미 판단을 자동으로 다시 실행하는 model 호출은 없습니다.

출력의 `patch_sha256`은 실제 patch 파일이 아니라 변경된 경로와 변경 전후 Markdown hash를 묶은 값입니다.
`scope_sha256`은 이 묶음을 계획의 판단 내용과 묶으며, 실행 기록인 두 checkpoint만 제외합니다.
승인은 이 scope와 실제로 검토 가능한 diff에 대해 받습니다. checkpoint 저장만으로는 승인
범위가 바뀌지 않지만, source, target, base, 판단, probe, 계획이 바뀌면 새 승인이 필요합니다.

probe는 모든 핵심 의미를 다루어야 하지만, 같은 질문으로 여러 item을 검증할 수 있습니다.
귀속, 정정, 충돌, 시점, 적용 조건, 사용자 선호의 과잉 일반화는 해당 변경에서 검증하며,
존재하지 않는 사실과 엉뚱한 사람에 대한 오귀속이라는 두 negative control을 포함합니다.
profile 변경에서는 가설과 반례가 보존되고 행동 지시로 오인되지 않는지 검증합니다.
의미 검토는 source와 patch를 비교합니다. 적용 후에는 승인된 patch가 적용되었는지 기계적으로 확인합니다.
검색 probe는 별도의 책임입니다. 최종 index에서 검색 가능성과 answer_state를 검증합니다.

```bash
# After applying the approved exact patch, run with the same Vault/DB/Qdrant settings as the Gateway
uv run pvg-wiki --vault . compact-finish .tmp/curating/plan.json
```

승인된 source 삭제 목록은 `review.delete_paths`에, `updated|unchanged`와 그 이유는 `review.user_knowledge`에 둡니다.
선택되었지만 유지되는 source도 item 판단을 모두 마치며, 그 내용은 그대로 둡니다.
이 명령은 Git base 대비 coverage, 중복 ID, deferral 보존, target, 변경 범위, 문자 수 감소를 검사하고,
변경 전후 Wiki 분석을 한 번 거쳐 새 metadata/reference/cycle 오류와 충돌 변화를 보고합니다.
적용된 내용이 검토한 초안과 일치함을 확인한 뒤 기존 증분 색인을 호출하고, 삭제된 경로의 Qdrant point가
0개인지, 그리고 모든 검색 probe의 경로와 answer_state를 확인합니다. 각 검색은 `refresh=false`를 씁니다.
이미 최신인 index는 다시 색인하지 않으며, 같은 입력으로 완료된 재실행은 저장된 검색 결과도 재사용합니다.
`finish_checkpoint`는 계획 안에만 저장됩니다. 실패, quota, stale/fallback, 변경 감지 시에는 완료 기록을 쓰지 않습니다.
quota가 복구되면 같은 명령으로 재시도합니다. 새 DB 초기화, model 변경, collection 재생성은 이 명령의 범위 밖입니다.
명시적인 `EMBEDDING_PROVIDER=none`에서는 같은 필수 검색 probe가 대신 계획과 source 문서 hash에 묶인 현재 Markdown에 대해 실행되며, Qdrant나 embedding을 쓰지 않고 index metadata도 쓰지 않습니다. semantic 검색을 켠 경우에는 위의 모든 index 요건이 그대로 적용됩니다.
기존 Gateway DB와 호환되는 index를 써야 하며, CLI와 서버의 색인은 공통 lock으로 중복 실행을 거부합니다.
DB/index를 쓰는 다른 Gateway 설정이나 다른 Vault checkout에 임의로 연결하지 않습니다.

점검에 실패하면 exit code 1과 함께 `blocked`가 됩니다. 실행 시간은 `timings`에 첫 Vault 읽기, 기계적 점검, 색인/검색으로 나뉘어 표시됩니다.
이 명령은 승인이나 의미 보존을 자동으로 증명하지 않으며 `approval=not_checked`를 유지합니다.
또한 자동으로 적용, DELETE, commit, push하지 않습니다. 색인 없이 읽기 전용 진단이 필요할 때만 `compact-check`를 씁니다.
삭제할 source의 전송 checkpoint와 모든 Markdown 링크의 살아 있는 inbound 참조는 따로 확인합니다.

## 6. 승인과 삭제 gate

현재 대화에서 정확한 계획에 대해 승인을 받습니다. 전체 graph 대신 아래 요약과 검토 가능한 patch를 제시합니다.

```text
Base commit: <full commit id>
Source/target paths and SHA-256: <plan JSON>
Plan scope SHA-256 / target diff binding: <scope_sha256> / <patch_sha256>
Delete exactly: <paths and SHA-256>
Active Markdown characters: <before> -> <after> (<delta>, <ratio>)
Validation probes: <passed>/<total>
Requested external actions: <none|commit|push>
```

source/target hash, base, 경로, patch, 외부 행동이 바뀌면 승인은 무효입니다.
"continue"나 "clean it up"을 삭제, commit, push에 대한 새 승인으로 취급하지 않습니다.

폐기할 모든 raw/legacy source는 다음을 모두 충족해야 합니다.
- commit되어 있고, raw가 당일 날짜가 아니며, 파일 전체의 모든 item이 merge/replace/already-covered/discard로 판단되었습니다.
- hold가 하나라도 있는 파일은 유지하며, 그 지속적 의미는 승인된 target에서 source 근거와 함께 검증합니다.
- 활성 Codex·Claude client가 날짜별 payload checkpoint를 지원하고 해당 조각의 전송이 성공했습니다. 이를 확인할 수 없으면 재전송 위험 때문에 파일을 유지합니다.
- 삭제 경로와 hash가 정확히 일치하고, 살아 있는 inbound 참조가 없으며, active Markdown 문자 수가 줄어듭니다.
- 새 무결성 오류가 없고, 핵심 probe가 통과하며, 최종 Qdrant fingerprint가 Vault와 일치하고(`EMBEDDING_PROVIDER=none`이면 현재 Markdown과 그 hash), 삭제된 source는 검색되지 않습니다.

Cloudflare quota나 Qdrant 오류로 최종 검색을 검증할 수 없으면 파괴적 작업을 완료하거나 commit하지 않습니다.

참고: raw의 날짜는 hook이 실행된 PC의 현지 날짜입니다. 전날의 기록이 늦게 재전송되어 파일이 바뀔 수 있습니다.
적용 전에 전송 확인이 없거나 source hash가 계획과 다르면, 승인된 계획에서 그 파일만 조용히 제외하도록
수정하지 않습니다. 적용을 중단하고 계획을 다시 만들어 새 승인을 받습니다(제외에도 새 승인이 필요합니다). 삭제 직전의
정확한 hash 확인은 동시 쓰기에 대한 원자적 보장이 아니며, 여러 writer 사이의 분산 일관성은 주장하지 않습니다.

기존의 무관한 경고를 매번 해결하느라 범위를 넓히지 않되, 이번 변경에서 생기는 새 오류와 의미 손실은 차단합니다.
충돌 해결은 canonical의 상태와 본문, 그리고 정확한 contradicts edge에 반영하고, reopen 시 그 edge를 복원합니다.
새 receipt/conflict event를 만들지 않습니다.

## 7. 감사, 복구, 중단

target 변경과 source 삭제를 승인된 단일 부모 compaction commit 하나에 함께 담고 다음을 남깁니다.

```text
PVG-Curation-Base: <full parent commit>
PVG-Curation-Plan-SHA256: <scope_sha256>
PVG-Active-Characters: <before> -> <after>
PVG-Validation: <passed>/<total>
```

삭제된 raw는 `git show <base-commit>:<path>`로 복구합니다. 이는 active store에서의 퇴출이지 Git history에서
개인정보를 완전히 삭제하는 것이 아닙니다. main에 force-push하지 말고 전체 저장소 백업을 하나 유지합니다.
새 DB, graph 서비스, scheduler, audit store, tag, archive branch, Git note를 도입하지 않습니다.

dirty tracked Markdown, base/upstream drift, 승인 불일치, coverage·의미·검색 검증 실패가 있거나 secret/purge
또는 범위 밖의 변경이 필요하면 적용을 중단합니다. 승인 전에는 tracked 파일을 수정하지 않으며,
적용 후 실패가 생기면 commit이나 push를 하지 말고 승인된 범위 안에서만 복구합니다.
해석할 수 없는 개별 source는 보류할 수 있지만, 이미 승인된 patch에서 제거하거나 다른 batch로 옮기려면 새 승인이 필요합니다.
