# Custom GPT Actions setup

**English** | [한국어](gpt-actions.ko.md)

To use PersonaVault from a Custom GPT, create one `Read + Write` token on the admin page and put it
only in the Actions authentication. The admin UI shows only `Read`, `Write`, and `Read + Write`,
while the token list may show the internal scopes `vault-rag`, `conversation-log`, and `agent-memo`.
Do not put the token in the OpenAPI schema.

The current public API is v3. The v1 and v2 routes authenticate the token first and then
return `410 client_upgrade_required`.

## Authentication

Configure Custom GPT Actions as follows.

```text
Authentication: API Key
Auth Type: Bearer
API Key: pvg_...
```

Create or rotate the token at `<gateway-url>/admin/tokens`.

## Public exposure

GPT Actions needs a Gateway reachable over HTTPS, and ideally only the `/gateway/v3/...` routes that Actions uses
should be open to the public. Keep `/admin`, `/docs`, `/redoc`, `/openapi.json`, and Qdrant off the public side,
and use the admin screen only over a private path with TLS and access control (VPN, IP allowlist, etc.). No specific
proxy configuration is required, and the endpoints and authentication method do not change.

This isolation is not automatic. The hosted service (the unified browser setup deployment, including the provided Railway and
Render configurations) exposes `/setup` and `/admin` from the same public HTTPS service unless the operator
restricts them separately with platform access control. Qdrant stays private. Admin login is rate limited per client address
(5 attempts per 5 minutes, then `429` with `Retry-After`), but that limit is held in process memory only
and does not replace network access control. See [../SECURITY.md](../SECURITY.md).

## OpenAPI schema

Change only `servers.url` to the actual gateway address. Do not append a trailing `/` or `/gateway/v3`.
Do not add a `components` section.

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
      summary: Search PersonaVault with multiple relevant passages grouped by document.
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
          description: Bounded search context and document results containing relevant passages.
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
                        passages:
                          type: array
                          items:
                            type: object
                            additionalProperties: false
                            properties:
                              block_index:
                                type: integer
                                description: Zero-based evidence block in the current parsed document.
                              chunk_index:
                                type: integer
                                description: Zero-based semantic chunk when block_index is absent.
                              section:
                                type: string
                              snippet:
                                type: string
                              truncated:
                                type: boolean
                              answer_relevance:
                                type: string
                            required:
                              - snippet
                        passages_omitted:
                          type: integer
                        answer_relevance:
                          type: string
                        answer_conflict_state:
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

## GPT instructions

```text
Use searchPersonaVault when the user asks about prior notes, personal memory,
project history, decisions, or anything that may already exist in PersonaVault.
Always send view: all for broad search, current for compacted current knowledge,
evidence for raw sources, history for chronology, or conflicts for unresolved
claims. Search returns multiple passages grouped by document; never send bundle or compact.
Read each passage's qualifiers and answer_relevance. If excerpts or results were omitted,
narrow the query before claiming complete coverage. The snippet field is only the first passage.

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

## Why not every endpoint?

Connect only `search` and the `capture` note input to the Custom GPT. Conversation input is for the trusted
plugin hook only, and the GPT does not need to call the capabilities, working agreement, health, or admin endpoints
directly. The helper names remain `pvg-agent-memo` and `pvg-rag-search`.
