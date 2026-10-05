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
protocol_version: 23
---

# PersonaVault Curator Protocol

The goal is to consolidate raw records into knowledge, to reduce active Markdown, and to keep each project's situation readable at a glance in `BRIEF.md`.
Audit documents, plan descriptions, and the number of sessions processed are not results. Place this file in the Vault root as `CURATOR.md`.

```text
active_markdown_characters_after < active_markdown_characters_before
and every retired source item has a final disposition
and all critical validation probes pass
```

The character count is the total Markdown body size within the search scope, excluding `.git/`, `.obsidian/`, and `.tmp/`.
Preserving the full context does not mean repeatedly preserving the original wording. It means preserving attribution, evidence, negation, exceptions, retractions, conflicts, timing, and conditions of applicability.

Operational results are measured by the amount of raw actually removed relative to the inflow and by the net decrease in active Markdown, not by the number of batches processed.
In the final report, briefly state the applied reduction and the actually measured time for reading, consolidation, review, mechanical checks, and indexing.
Keep user approval wait time separate from processing time. Compare token usage only when it can be measured,
and do not convert account usage percentages or CLI run time into total curation tokens or time.
If curation speed cannot keep up with inflow, first adjust repeated reading, plan output, and the approval unit.
Do not skip the semantic preservation, approval, or deletion gates because of delay, and do not mark a large batch complete without reading it.

## 1. Boundaries and lifecycle

- A Curator task starts with the Vault that contains this file as its working root. A session that starts from another root and only edits Vault paths is not a Curator.
- The `id` above and the file name are fixed. When a trusted plugin detects this ID, it skips SessionStart injection and automatic conversation capture.
- Markdown and Git are the source of truth. Qdrant and the SQLite `rag_index_meta` are regenerable search projections.
- Do not modify tracked files before approval. Keep plans and drafts only in `.tmp/curating/`, which is excluded from Git.
- Do not directly read or modify the inside of `.git/` or `.obsidian/`. Use Git commands for recovery and status checks.
- Do not copy secrets into verbatim quotes or summaries. Privacy purges and history rewrites require separate approval.
- The Curator chooses the next target even if the user does not specify a subject.

| Path | Role |
| --- | --- |
| `30_Conversations/raw/` | Whole-file retirement target, by date. Do not edit lines, abbreviate, or move. |
| `40_Agents/` | Existing evidence. Do not create new files; consolidate under the same coverage and approval gates. |
| `10_User/` | Explicitly approved collaboration rules (`WORKING_AGREEMENT.md`), the user brief `PROFILE.md` (kind `user_profile`) and the observation ledger `OBSERVATIONS.md` (kind `user_ledger`). Other documents here are human-maintained records; they are not Curator targets unless the user asks. |
| `20_Projects/<Project>/BRIEF.md` | Status board, rewritten every curation. Body at most 8,000 characters (`--brief-cap`), markers not counted. |
| `20_Projects/<Project>/DECISIONS.md` | Decision ledger: one table, stable ids `D-###`, append-mostly. |
| `20_Projects/<Project>/<topic>.md` | Second priority. Touch only for reusable procedure or knowledge. |
| `50_Knowledge/` | Knowledge that can be reused across projects. |
| `30_Conversations/summaries/` | Use only when the order of the conversation and the rejected alternatives themselves are needed. |
| `90_Private/` | Modify only within the scope approved by the user. |

Update `DECISIONS.md` and rewrite `BRIEF.md` first; touch topic documents only for reusable procedure or knowledge. Create a new topic document only when no suitable document exists and it has independent search value.
When the plan lists a target with `create: true` (or `adopt: true`, an existing file of another kind), start the draft from the template path in the plan (`docs/templates/<NAME>.md` of the PersonaVault checkout; do not link it). Keep its frontmatter and headings exactly, replace only `<Project>` and `<project-slug>`, and carry the valid content of an adopted file into the template headings.
Do not automatically generate monthly or per-session summaries, index stubs, personality scores, or curation explanation documents.
Do not recreate or maintain past receipts in `60_Curation/` as an active lifecycle.
Do not widen the scope of edits or deletions to tidy existing documents unrelated to this task.

## 2. Select once and read only what is needed

1. After `git pull --ff-only`, fix the full HEAD as the base. Stop if there is dirty tracked Markdown.
2. Create a new plan with the command below. The default output is a summary; the full graph, item IDs, and hashes are kept in the JSON.
3. Read the selected sources and the related existing targets. Do not re-read or re-explain the same content or hash within the task.
4. Bind the source and target hashes, the base, the review scope, and the probes to the plan. If new evidence, a counterexample, or a change to a related file appears, re-review the affected judgments.

```bash
uv run pvg-wiki --vault . compact-plan --output .tmp/curating/plan.json
```

The preview lists `selected.ledgers` (path, `create`/`adopt`, current characters or rows, and for ledgers the `next_id` to use), `selected.create_targets` and `selected.subagent_items`, the count of raw items whose `event_role` is `subagent`. Each `raw_item` node carries its `event_role`.
`--brief-cap N` (default 8000) is accepted by `compact-plan`, `compact-review`, `compact-check` and `compact-finish`.

`--output` only creates a new JSON file under the Vault's `.tmp/curating/` and does not overwrite an existing plan.
Use a different file name for the next run. If the option is omitted, no file is created.
`--full` is for diagnostics that need the full output. Do not paste the full JSON into the conversation every time.
If the production Qdrant is only on the internal network, run this from the Gateway container. It does not modify the DB, Qdrant, or tracked Markdown.

### Queue and read budget

- The effective date follows this order: dated raw path -> `observed_at` -> `created_at` -> dated source path -> the Git first-added date of a root raw. If the date cannot be determined, exclude the source.
- The date is for sorting and for excluding same-day raw; it is not provenance or a basis for approval. The plan preserves `date_basis`.
- Among committed sources, exclude same-day and future raw and valid deferrals. The oldest component is the starting point.
- If the starting dates are the same, choose the component with the larger source character count on that date, then by stable ID.
- Within a project, group by subject, then error signature, then topic; if none exist, use the project; if there is no project either, treat the source as independent.
- Read the selected component in oldest-first order across dates, up to 8 files and 60,000 characters by default. Adjust with `--max-sources` and `--max-characters`. For other components, apply the oldest-first order again at the next selection.
- If the first file alone is larger than the budget, flag it as `oversized_source=true` and select the whole file. Do not automatically truncate it, partially delete it, or skip it. Even if the reading is split, do not retire the file before all of its items have been reviewed.
- The budget is based on source body size and is not a token limit. If the target and reviewer context are large, reduce the budget for the next plan.

The graph is a working tool that links the locations of sources, items, and existing documents. Documents of the same project are found as
modification candidates even without Qdrant. A current Qdrant only supplements candidates using existing point IDs and does not create new embeddings.
When it is stale or errors, use structural candidates. The same component or a high score does not mean the same meaning, truth, winner, or permission to delete.

### Individual deferral

Do not repeatedly audit the whole queue because of one uninterpretable file. Keep only that file and process the next candidate.
Put the list below in the existing `.tmp/curating/deferred.json` and pass it with `--deferrals .tmp/curating/deferred.json`.

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

If a source or dependency changes or disappears, the CLI invalidates the deferral. The Curator removes `invalidated_deferrals` from
the list and re-judges them. The Curator confirms a user clarification that causes no hash change.
Do not re-analyze a deferral whose conditions are unchanged every time. If the list is not given, the files become candidates again.
If everything is deferred, report only the conditions and finish. Do not work around dirty/base/approval problems with individual deferrals.

## 3. Interpret once and group the judgments

Read by linking the user request -> agent action -> user reaction or correction -> result. Distinguish the attribution of the original user, agent delegation,
subagent results, and supplied material. Do not turn a whole session into a single conclusion.

| Disposition | Meaning |
| --- | --- |
| `decision`, `deferral`, `supersession`, `goal_change`, `question` | A project decision, a deferral, a decision replaced by another, a changed goal, or an open question. Targets: `DECISIONS.md` and/or `BRIEF.md`. |
| `knowledge` | Reusable across projects. Target: `50_Knowledge/` or a topic document. |
| `user_preference`, `user_constraint`, `user_feedback`, `user_context`, `user_retraction` | Evidence about the person. Targets: `OBSERVATIONS.md` and the `PROFILE.md` rewrite. |
| `already-covered` | Current knowledge already preserves the same meaning and conditions sufficiently. |
| `discard` | No lasting value, such as progress narration, repetition, temporary errors, help/smoke output, or plain copies. |
| `hold` | Attribution, meaning, or scope of application cannot be judged safely. |

`merge` and `replace` are still accepted in protocol 23 with a warning and are removed in 24; they cannot target `BRIEF.md`, `DECISIONS.md`, `PROFILE.md` or `OBSERVATIONS.md`.

Items with the same judgment, target, and evidence can be recorded as one entry in `review.items`.
Use either a single `id` or a list of `ids`. Each ID must be included exactly once across the whole review.
The typed dispositions (everything except `already-covered`, `discard`, `hold`, and the deprecated `merge`/`replace`) take one `id` each; do not group them.
Do not fill coverage with a default judgment, a wildcard, or "discard the rest". Do not copy the gist repeatedly into the inventory, summary, or report.
Use the IDs, hashes, and character counts produced by the tool; the agent must not regenerate them or list them by hand.
Select the ID list for an identical judgment programmatically from the plan as well, but decide which items to group by reviewing the source.

```json
{"ids":["item:...","item:..."],"disposition":"already-covered","targets":["20_Projects/Project/current.md"],"reason":"The same conclusion under the same conditions is preserved in that document"}
```

A typed item (every disposition above except `already-covered`, `discard` and `hold`) adds these fields:

- `quote`: a short exact span of the raw item, 4 to 200 characters. The tool checks it as a whitespace-normalised substring of that item.
- `entry`: for a ledger row, the proposed row, free-form, such as `{"id":"D-014","type":"결정","status":"유효"}`. Take the id from `selected.ledgers[].next_id`; never invent a placeholder such as `D-new`. The tool does not read `entry`, but changing it invalidates the review.
- `anchors`: target path -> a quote, or a list of quotes, taken from the DRAFT line where the claim landed. Anchor a ledger row by its first two cells, such as `| D-014 | 2026-10-05 |`, because the bare id also appears in the 후속 cell of other rows. One item can mark several rows.
- `reason`.

`compact-annotate` (section 5) writes the `<!-- pvg-src: item:... -->` marker at the end of each anchored line, inside the last cell of a table row. It holds only item IDs, which are one-way hashes of path and locator.
Never type or edit a marker or an ID by hand. The one exception is the `doc:` source of the migration in section 8.

```jsonl
{"id":"item:...","disposition":"decision","targets":["20_Projects/Project/DECISIONS.md","20_Projects/Project/BRIEF.md"],"quote":"retries stop after 3 attempts","entry":{"id":"D-014","type":"결정","status":"유효"},"anchors":{"20_Projects/Project/DECISIONS.md":"| D-014 | 2026-10-05 |","20_Projects/Project/BRIEF.md":"| D-014 | 2026-10-05 |"},"reason":"The user fixed the retry limit"}
{"id":"item:...","disposition":"user_preference","targets":["10_User/OBSERVATIONS.md","10_User/PROFILE.md"],"quote":"keep answers short","entry":{"id":"U-007","type":"선호(명시)","status":"확인"},"anchors":{"10_User/OBSERVATIONS.md":"| U-007 | 2026-10-05 |","10_User/PROFILE.md":"U-007 Keeps answers short"},"reason":"The user stated this preference for all projects"}
```

A raw item whose `event_role` is `subagent` may only be `knowledge`, `already-covered`, `discard` or `hold`. A subagent's words are written by the agent, never a user decision.

`already-covered` and the typed dispositions specify an actual target. Do not blur items with different judgments, or with different negations, retractions, or conditions,
into one reason. A plain copy of supplied material can be discarded, but decisions made using that material and new results are judged separately.
Preserve important numbers, dates, and versions; reusable successes, failures, and causes; user constraints; results that change the next action; and unresolved questions.

- Accept a user instruction only from a user utterance in the original main conversation. A subagent delegation message is an instruction written by the agent.
- A document or quotation pasted by the user is supplied material, not the user's preference, claim, or identity.
- The user is the authority on their own preferences and constraints, but technical facts and external state are assessed using evidence.
- Consolidate a conflict if the claimants, evidence, time, conditions, and unresolved state can be preserved. Do not defer raw permanently only because a conflict is unresolved.
- For an unresolved conflict, leave `conflict_state: unresolved` in the canonical along with both claims and the conditions for confirming them. Search must return `review_required`, and do not change it to resolved because of compaction.

## 4. Rewrite existing project knowledge too

`human_accepted` means the current version was approved. It does not mean the content is accurate forever or that the file is immutable.
Do not stop at appending raw after an existing project document; rewrite the current description to fit the new evidence.

- Merge repeated sentences about the same fact, fix wrong descriptions, and remove instructions that are no longer current from the current description.
- Do not delete a valid constraint, exception, counterexample, or unresolved item that exists only in the existing target because it is absent from the new source.
- If a past state is needed for later judgments, briefly preserve its time and scope of application. There is no need to create a new history file or change log for every change. The diff of the original stays in Git.
- Do not decide truth just because something is newer or because it is written in the existing canonical. If opposing evidence of the same scope remains, preserve it as a conflict.
- Review the existing meaning of the target together with the new evidence, and get approval for an exact patch that includes sentence merging, replacement, and removal. Do not reuse the existing approval state as approval of the new patch.
- `delete_paths` in this CLI is for raw/legacy sources. Deleting or renaming a canonical file itself is not automatically allowed.
- `DECISIONS.md` and `OBSERVATIONS.md` are never rewritten. Rows are appended. Only 상태 and 후속 (decision ledger) or 상태 and 독립 세션 (observation ledger) change. 날짜, 유형, 내용 and 이유/근거 are frozen; a human fixes typos in Obsidian between curations.
- `BRIEF.md` and `PROFILE.md` are fully rewritten from the current ledger each curation. The tool blocks them above the body cap.

Example: if new evidence corrects an existing "always use A" to "B is needed under condition X",
do not leave the existing sentence immutable and accumulate annotations; rewrite it as "A under general conditions, B under condition X".
Keep valid exceptions for other situations and the remaining uncertainty.

### Ledgers and briefs

The templates fix the ledger headers and the `##` headings; do not rename or reorder them. Extra `##` headings are allowed.
`BRIEF.md` has `현재 목표`, `유효한 결정`, `미뤄진 것`, `대체된 것`, `열린 질문`, `최근 변화`. `PROFILE.md` has `역할·맥락`, `확인된 선호`, `가설`, `제약`, `에이전트에 준 피드백`, `반례·철회`, `최근 변화`.

- A new ledger row takes the id from `selected.ledgers[].next_id`. Ids only grow and are never reused.
- `DECISIONS.md` has 7 columns: id, 날짜, 유형, 상태, 내용, 이유·근거, 후속. 유형: `결정`, `미룸`, `대체`, `목표변경`, `질문`. 상태: `유효`, `대체됨`, `보류`, `종결`.
  후속 is `-` or the id of the row that replaced this one, and is required when 상태 is `대체됨`.
- `OBSERVATIONS.md` has 7 columns: id, 날짜, 유형, 상태, 내용, 근거, 독립 세션. The last cell is written by the tool.
- 날짜 is `YYYY-MM-DD`, or `-` when unknown.
- `유효한 결정` lists only rows whose 상태 is `유효`, and `확인된 선호` only rows whose 상태 is `확인`. Start each such row or bullet with its ledger id.

### Writing style

When consolidating as above, use by default a writing style inspired by STE (Simplified Technical English). Compliance with ASD-STE100 is not required,
and do not add separate steps, checklists, LLMs, tools, or schemas. Finish within the same patch and the existing semantic review and approval.

- Group duplicated statements in one place, and remove progress narration with no lasting value.
- Write one claim or one action per sentence. State the actor and the condition.
- Use one consistent term. If it helps search, give the original term or abbreviation alongside it only the first time.
- Do not reduce source attribution, negation, uncertainty, exceptions, date and scope of application, or strength of evidence. Do not turn an observation into a fact or policy,
  a merely reviewed option into an adopted one, or a context-limited preference into a universal tendency.
- Do not force English, an approved English dictionary, or an English word-count limit onto Korean documents.
- Do not rewrite raw in place. Do not bulk-revise existing documents for style alone.

Choose a table or Mermaid only when comparison, branching, dependency, state, or order is easier to read than prose. It is not needed in every document.
Use it only to replace a long explanation, and do not duplicate the same content in both text and a diagram. Keep the key relations and conditions in short sentences
for search, accessibility, and renderers that do not support Mermaid. Do not invent nodes, edges, or causality that the evidence does not support.
A diagram gets the same approval in the same patch as the text. Do not add separate approval, a diagram service, a graph DB, or an automatic generation pipeline.
Do not promise token savings or renderer portability. Preserve meaning, and the existing net-active-Markdown reduction gate still applies.

Example: "Deployment runs after approval. The approver is the user. Before approval, only drafts are created."

### Understanding the user

In the same reading pass, compare the relevant `10_User/` and project rules with the new evidence. Do not rebuild the whole user
profile every time or require a change. If there is no new meaning, leave only `unchanged` and the reason in `review.user_knowledge`.

- Look not only at complaints but also at the alternative chosen, explicit satisfaction, and approval. Do not infer emotion or personality from strong language; look for conditions that prevent recurrence.
- Distinguish direct statements, conditional observations, and inferred hypotheses. Silence, "go ahead", and an agent's declaration of success are not approval of a lasting preference.
- Evidence goes to `OBSERVATIONS.md` as rows `U-###`. 유형: `선호(명시)`, `선호(추론)`, `제약`, `피드백`, `맥락`, `철회`. 상태: `확인`, `가설`, `철회됨`.
  Narrow, correct, or retract by adding a row and changing the earlier row's 상태. Briefly record the conditions and the gist of representative evidence and counterexamples.
- The three-session threshold is computed. The tool writes 독립 세션: the number of distinct session ids behind the row. The count accumulates across curations through the `sess:` tokens of the marker, and different agents in one session count once. An agent memo (`pvg_memo`) names no session, because its stored session id is a fresh note id; it adds none.
  A `선호(추론)` row may be `확인` only with at least 3 independent sessions. Every other 유형 may be `확인` with one source. Otherwise write `가설`; the tool blocks an unsupported `확인` and never rewrites 상태 for you.
  Resending, quotation, or agent repetition is not independent evidence, and `확인` is not approval of a global rule.
- Content the user directed an agent to write about themselves is explicit, human_asserted material: 유형 `맥락`, never `가설`. A document or quotation the user pasted or supplied is not the user's claim; that judgment stays with the semantic review.
- An item from a subagent event can never support a user row.
- `PROFILE.md` is a description to be searched, not an instruction for behavior. Rewrite it from the ledger each curation: `확인된 선호` lists the `확인` rows, `가설` the hypotheses, `반례·철회` the counterexamples and retractions.
  In `역할·맥락`, link detailed existing `10_User/` documents by relative path. Those documents are not converted and are not targets.
- Put only explicitly approved global rules in `10_User/WORKING_AGREEMENT.md`, at 8,000 characters or fewer. It keeps its separate explicit approval. Project rules go in the relevant project.
- Keep profile edits and agreement changes distinct even within the same plan. Approval of a profile does not turn a hypothesis into fact or approve a global instruction.
- Apply the same retirement gate to user knowledge after meaning is preserved. Do not accumulate repeated raw text or live references to deleted sources.

## 5. Finish in one approval unit

1. **Prepare:** Fix the base, sources, and targets and read them once. The existing plan JSON and the canonical patch are the working artifacts; do not create duplicate planning documents.
2. **Consolidate:** Create the item judgments and the minimal patch together. Before adjusting to the result, define probes that verify critical meaning, attribution, corrections, exceptions, and conflicts.
3. **Review and approve:** A read-only context that is not the author independently reviews the source evidence and the before/after patch of the target. Do not re-review only the summary. After review, record the hashes with `compact-review --record` and obtain exact approval.
4. **Apply and finish check:** After confirming exact approval and applying, run `compact-finish` once. If a failure changes the patch, run the check again after the needed re-review and re-approval.
5. **Commit:** If the final checks and search pass, create the approved commit. Do not repeat checks, indexing, or commits for every item or file.

One plan is one approval and completion unit. Multiple small groups are handled by splitting and reviewing that plan's sources.
Do not automatically merge independent plans or different approvals. Before `compact-finish`, do not run `compact-check`,
`health`, `conflicts list`, or a separate reindex repeatedly as a normal procedure.

Reuse review results bound to the source and target hashes, the patch hash, and the probes. Do not restate semantic judgments that have not changed.
Pass the reviewer only the relevant sources, targets, patch, probes, and the needed rules. Do not pass the whole parent conversation or the plans
and reviews of batches already finished. Instead of a new audit document, leave only defects and pass/fail in the existing plan.
Re-review judgments affected by new evidence, counterexamples, or patch changes; follow the gates below for base drift or approval changes.
This is reuse of review results, not authority to reuse the approval of earlier work.

### Review checkpoint

Add `drafts` (changed target -> Vault-relative draft path) and `probes` to the existing `review`.
Omit unchanged targets from `drafts`. Record the evidence and policy additionally used for the judgment as paths in `dependencies`.
By default, the full source context, existing candidates, and the root CURATOR are bound to the hash.

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

This is an example of fields to add to `review` and does not replace `items/delete_paths/user_knowledge`.
Write probes to fit the actual source and questions. `expect_paths` are paths that must all be retrieved, and
`forbid_paths` are paths that must not be returned. State the `bundle` and the expected `answer_state` explicitly (at most 20).

```bash
# Write pvg-src markers for the anchored lines into the drafts only; re-run after any draft or anchor edit
uv run pvg-wiki --vault . compact-annotate .tmp/curating/plan.json
# Before approval: leave tracked files untouched and check only the mechanical checks of the drafts and the IDs to re-review
# (this and compact-check/compact-finish also take --brief-cap N, default 8000)
uv run pvg-wiki --vault . compact-review .tmp/curating/plan.json
# Record only after the independent semantic review has actually finished
uv run pvg-wiki --vault . compact-review .tmp/curating/plan.json --record
```

`compact-annotate` edits only the drafts in `review.drafts`, never their frontmatter, and running it again leaves the bytes unchanged.
It puts each marker inside the last cell of a table row. For `OBSERVATIONS.md` it also adds one `sess:` token per item (a hash of the session id of the item's source; a source without a session id, or an agent memo, adds none) and rewrites the 독립 세션 cell to the number of `sess:` tokens in the marker.
It blocks without changing any draft if a quote matches no line or several lines, matches a heading, names an ID that is not in the plan, or belongs to an item without a marked disposition (a typed one, `merge` or `replace`).

`compact-review`, `compact-check` and `compact-finish` run these mechanical checks. Each failure is an error unless marked as a warning.

- `quote` is a span of its raw item, and a subagent item has only `knowledge`, `already-covered`, `discard` or `hold`.
- Claim coverage: the item's ID is in a marker of each of its targets. A missing marker is an error for `BRIEF.md`, `DECISIONS.md`, `PROFILE.md` and `OBSERVATIONS.md`. For other targets it counts in `provenance.unmarked_items` (not an error yet); it will become an error after `compact-annotate` has been used on one real batch.
- A new marker that no review of that target backs is an error.
- Ledgers: unique ids; 유형 and 상태 from the vocabularies; `대체됨` has an existing 후속; no row deleted; the frozen columns of an existing row unchanged; marker tokens only grow; new ids above the base maximum; every new row has an `item:` or `doc:` marker; a user row's 독립 세션 equals its `sess:` count; a `선호(추론)` row that is new as `확인` or changes to `확인` needs 3 sessions.
- `BRIEF.md` and `PROFILE.md`: the fixed headings once each and in order; the body within the cap, markers not counted; every id under `유효한 결정` or `확인된 선호` is a row of the sibling ledger (`DECISIONS.md` in the same folder, `OBSERVATIONS.md` in `10_User/`) with the matching 상태, also when only the ledger changed. Warning: an id under `현재 목표` or `열린 질문` that is unknown or not `유효`.

The summary reports `curation.brief_chars`, `curation.ledger_rows` and `curation.superseded_rows` per path and lists the `warnings`.
A marker shows only that the reviewed plan says the item landed on that line, so it does not replace the semantic review.
Markers are Markdown body text. They count toward the active characters and are indexed, so keep a marker with its claim when you rewrite the line. Only the `BRIEF.md` and `PROFILE.md` cap excludes them.

Only `--record` updates the `review_checkpoint` of the local plan. It is not a command that performs an LLM review or generates approval.
The CLI compares the source/target before/after hashes, judgments, and probes, and reuses items that have not changed.
Even if some changes of the same base/plan have already been applied, a new draft can be re-reviewed by comparing it with the Git base.
There is no need to restore raw or to apply the new draft first. Changes outside the plan and content edits to the remaining raw are blocked.
Even with a re-review record, applying a new draft needs separate exact approval, and finish rejects it if the applied content differs from the draft.
If one independent target draft changes, only the judgments that use that target are invalidated. If the shared source context, rules, or probes
change, re-review them together conservatively. A new user clarification or counterexample without a file change is also
reflected in the reason, dependencies, and probes so that the review is invalidated. There are no model calls that automatically re-run unrelated semantic judgments.

`patch_sha256` in the output is not a literal patch file; it is a binding of the changed paths and the before/after Markdown hashes.
`scope_sha256` binds this binding with the decision content of the plan, excluding only the two checkpoints, which are execution records.
Approval is obtained for this scope and the actual reviewable diff. Saving a checkpoint alone does not change the approval
scope, but a change to the source, target, base, judgments, probes, or plan requires new approval.

Probes must cover all critical meanings, but the same question can verify several items.
Attribution, corrections, conflicts, time, conditions of applicability, and over-generalization of user preferences are verified in the relevant change,
and two negative controls are included: a nonexistent fact and a misattribution to the wrong person.
For profile changes, verify that hypotheses and counterexamples are preserved and are not mistaken for behavioral instructions.
The semantic review compares the source with the patch. After application, mechanically confirm that the approved patch was applied.
Search probes are a separate responsibility: verifying discoverability and answer_state in the final index.

```bash
# After applying the approved exact patch, run with the same Vault/DB/Qdrant settings as the Gateway
uv run pvg-wiki --vault . compact-finish .tmp/curating/plan.json
```

Put the approved source deletion list in `review.delete_paths`, and `updated|unchanged` with the reason in `review.user_knowledge`.
A source that was selected but is kept still gets its item judgments completed, and its content is left as is.
This command checks coverage against the Git base, duplicate IDs, deferral preservation, targets, change scope, and character reduction,
and reports new metadata/reference/cycle errors and conflict changes in one pass through before/after Wiki analysis.
After confirming that the applied content matches the reviewed draft, it calls the existing incremental indexing, and checks that the Qdrant points
of the deleted paths are 0 and the paths and answer_state of all search probes. Each search uses `refresh=false`.
An index that is already current is not reindexed, and a rerun completed with the same input also reuses the stored search results.
`finish_checkpoint` is stored only inside the plan. On failure, quota, stale/fallback, or change detection, no completion record is written.
After the quota is restored, retry with the same command; initializing a new DB, changing the model, or recreating the collection is outside the scope of this command.
With an explicit `EMBEDDING_PROVIDER=none`, the same required search probes instead run against the current Markdown, bound to the plan and the source document hashes; no Qdrant or embedding is used and no index metadata is written. With semantic search enabled, all the index requirements above still apply.
It must use an index compatible with the existing Gateway DB, and indexing by the CLI and the server rejects duplicate runs with a common lock.
Do not arbitrarily connect to another Gateway configuration that uses the DB/index or to another Vault checkout.

A failed check is `blocked` with exit code 1. Run time is shown in `timings`, split into the first Vault read, mechanical checks, and indexing/search.
The command does not automatically prove approval or semantic preservation, and keeps `approval=not_checked`.
It also does not automatically apply, DELETE, commit, or push. Use `compact-check` only when a read-only diagnosis without indexing is needed.
Separately check the live inbound references across all Markdown links and the transmission checkpoint of the sources to be deleted.

## 6. Approval and deletion gates

Obtain approval for the exact plan in the current conversation. Present the summary below and the reviewable patch instead of the full graph.

```text
Base commit: <full commit id>
Source/target paths and SHA-256: <plan JSON>
Plan scope SHA-256 / target diff binding: <scope_sha256> / <patch_sha256>
Delete exactly: <paths and SHA-256>
Active Markdown characters: <before> -> <after> (<delta>, <ratio>)
Unmarked claim items: <provenance.unmarked_items>
Curator documents: <ledger_rows> rows (<superseded_rows> superseded), brief <brief_chars>/<brief_cap>
Warnings: <warnings>
Validation probes: <passed>/<total>
Requested external actions: <none|commit|push>
```

If the source/target hashes, base, paths, patch, or external actions change, the approval is void.
Do not treat "continue" or "clean it up" as new approval for deletion, commit, or push.

Every raw/legacy source to retire must satisfy all of the following.
- It is committed, the raw is not of the current date, and every whole-file item has a final disposition other than `hold`.
- A file with even one hold is kept, and its durable meaning is verified in the approved target together with source support.
- The active Codex and Claude clients support per-date payload checkpoints, and the transmission of the corresponding fragment succeeded. If this cannot be confirmed, keep the file because of the risk of retransmission.
- The exact deletion path and hash match, there is no live inbound reference, and the active Markdown character count decreases.
- There is no new integrity error, the critical probes pass, the final Qdrant fingerprint matches the Vault (with `EMBEDDING_PROVIDER=none`, the current Markdown and its hashes instead), and the deleted sources are not retrieved.

If the final search cannot be verified because of a Cloudflare quota or Qdrant error, do not complete or commit the destructive work.

Note: the date of a raw is the local date of the PC where the hook ran. A previous day's record may be retransmitted late and change the file.
If there is no transmission confirmation before applying, or the source hash differs from the plan, do not silently edit the approved plan to exclude
only that file. Stop applying, recreate the plan, and obtain new approval (excluding it requires new approval). The exact
hash check immediately before deletion is not an atomic guarantee against concurrent writes, and no distributed consistency among multiple writers is claimed.

Do not widen the scope by resolving existing unrelated warnings every time, but block new errors and semantic loss from this change.
Reflect a conflict resolution in the canonical state and body and in the exact contradicts edge, and restore that edge on reopen.
Do not create new receipt/conflict events.

## 7. Audit, recovery, and stop

Include the target changes and the source deletion together in one approved single-parent compaction commit, and leave the following.

```text
PVG-Curation-Base: <full parent commit>
PVG-Curation-Plan-SHA256: <scope_sha256>
PVG-Active-Characters: <before> -> <after>
PVG-Validation: <passed>/<total>
```

Recover deleted raw with `git show <base-commit>:<path>`. This is active-store eviction, not complete deletion of
personal information from Git history. Do not force-push main, and keep one full-repository backup.
Do not introduce a new DB, graph service, scheduler, audit store, tag, archive branch, or Git note.

Stop applying if there is dirty tracked Markdown, base/upstream drift, an approval mismatch, a failure of coverage, meaning, or search verification, or if a secret/purge
or an out-of-scope change is needed. Before approval, do not modify tracked files;
if a failure occurs after applying, do not commit or push, and do recovery only within the approved scope.
An individual uninterpretable source can be deferred, but removing it from an already approved patch or moving it to another batch requires new approval.

## 8. Migration from topic documents

This is a one-time pass for a project that already has topic documents.

1. Read the existing `20_Projects/<Project>/*.md`. Extract the decisions, deferrals, supersessions, goal changes and open questions into `DECISIONS.md` rows, with the best known date or `-`. Write `BRIEF.md` from them.
2. Type the source by hand at the end of each row, inside the last cell: `<!-- pvg-src: doc:20_Projects/Project/topic.md#Heading%20Text -->`. Percent-encode spaces. The path must be a topic or user document at the Git base (not a brief or a ledger), and the heading must exist in that file; the tool checks both. This is the only marker you type; `item:` markers are still never typed.
3. Later curations may shrink the topic documents.

Existing `10_User/` documents are not migrated. Link them from `PROFILE.md`.
