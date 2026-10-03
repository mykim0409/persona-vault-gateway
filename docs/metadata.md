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
| Other documents in `10_User/` | Detailed user records maintained by a human and searched when needed |
| `20_Projects/`, `50_Knowledge/` | `canonical`, `primary`, approved by a human or the Curator |
| Invalid or uninterpretable metadata | `unknown`, `unreviewed`, time fields omitted, `evidence` |
| Missing provenance | `provenance_mode: reported`, `provenance_defaulted: true` |
| Missing or unsupported outcome | `outcome: unknown` |

Gateway raw and the legacy agent root do not gain canonical authority even if the frontmatter says `canonical`, `human_accepted`, `current`, or
`primary`. Canonical authority is recognized only when a human-maintained root and
the review policy both allow it. A public `view=all` search covers `primary`, `supporting`,
`evidence`, `history`, and `archive` together, and `retrieval_tier` is a search priority, not a read
permission.

An explicit `provenance_mode: reported` has `provenance_defaulted: false`. When a fallback is applied because provenance is missing, the normalized result and the user-facing display must distinguish it as `reported (default)`, and it is not promoted to direct observation or human review.

## Fields

| Field | Meaning |
| --- | --- |
| `pv_schema` | Version of the metadata contract. The current value is `1` |
| `id` | Stable identifier of the document |
| `memory_type` | Knowledge role, one of `transcript`, `episode`, `candidate`, `canonical`, `derived_view` |
| `kind` | Content type such as `debugging`, `procedure`, `decision`, `lesson`, `handoff` |
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
