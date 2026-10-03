# Evaluation error book

**English** | [한국어](evaluation-error-book.ko.md)

This book records cases where a PersonaVault search/usage benchmark violated the expected behavior, so that they can be reproduced and regressions prevented. Successful runs are tracked as aggregate metrics; only failures are recorded, one row each.

## Record template

| run_id | scenario_id | bundle | category | sanitized_query | expected | actual | evidence_ref | cause_or_hypothesis | next_action | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `20260716-01` | `cross-agent-01` | `experiences` | `retrieval_miss` | `재색인 실패 복구 방법` | returns `ep_qdrant_rebuild` | no related episode | `benchmark/runs/20260716-01` | error signature mismatch | normalize alias/signature, then rerun | `open` |

The sample query is Korean on purpose: it is a multilingual fixture (it means "how to recover from a reindex failure"), so keep it as is.

`status` is one of `open`, `fixed`, or `accepted`. If the cause is not yet known, do not state a guess as fact; mark it as a hypothesis in `cause_or_hypothesis`.

## Failure categories

| category | Criterion |
| --- | --- |
| `retrieval_miss` | A required document is not within the allowed rank |
| `authority_leak` | Agent, raw, or derived material is presented as canonical |
| `stale_current` | A superseded or outdated claim is returned as a current fact |
| `negative_transfer` | A solution from another environment or repository is used without checking that it applies |
| `conflict_hidden` | Conflicting evidence is omitted, or one side is chosen without evidence |
| `provenance_error` | Repetition of the same source is counted as independent verification, or fallback provenance is shown as an explicit value |
| `unsupported_answer` | An answer is asserted despite insufficient evidence, failing to abstain |
| `duplicate_subject` | The same subject is duplicated as equivalent top answers |
| `cross_scope_miss` | An alias, error signature, multilingual, or cross-project link is missed |
| `metadata_error` | Metadata parsing, normalization, or source-hash judgment is wrong |
| `harness_error` | The benchmark fixture, index preparation, or runner failed, not the product behavior |

## Operating rules

1. Record the expected value, the actual value, and the evidence using reproducible IDs and repository-relative paths.
2. Write one primary failure category per row, and put cascading symptoms in `actual`.
3. Excerpt queries and outputs minimally, and remove tokens, credentials, personal hosts, usernames, and local absolute paths.
4. Do not copy the original transcript or the full tool log; leave only a reference to an access-controlled benchmark artifact.
5. After a fix, rerun the same scenario, add a new run, and then change the existing row to `fixed`. Do not erase the past failure content.
6. Do not store transient help output, smoke-test output, installation checks, or one-off network errors in the Vault. If needed, keep them in a CI artifact or benchmark run log outside the Vault.
7. Record a failure as a separate `episode` only when it is reusable for other work and has real observation, applicability conditions, and evidence. Do not promote the benchmark book itself to canonical or candidate.
