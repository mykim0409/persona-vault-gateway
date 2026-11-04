---
name: persona-vault
description: Search PersonaVault Gateway and submit user-requested raw notes. Use when the user asks to recall, search, remember, save, log, record, hand off, or use PersonaVault, Obsidian, vault, memory, or long-term notes.
---

# PersonaVault

Use PersonaVault Gateway as the read/search path for compacted knowledge and raw evidence.

## Prefer the MCP Tools

If the session lists the PersonaVault MCP tools `pvg_search` and `pvg_memo` (Claude Code names them
`mcp__plugin_persona-vault_pvg__pvg_search` and `..._pvg_memo`), call them instead of the helper commands.
They follow the same rules and use the same token config. Their arguments are the helper flags with
underscores (`view`, `limit`, `note_type` for `--type`, `session_id`, `evidence`, `tags`), and the memo body is the
`body` argument instead of stdin, so the OS reference is not needed to call them. `pvg_search` never refreshes the index.
If they are not listed, use the helpers exactly as below. If a tool reports missing config, a rejected token, or
`client_upgrade_required`, follow the installer and upgrade steps in the matching OS reference.

## Choose the Execution Environment First

Both OS surfaces run the same helper behavior with the same `--long-flags` (`--view`, `--project`, `--help`);
only the launcher path, quoting, and installer differ. Use the OS and shell of the tool that will run the helper,
not the Gateway server's OS. Use the session's environment metadata; do not run a series of Linux probes to
rediscover a known Windows host. Read only the matching reference before invoking a helper:

- **Native Windows, PowerShell or cmd.exe:** [Windows commands](references/windows.md). Use the `.ps1` launchers.
- **macOS/Linux or an explicitly selected WSL/remote Linux shell:** [POSIX commands](references/posix.md).

Git Bash on Windows does not imply a WSL installation. Use the Windows helpers through PowerShell rather
than trying `bash`, `which`, `command -v`, `/tmp`, or `source` for native Windows setup. Only choose WSL
when it is the intended execution environment, with its own helpers and config.
If the helper is absent from PATH, check its documented installation path once. A missing PATH entry
does not mean the token is missing. Do not scan the filesystem or try both OS installers.

## Rules

- Never ask the user to paste a token into chat.
- Never write directly to the vault repo.
- Keep PersonaVault context with the main agent. Give subagents only task-specific facts and constraints, and delegate PersonaVault search explicitly when needed.
- Trusted hooks automatically capture completed turns as temporary raw conversation evidence.
- Use `pvg_search` (or `pvg-rag-search`) when the user asks about prior notes, memory, project history, decisions, or context that may already exist in PersonaVault.
- Use `pvg_memo` (or `pvg-agent-memo`) only when the current user explicitly asks to save, log, record, remember, or hand off information. Do not infer a save request from usefulness.
- Treat an explicit note as raw evidence for later curation, never as approved or canonical knowledge.
- If the helper is absent at its installation path or reports missing config, give the installer from the selected OS reference. Do not switch shells as a recovery loop.
- Never print or paste the config file: it contains the token. Check existence only, then let the helper load it.

- If the user has no token, tell them to create or rotate one at:

```text
<gateway-url>/admin/tokens
```

Then rerun the installer for their OS.

If a helper reports `client_upgrade_required`, update `persona-vault` from the configured marketplace, rerun the installer, and retry the unchanged request.

## Explicit Notes

Submit one note for one coherent subject. The v3 note types are:

- `observation`: a sourced fact, outcome, preference, or environment-specific behavior
- `proposal`: a suggested decision, correction, rule, or procedure that still needs curation
- `handoff`: temporary context another agent needs to resume work

The helper defaults to an observation; use `--type observation|proposal|handoff` to select another note type. `--kind` supplies a narrower `note_kind` classification; it does not change the v3 capture kind.

Before saving, search the same subject and source. Skip copied or same-source echoes. Preserve attribution, negation, disagreements, chronology, applicability, and uncertainty. Choose provenance explicitly, and give independent runs distinct session IDs and evidence references with `--provenance`, `--session-id`, and `--evidence`.

Do not save:

- progress updates or step-by-step logs
- help output, smoke tests, or temporary errors
- source code or repository documentation copied verbatim
- information trivially recoverable from the current code
- an unattempted procedure presented as observed fact
- a conclusion merely repeated from another source

Keep each claim with its known actor, action, outcome, time, essential conditions and negation in the same paragraph or table row.
Unknown facts stay unknown; capture time is not the event time. Use the existing subject, provenance and evidence fields rather than adding a new schema.
Link to a specific section only for longer procedures or evidence; do not move the claim's essential conditions behind that link.
Do not create extra notes or duplicate the automatic transcript just to satisfy this writing guidance.

A compact observation can use:

```text
Observation: <actor> performed <action> under <conditions>, with <outcome> at <time, if known>.
Evidence: run, file, commit, issue, transcript, or prior PersonaVault ID
Uncertainty: what remains unverified
```

Use the selected OS reference for how to pass the stdin body; do not translate a Bash pipeline literally into PowerShell.

## RAG Search

Search combines semantic and keyword matching, so exact names, IDs, and error strings are useful. The public views are:

- `all`: broad search across current knowledge and supporting material; this is the default
- `current`: compacted current knowledge
- `evidence`: temporary raw source material
- `history`: prior states and chronology
- `conflicts`: unresolved competing claims

Use `current` for accepted context, `evidence` for source support, and `history` for chronology.
The OS reference shows search and temporary `PVG_RAG_REFRESH` syntax. Keep normal searches read-only (`refresh=false`).

Normal searches combine stored semantic matches with a live keyword scan. A stale index does not make results keyword-only: stored semantic matches are used only when their source hash is verified against the current document, and lexical matches fill in the rest. If the response reports `index.stale=true`, use refresh only when fresh semantic matching is necessary. If `index.search_mode=keyword`, use the returned keyword evidence and do not request refresh; the Gateway resumes pending indexing after its stored limit reset.

Search returns sources, not a truth verdict. `answer_state=evidence` and `lexical_match` mean retrieval, not support for the question; even an approved canonical can contradict its premise. `abstain` means no relevant sources were found, not that the claim is false. `review_required` flags unresolved source conflicts: inspect their scope and present relevant competing claims without choosing a winner. Read attribution, negation, dates, quantities and conditions in the excerpts; do not add a separate verification tool call. Legacy `supported` is not proof either.

Read each passage's conditions and negation. `linked_context` is bounded supplemental context, not proof of a missing claim.
If excerpts are omitted, narrow the query only when that missing detail is needed; do not recursively traverse every link or assume a preview is complete.

## Local Curation Tools

Only for user-requested curation of an explicitly selected local Git Vault, use `pvg_compact_read` and
`pvg_compact_record` with an existing plan under that Vault's `.tmp/curating/`. They run the shared `pvg-wiki`
CLI on the MCP host, not the Gateway. Remote token access alone does not provide a local Vault or authorize curation.
Read the Vault's `CURATOR.md` and applicable local rules first. If `pvg-wiki` is absent, ask for local CLI setup;
do not try another OS's commands or automatically install software. Absolute `vault` and `plan` paths are required.

Read `primary` text with its roles and source spans; `context_only` neighbors are supplemental, not reviewed items.
Follow `next_cursor` for remaining text. Positions are Unicode code points within the item's body, not UTF-16 offsets.
For multiple claims in one event, keep its ID and record `parts` with separate spans, dispositions, targets, reasons and quotes.
Preserve negation, scope and attribution; subagent text is not the user's preference. Do not infer judgments from keywords.
Reuse the latest `plan_sha256`; record replaces each supplied ID's complete judgment, including all its parts.
Inspect `unreviewed_ranges`: a gap cannot pass final review. Range coverage does not establish that all meaning was preserved.

These tools only read Git-base raw and edit the staged review plan. They do not write canonical notes, delete raw,
approve changes, commit or reindex. Use the existing independent review and exact approval gates from `CURATOR.md`;
`compact-record` is not `compact-review --record`. Existing whole-item reviews still work.
