# PersonaVault metadata

**English** | [한국어](metadata.ko.md)

`pv_schema: 1` is the official Markdown frontmatter format. When the Gateway writes a new document, it keeps the top-level keys flat and records objects and lists as one-line JSON-flow values.

`POST /gateway/v3/capture` in Gateway v3 distinguishes input with `kind=conversation|note`,
but stores both kinds as pre-review evidence under `30_Conversations/raw/`. A note uses
`note_type=observation|proposal|handoff` together with a free-form `note_kind`.
The API `kind` discriminator and the content-classification `kind` in the Markdown below are different fields.
The Gateway does not create new `40_Agents/` files.

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

Do not invent unknown values. In particular, if the time is unknown, do not write `observed_at`, `effective_from`, or `effective_to`, and do not substitute an empty string, the current time, or the file creation time.

## Compatible input

When reading existing documents, nested YAML with the same meaning is also accepted. The official writer always outputs the flat format above.

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

Nested `review.state`, `temporal.*`, and `provenance.*` are normalized to the flat `review_state`, `temporal_*`, and `provenance_*` respectively. JSON-flow values can also be read as block lists and objects. If values with the same meaning conflict, or the YAML cannot be interpreted safely, the document is treated as pre-review evidence without raising its authority.

## Authority rules

The path and the authenticated author take precedence over what the document declares.

| Input state | Conservative interpretation |
| --- | --- |
| Existing `40_Agents/<agent_id>/` | legacy `episode` or `candidate`, `unreviewed`, at most `supporting` |
| `30_Conversations/raw/` | `transcript` (a v3 conversation or note), `unreviewed`, `evidence` |
| `30_Conversations/summaries/` | `derived_view` with source IDs and hashes, at most `supporting` |
| `30_Conversations/important/` | `derived_view` for backward compatibility with earlier versions, at most `supporting` |
| `10_User/WORKING_AGREEMENT.md` | `canonical`, `primary`: global collaboration rules approved by a human |
| `10_User/` documents with `kind: user_profile` or `user_ledger` | `canonical`, `primary`: the user brief and observation ledger maintained by the approved Curator |
| Other documents in `10_User/` | Detailed user records maintained by a human and searched when needed |
| `20_Projects/`, `50_Knowledge/` | `canonical`, `primary`, approved by a human or the Curator. Project `BRIEF.md` (`kind: brief`) and `DECISIONS.md` (`kind: decision_ledger`) live here |
| Invalid or uninterpretable metadata | `unknown`, `unreviewed`, time fields omitted, `evidence` |
| Missing provenance | `provenance_mode: reported`, `provenance_defaulted: true` |
| Missing or unsupported outcome | `outcome: unknown` |

Gateway raw and the legacy agent root do not gain canonical authority even if the frontmatter says `canonical`, `human_accepted`, `current`, or
`primary`. Canonical authority is recognized only when a human-maintained root and
the review policy both allow it. A public `view=all` search covers `primary`, `supporting`,
`evidence`, `history`, and `archive` together, and `retrieval_tier` is a search priority, not a read
permission.

An explicit `provenance_mode: reported` has `provenance_defaulted: false`. When a fallback is applied because provenance is missing, the normalized result and the user-facing display must distinguish it as `reported (default)`, and it is not promoted to direct observation or human review.

## Search response safety

Search candidates are not automatically answer evidence. Response-only `answer_relevance`
distinguishes `candidate`, `semantic_candidate`, `grounded`, and `related_claim`.
Grounding uses query-term coverage, explicit identifiers, and conservative actor/action guards.
Coverage is evaluated per paragraph, list item, or Markdown table row, with enclosing ATX headings,
list-item introductions, and table headers retained as local context. Sibling list items stay
separate even without blank lines; wrapped lines stay with their item. Nested items inherit their
parent's introductory paragraph, not sibling content. This is a conservative block splitter, not
a complete CommonMark parser. Unrelated paragraphs, sibling sections, list items, and table
rows cannot pool their words into one anchor. Keyword/hybrid snippets come from the best
eligible block rather than the first matching part of the document. Provenance markers remain hidden.
Approval queries and claims that someone confirmed or verified something require one identifiable
actor linked to that action in the source.
Missing, ambiguous, or unsupported actor expressions retain candidates but return `abstain`;
an empty actor extraction is never treated as a match, even for actor-free approval queries.
Confirmation claims also require all requested objects and qualifiers in the same affirmative
clause. Words from separate confirmations, another person's report, or an unconfirmed completion
cannot be combined into proof. Negation, plans, uncertainty, and checking *whether* something is
complete do not establish completion. A topic lookup such as `NFS 복구 상태 확인해줘` remains a normal search.
These lexical guards can abstain on valid paraphrases or evidence spread across sentences or blocks.
It is not a general entailment checker: `supported` still requires reading the cited claim,
including its attribution, scope, and negation. A shared project name alone is not proof.
Semantic-only candidates remain available with `answer_state=evidence` and
`reason=semantic_candidates_require_verification`, not automatic `supported`.
Candidates that violate explicit question constraints do not become answer evidence.

An unresolved conflict on a grounded document still gates the answer. Other conflicts gate it
when linked by the same `subject_id` or explicit relations, not merely by a shared keyword.
This does not localize document-level conflict metadata to individual paragraphs.
Keep `subject_id` claim-specific, not project-wide. Safety runs before the response `limit`;
`index.results_truncated` warns when related evidence was omitted from the displayed results.
Neither this response field nor its computation changes stored Markdown or requires reindexing.

## Fields

| Field | Meaning |
| --- | --- |
| `pv_schema` | Version of the metadata contract. The current value is `1` |
| `id` | Stable identifier of the document |
| `memory_type` | Knowledge role, one of `transcript`, `episode`, `candidate`, `canonical`, `derived_view` |
| `kind` | Content type such as `debugging`, `procedure`, `decision`, `lesson`, `handoff`. The Curator also uses `brief` and `decision_ledger` (project status board and decision ledger) and `user_profile` and `user_ledger` (user brief and observation ledger) |
| `capture_kind` | `conversation` or `agent_note`, as recorded by the v3 raw writer |
| `note_type` | Lifecycle role of a note: `observation`, `proposal`, `handoff` |
| `note_kind` | More specific free-form content classification of a note. Preserved in the API input and the raw context |
| `review_state` | Review state such as `unreviewed`, `human_accepted`, `human_rejected`, `merged` |
| `temporal_state` | `point_observation`, `proposed_current`, `current`, `historical`, `superseded`, `unknown` |
| `outcome` | `success`, `failure`, `mixed`, `unknown`, `not_applicable` |
| `observed_at` | Time the event was observed |
| `session_id`, `segment_date` | Identifier that links per-date transcripts to the same agent session, and the local date |
| `effective_from`, `effective_to` | Period during which a claim or decision is valid |
| `projects`, `topics` | Search scope and flexible topic markers |
| `subject_id` | Stable subject identifier that groups the same claim |
| `applicability` | Applicability conditions such as repository, revision, OS, and tool version |
| `provenance_mode` | Nature of the source: one of `direct_observation`, `derived`, `reported`, `human_asserted` |
| `provenance_defaulted` | Normalization result indicating whether `reported` was applied because provenance was missing |
| `evidence_refs`, `method_refs`, `derived_from` | IDs of the observation evidence, the methods consulted, and the derived-from originals |
| `relations` | Document relations such as `supports`, `contradicts`, `supersedes` |
| `conflict_state` | `none`, `unresolved`, `resolved` |
| `retrieval_tier` | `primary`, `supporting`, `evidence`, `history`, `archive` |
| `privacy` | Content-classification marker. It does not provide access control by itself |

## P1 search fields

| Field | Rule |
| --- | --- |
| `subject_aliases` | Language and spelling variants of the same subject. Does not create a separate canonical document |
| `error_signatures` | Short, reusable error shapes. Do not include full logs, temporary IDs, tokens, or personal hosts/paths |
| `source_refs` | List of source IDs used for a derived view or claim |
| `source_hashes` | Object that maps source IDs to content hashes. Do not create an entry for an unknown hash |
| `repository_sources` | `repo_id`, `path`, `commit`, and an optional `anchor` for facts whose origin is a repository |

`source_hashes` is used to judge whether a derived view is stale because a source changed, and it does not raise authority. Do not put remote credentials or local absolute paths in `repository_sources`.

## Curator documents and provenance markers

The Curator keeps four fixed-shape documents. They are found by `kind`, not by file name, and the `kind` must sit under the directory shown. Headings and table headers are Korean; [CURATOR.md](CURATOR.md) explains them.

| Path | `kind` | Shape |
| --- | --- | --- |
| `20_Projects/<Project>/BRIEF.md` | `brief` | Status board with fixed `##` headings. Rewritten every curation. Body at most 8,000 characters, markers not counted |
| `20_Projects/<Project>/DECISIONS.md` | `decision_ledger` | One table of rows `D-###`. Rows are appended and never deleted |
| `10_User/PROFILE.md` | `user_profile` | User brief with fixed `##` headings. Rewritten every curation. Same cap as `BRIEF.md` |
| `10_User/OBSERVATIONS.md` | `user_ledger` | One table of rows `U-###`. Rows are appended and never deleted |

A claim written from raw ends with a provenance marker, an HTML comment such as `<!-- pvg-src: item:<16 hex> -->`. One marker can hold several space-separated tokens:

- `item:<16 hex>` is a one-way hash of a raw item's path and locator. It holds no path or quote.
- `sess:<8 hex>` is a one-way hash of a session id. Only user-ledger rows carry it.
- `doc:<path>#<heading>` points at an existing document. It is typed by hand, and only in the one-time migration from topic documents.

The marker sits at the end of the claim line, inside the last cell of a table row. It is hidden in the Obsidian reading view. It is part of the Markdown body, so it counts in the active characters and is indexed. The Curator tools write `item:` and `sess:` tokens; do not edit them by hand. Only `doc:` tokens are typed by hand.
