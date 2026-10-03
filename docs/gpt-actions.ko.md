# Custom GPT Actions 설정

[English](gpt-actions.md) | **한국어**

PersonaVault를 Custom GPT에서 쓰려면 관리 페이지에서 `Read + Write` token을 하나
만들고 Actions 인증에만 넣습니다. 관리 UI는 `Read`, `Write`, `Read + Write`만 표시하며,
token 목록에는 내부 scope인 `vault-rag`, `conversation-log`, `agent-memo`가 보일 수 있습니다.
OpenAPI schema에는 token을 쓰지 않습니다.

현재 public API는 v3입니다. v1과 v2 route는 token을 먼저 인증한 뒤
`410 client_upgrade_required`를 반환합니다.

아래 OpenAPI schema와 GPT instructions는 Custom GPT에 그대로 붙여 넣는 영어 원문이며 번역하지 않습니다. 영어 문서
[gpt-actions.md](gpt-actions.md)의 같은 블록과 바이트 단위로 같아야 합니다.

## 인증

Custom GPT Actions에서 다음처럼 설정합니다.

```text
Authentication: API Key
Auth Type: Bearer
API Key: pvg_...
```

token은 `<gateway-url>/admin/tokens`에서 생성하거나 rotate합니다.

## 공개 노출 범위

GPT Actions에는 HTTPS로 접근 가능한 Gateway가 필요하며, 공개 대상에는 Actions가 쓰는 `/gateway/v3/...`
route만 열어 두는 것이 이상적입니다. `/admin`, `/docs`, `/redoc`, `/openapi.json`과 Qdrant는 공개 대상에서 제외하고,
관리 화면은 TLS와 접근 제어(VPN·IP allowlist 등)가 있는 private 경로로만 쓰세요. 특정 proxy 구성을
요구하지 않으며 endpoint와 인증 방식은 바뀌지 않습니다.

이 격리는 자동으로 보장되지 않습니다. 호스팅 서비스(제공된 Railway·Render 설정을 포함한 통합 브라우저 설정 배포)는
운영자가 플랫폼 접근 제어로 따로 제한하지 않는 한 `/setup`과 `/admin`을 같은 공개 HTTPS 서비스로 함께 노출합니다.
Qdrant는 비공개로 둡니다. Admin login에는 client 주소당 5분에 5회 제한이 있고 초과 시 `429`와 `Retry-After`를
반환하지만, 이 제한은 프로세스 메모리에서만 동작하므로 네트워크 접근 제어를 대체하지 못합니다.
[../SECURITY.ko.md](../SECURITY.ko.md)를 보세요.

## OpenAPI schema

`servers.url`만 실제 gateway 주소로 바꿉니다. 끝 `/`나 `/gateway/v3`를 붙이지 않습니다.
`components` 섹션은 넣지 않습니다.

```yaml
openapi: 3.1.0
info:
  title: PersonaVault Gateway
  version: 3.0.0
servers:
  - url: https://persona-vault.example.com
paths:
  /gateway/v3/capture:
    post:
      operationId: writePersonaVaultNote
      summary: Write a user-requested note as temporary raw evidence.
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              additionalProperties: false
              properties:
                kind:
                  type: string
                  enum:
                    - note
                title:
                  type: string
                  minLength: 1
                  maxLength: 120
                body:
                  type: string
                  minLength: 1
                  maxLength: 64000
                note_type:
                  type: string
                  enum:
                    - observation
                    - proposal
                    - handoff
                note_kind:
                  type: string
                  minLength: 1
                  maxLength: 80
                project:
                  type: string
                session_id:
                  type: string
                observed_at:
                  type: string
                subject_id:
                  type: string
                subject_aliases:
                  type: array
                  items:
                    type: string
                error_signatures:
                  type: array
                  items:
                    type: string
                tags:
                  type: array
                  items:
                    type: string
                outcome:
                  type: string
                  enum:
                    - success
                    - failure
                    - mixed
                    - unknown
                    - not_applicable
                applicability:
                  type: object
                  additionalProperties: true
                provenance:
                  type: object
                  additionalProperties: false
                  properties:
                    mode:
                      type: string
                      enum:
                        - direct_observation
                        - derived
                        - reported
                        - human_asserted
                    evidence_refs:
                      type: array
                      items:
                        type: string
                    method_refs:
                      type: array
                      items:
                        type: string
                    derived_from:
                      type: array
                      items:
                        type: string
                relations:
                  type: object
                  additionalProperties: false
                  properties:
                    supports:
                      type: array
                      items:
                        type: string
                    contradicts:
                      type: array
                      items:
                        type: string
                    supersedes:
                      type: array
                      items:
                        type: string
                source_refs:
                  type: array
                  items:
                    type: string
                source_hashes:
                  type: object
                  additionalProperties:
                    type: string
                repository_sources:
                  type: array
                  items:
                    type: object
                    additionalProperties: false
                    properties:
                      repo_id:
                        type: string
                      commit:
                        type: string
                      path:
                        type: string
                      anchor:
                        type: string
                    required:
                      - repo_id
                      - commit
                      - path
                privacy:
                  type: string
              required:
                - kind
                - title
                - body
                - note_type
                - note_kind
      responses:
        "200":
          description: Raw note capture result.
          content:
            application/json:
              schema:
                type: object
                additionalProperties: false
                properties:
                  status:
                    type: string
                  operation:
                    type: string
                  conversation_id:
                    type: string
                  note_id:
                    type: string
                  note_type:
                    type: string
                  path:
                    type: string
                required:
                  - status
                  - operation
                  - note_id
                  - note_type
                  - path
  /gateway/v3/search:
    post:
      operationId: searchPersonaVault
      summary: Search PersonaVault and return compact flat results.
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              additionalProperties: false
              properties:
                query:
                  type: string
                  minLength: 1
                  maxLength: 500
                limit:
                  type: integer
                  minimum: 1
                  maximum: 20
                  default: 5
                refresh:
                  type: boolean
                  default: false
                view:
                  type: string
                  enum:
                    - all
                    - current
                    - evidence
                    - history
                    - conflicts
                context:
                  type: object
                  additionalProperties: true
              required:
                - query
                - view
      responses:
        "200":
          description: Compact search context and one flat result list.
          content:
            application/json:
              schema:
                type: object
                additionalProperties: false
                properties:
                  query:
                    type: string
                  view:
                    type: string
                  embedding_model:
                    type: string
                  context:
                    type: string
                  index:
                    type: object
                    additionalProperties: true
                  results:
                    type: array
                    items:
                      type: object
                      additionalProperties: false
                      properties:
                        document_id:
                          type: string
                        path:
                          type: string
                        title:
                          type: string
                        snippet:
                          type: string
                        score:
                          type: number
                        match:
                          type: string
                        outcome:
                          type: string
                        review_state:
                          type: string
                        temporal_state:
                          type: string
                        conflict_state:
                          type: string
                        role:
                          type: string
                          enum:
                            - current
                            - evidence
                            - history
                      required:
                        - document_id
                        - path
                        - snippet
                        - role
                  answer_state:
                    type: object
                    additionalProperties: false
                    properties:
                      state:
                        type: string
                        enum:
                          - supported
                          - evidence
                          - abstain
                          - review_required
                      reason:
                        type: string
                    required:
                      - state
                      - reason
                required:
                  - query
                  - view
                  - context
                  - results
                  - answer_state
```

## GPT 지침 (Instructions)

```text
Use searchPersonaVault when the user asks about prior notes, personal memory,
project history, decisions, or anything that may already exist in PersonaVault.
Always send view: all for broad search, current for compacted current knowledge,
evidence for raw sources, history for chronology, or conflicts for unresolved
claims. Search already returns compact flat results; never send bundle or compact.

If answer_state=abstain, say the available evidence is insufficient or does not
match the requested environment. If answer_state=review_required, show the
competing claims without choosing a winner. Prefer refresh=false; ordinary search
does not start or rebuild the embedding index. If
index.search_mode=keyword, use the returned keyword evidence and do not request
refresh.

Use writePersonaVaultNote only when the user explicitly asks to save, remember,
record, log, or hand off something. Always send kind: note, a note_type, and a
specific note_kind. Use observation for a sourced fact, outcome, or confirmed
preference; proposal for a suggested decision, correction, rule, or procedure;
and handoff for temporary continuation context. A captured note is unreviewed raw
evidence in 30_Conversations/raw, not canonical knowledge.

Before writing, search the same subject and source. Preserve attribution,
negation, conflicts, chronology, applicability, and uncertainty. Do not save
progress logs, help or smoke output, temporary errors, copied repository
documentation, secrets, passwords, tokens, or private keys.

If an Action returns client_upgrade_required, tell the user to update the
PersonaVault Action schema and plugin, then retry the unchanged request. Do not
claim that a note was saved before the retry succeeds.
```

## 모든 endpoint를 연결하지 않는 이유

Custom GPT에는 `search`와 `capture`의 note 입력만 연결합니다. Conversation 입력은 신뢰된
plugin hook 전용이고, capabilities, working agreement, health와 admin endpoint는 GPT가 직접
호출할 필요가 없습니다. Helper 이름은 계속 `pvg-agent-memo`, `pvg-rag-search`를 사용합니다.
