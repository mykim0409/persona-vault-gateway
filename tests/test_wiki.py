from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway.core as core
from gateway.cli import analysis_for, run
from gateway.core import Settings
from gateway.wiki import analyze_documents, build_compaction_plan, health_report


def write_note(vault: Path, path: str, **metadata: Any) -> None:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in {"pv_schema": 1, **metadata}.items()
    )
    target.write_text(f"---\n{frontmatter}\n---\n\n# {metadata['id']}\n\nTest claim.\n", encoding="utf-8")


def pure_analysis_checks() -> None:
    source_a = {
        "document_id": "run_a",
        "memory_type": "episode",
        "kind": "procedure",
        "outcome": "success",
        "session_id": "session-a",
        "provenance_mode": "direct_observation",
        "evidence_refs": ["execution-a"],
        "subject_id": "retry-policy",
        "topics": ["retry"],
        "applicability": {"repository": "demo", "operating_system": "linux"},
        "document_hash": sha256(b"source-a").hexdigest(),
    }
    source_b = {
        **source_a,
        "document_id": "run_b",
        "session_id": "session-b",
        "evidence_refs": ["execution-b"],
        "method_refs": ["run_a"],
        "document_hash": sha256(b"source-b").hexdigest(),
    }
    echo = {
        "document_id": "echo_a",
        "memory_type": "derived_view",
        "kind": "summary",
        "provenance_mode": "derived",
        "derived_from": ["run_a"],
        "subject_id": "retry-policy",
        "topics": ["retry"],
        "source_refs": ["run_a"],
        "source_hashes": {"run_a": source_a["document_hash"]},
    }
    candidate = {
        "document_id": "cand_retry",
        "memory_type": "candidate",
        "kind": "procedure",
        "provenance_mode": "derived",
        "derived_from": ["run_a", "run_b"],
        "subject_id": "retry-policy",
        "topics": ["retry"],
        "applicability": {"repository": "demo", "operating_system": "linux"},
        "repository_sources": [{"repo_id": "demo", "commit": "abcdef1", "path": "src/retry.py"}],
    }
    alias_a = {"document_id": "alias_a", "memory_type": "canonical", "subject_id": "subject-a", "subject_aliases": ["공통 별칭"]}
    alias_b = {"document_id": "alias_b", "memory_type": "canonical", "subject_id": "subject-b", "subject_aliases": ["공통 별칭"]}
    invalid_repo = {
        "document_id": "bad_repo",
        "memory_type": "episode",
        "repository_sources": [{"repo_id": "demo", "commit": "not-a-commit", "path": "../secret"}],
    }

    documents = [source_a, source_b, echo, candidate, alias_a, alias_b, invalid_repo]
    analysis = analyze_documents(documents)
    assert analysis["retrieval_states"] == {"cand_retry": "machine_corroborated"}
    support = analysis["candidate_support"]["cand_retry"]
    assert support["independent_reproductions"] == 2
    assert support["reproduction_sessions"] == ["session-a", "session-b"]
    assert analysis["by_id"]["echo_a"]["provenance_family_ids"] == analysis["by_id"]["run_a"]["provenance_family_ids"]
    assert analysis["by_id"]["echo_a"]["summary_state"] == "fresh"
    assert analysis["alias_collisions"][0]["subject_ids"] == ["subject-a", "subject-b"]
    assert analysis["by_id"]["bad_repo"]["repository_validation"] == "invalid"

    same_session = analyze_documents(
        [
            source_a,
            {**source_b, "session_id": "session-a"},
            candidate,
        ]
    )
    assert "cand_retry" not in same_session["machine_corroborated"]

    missing_applicability = analyze_documents(
        [
            {**source_a, "applicability": {}},
            source_b,
            candidate,
        ]
    )
    assert "cand_retry" not in missing_applicability["machine_corroborated"]

    changed = [{**document} for document in documents]
    changed[0]["document_hash"] = sha256(b"source-a-changed").hexdigest()
    stale = analyze_documents(changed)
    assert stale["by_id"]["echo_a"]["summary_state"] == "stale"

    conflicted = analyze_documents(
        documents
        + [
            {
                "document_id": "cand_conflicted",
                "memory_type": "candidate",
                "kind": "procedure",
                "provenance_mode": "derived",
                "derived_from": ["run_a", "run_b"],
                "subject_id": "retry-policy",
                "applicability": {"repository": "demo", "operating_system": "linux"},
                "repository_sources": [{"repo_id": "demo", "commit": "abcdef1", "path": "src/retry.py"}],
                "relations": {"contradicts": ["run_a"]},
                "conflict_state": "unresolved",
            }
        ]
    )
    assert "cand_conflicted" not in conflicted["machine_corroborated"]
    report = health_report(conflicted)
    assert report["status"] == "attention" and report["unresolved_conflicts"] >= 1

    merged = analyze_documents(
        [
            {
                "document_id": "cur_merge",
                "memory_type": "derived_view",
                "kind": "merge-receipt",
                "evidence_refs": [
                    {
                        "kind": "repository",
                        "locator": "persona-vault@abcdef1:40_Agents/test/episodes/source.md",
                    }
                ],
                "repository_sources": [
                    {
                        "kind": "repository",
                        "repo_id": "persona-vault",
                        "commit": "abcdef1",
                        "path": "40_Agents/test/episodes/source.md",
                    }
                ],
            },
            {
                "document_id": "kn_merged",
                "memory_type": "canonical",
                "derived_from": ["cur_merge"],
            },
        ]
    )
    assert not merged["stale_summaries"] and not merged["broken_references"]


def conflict_list_checks() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        vault = root / "vault"
        settings = Settings(vault, root / "gateway.db", "test")
        write_note(
            vault,
            "50_Knowledge/current.md",
            id="kn_current",
            memory_type="canonical",
            review_state="human_accepted",
            temporal_state="current",
            provenance_mode="human_asserted",
            relations={"contradicts": ["cand_change"]},
        )
        write_note(
            vault,
            "40_Agents/test/candidates/change.md",
            id="cand_change",
            memory_type="candidate",
            kind="decision",
            provenance_mode="reported",
        )
        before = analysis_for(settings)
        conflict_id = before["conflict_queue"][0]["conflict_id"]

        listed = run(["--vault", str(vault), "conflicts", "list"])
        assert listed["conflicts"][0]["conflict_id"] == conflict_id
        assert not (vault / "60_Curation").exists()


def compaction_graph_checks() -> None:
    old_a = {
        "document_id": "conv_old_a",
        "path": "30_Conversations/raw/2026/08/01/a.md",
        "memory_type": "transcript",
        "projects": ["Demo"],
        "subject_id": "retry-policy",
        "document_hash": "a" * 64,
        "text": """# Conversation

### user

<!-- pvg-event {"event_id":"turn-1-user","timestamp":"2026-08-01T01:00:00Z"} -->

Keep retries bounded.

### assistant

<!-- pvg-event {"event_id":"turn-1-assistant","timestamp":"2026-08-01T01:01:00Z"} -->

Implemented it.

### Internal answer heading

This is still part of the assistant event.
""",
    }
    old_b = {
        **old_a,
        "document_id": "conv_old_b",
        "path": "30_Conversations/raw/2026/08/02/b.md",
        "document_hash": "b" * 64,
        "text": """# Conversation

### assistant: subagent

<!-- pvg-event {"event_id":"turn-2-subagent","timestamp":"2026-08-02T01:00:00Z"} -->

**Agent delegation (not a user statement)**

Check retry behavior.

**Result**

The retry remained bounded.
""",
    }
    newer = {
        **old_a,
        "document_id": "conv_newer",
        "path": "30_Conversations/raw/2026/08/03/newer.md",
        "projects": ["Other"],
        "subject_id": "other-subject",
        "document_hash": "c" * 64,
    }
    current = {
        **old_a,
        "document_id": "conv_current",
        "path": "30_Conversations/raw/2026/08/24/current.md",
        "document_hash": "d" * 64,
    }
    canonical = {
        "document_id": "kn_retry",
        "path": "20_Projects/Demo/retry.md",
        "memory_type": "canonical",
        "projects": ["Demo"],
        "subject_id": "retry-policy",
        "text": "Retries are bounded.",
    }
    related = {
        "document_id": "kn_related",
        "path": "50_Knowledge/backoff.md",
        "memory_type": "canonical",
        "projects": ["Shared"],
        "subject_id": "backoff",
        "text": "Use exponential backoff.",
    }
    documents = [old_a, old_b, newer, current, canonical, related]
    tracked = {document["path"] for document in documents}
    plan = build_compaction_plan(
        documents,
        before="2026-08-24",
        tracked_paths=tracked,
        semantic={
            "status": "current",
            "queries": 3,
            "edges": [
                {
                    "source_path": old_a["path"],
                    "target_document_id": "kn_related",
                    "target_path": related["path"],
                    "target_memory_type": "canonical",
                    "score": 0.82,
                }
            ],
        },
        revision="1" * 40,
    )
    selected = plan["selected"]
    assert selected["source_paths"] == [old_a["path"], old_b["path"]]
    assert selected["source_items"] == 4
    assert selected["compression_gate"]["ready_for_retirement"] is False
    assert plan["excluded"] == {"current_or_future_raw": 1}
    node_types = {node["id"]: node["type"] for node in selected["graph"]["nodes"]}
    assert node_types["document:kn_retry"] == "canonical_candidate"
    assert node_types["document:kn_related"] == "canonical_candidate"
    assert sum(node_type == "raw_item" for node_type in node_types.values()) == 4
    assert any(edge["type"] == "semantic_candidate" for edge in selected["graph"]["edges"])

    root_raw = {
        **old_a,
        "document_id": "legacy_root_raw",
        "path": "30_Conversations/raw/legacy.md",
    }
    legacy_agent = {
        **old_a,
        "document_id": "legacy_agent",
        "path": "40_Agents/test/candidates/legacy.md",
        "created_at": "2026-07-31T12:00:00Z",
    }
    fallback = build_compaction_plan(
        [root_raw, legacy_agent],
        before="2026-08-24",
        tracked_paths={root_raw["path"], legacy_agent["path"]},
        fallback_dates={root_raw["path"]: "2026-07-30T12:00:00Z"},
    )
    assert fallback["excluded"] == {}
    assert fallback["selected"]["source_paths"] == [root_raw["path"], legacy_agent["path"]]
    source_node = next(
        node for node in fallback["selected"]["graph"]["nodes"] if node.get("path") == root_raw["path"] and node["type"] == "source_file"
    )
    assert (source_node["date"], source_node["date_basis"]) == ("2026-07-30", "git_first_add")
    legacy_plan = build_compaction_plan(
        [legacy_agent], before="2026-08-24", tracked_paths={legacy_agent["path"]}
    )
    legacy_node = next(
        node for node in legacy_plan["selected"]["graph"]["nodes"] if node["type"] == "source_file"
    )
    assert (legacy_node["date"], legacy_node["date_basis"]) == ("2026-07-31", "created_at")

    tie_a_old = {
        **old_a,
        "document_id": "tie_a_old",
        "path": "30_Conversations/raw/2026/08/01/tie-a-old.md",
        "projects": ["Tie A"],
        "text": "x",
    }
    tie_a_later = {
        **tie_a_old,
        "document_id": "tie_a_later",
        "path": "30_Conversations/raw/2026/08/02/tie-a-later.md",
        "text": "x" * 1000,
    }
    tie_b_old = {
        **old_a,
        "document_id": "tie_b_old",
        "path": "30_Conversations/raw/2026/08/01/tie-b-old.md",
        "projects": ["Tie B"],
        "text": "x" * 10,
    }
    tie_plan = build_compaction_plan(
        [tie_a_old, tie_a_later, tie_b_old],
        before="2026-08-24",
        tracked_paths={tie_a_old["path"], tie_a_later["path"], tie_b_old["path"]},
    )
    assert tie_plan["selected"]["source_paths"] == [tie_b_old["path"]]
    bounded = build_compaction_plan(documents, before="2026-08-24", max_characters=len(old_a["text"]))
    assert bounded["selected"]["source_paths"] == [old_a["path"]]
    oversized = build_compaction_plan(documents, before="2026-08-24", max_characters=1)
    assert oversized["selected"]["source_paths"] == [old_a["path"]]
    assert oversized["selected"]["oversized_source"] is True
    assert build_compaction_plan(documents, before="2026-08-24", max_sources=1)["selected"]["source_files"] == 1
    project_source = {**old_a, "subject_id": None}
    project_plan = build_compaction_plan([project_source, canonical], before="2026-08-24")
    assert any(node["id"] == "document:kn_retry" for node in project_plan["selected"]["graph"]["nodes"])
    assert any(edge["type"] == "same_project_candidate" for edge in project_plan["selected"]["graph"]["edges"])


def qdrant_neighbor_checks() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        settings = Settings(root / "vault", root / "gateway.db", "test", embedding_provider="hash")
        settings.db_path.touch()
        document = {
            "document_id": "conv_source",
            "path": "30_Conversations/raw/2026/08/01/source.md",
            "text": "source text " * 400,
        }
        calls: list[tuple[str, dict[str, Any]]] = []
        original_current = core.qdrant_index_current
        original_json = core.qdrant_json

        def fake_json(_settings: Settings, _method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            assert body is not None
            calls.append((path, body))
            return {
                "result": [
                    {
                        "points": [
                            {
                                "score": 0.81,
                                "payload": {
                                    "document_id": "kn_target",
                                    "path": "50_Knowledge/target.md",
                                    "memory_type": "canonical",
                                },
                            }
                        ]
                    }
                    for _query in body["searches"]
                ]
            }

        try:
            core.qdrant_index_current = lambda _settings: True
            core.qdrant_json = fake_json
            result = core.qdrant_compaction_neighbors(settings, [document])
        finally:
            core.qdrant_index_current = original_current
            core.qdrant_json = original_json

        assert result["status"] == "current"
        assert result["queries"] > 1
        assert result["edges"] == [
            {
                "source_path": document["path"],
                "target_document_id": "kn_target",
                "target_path": "50_Knowledge/target.md",
                "target_memory_type": "canonical",
                "score": 0.81,
            }
        ]
        assert all(path.endswith("/points/query/batch") for path, _body in calls)
        assert all(isinstance(query["query"], str) for _path, body in calls for query in body["searches"])
        assert all(
            query["filter"]["must_not"][0]["key"] == "path"
            for _path, body in calls
            for query in body["searches"]
        )


def main() -> None:
    pure_analysis_checks()
    conflict_list_checks()
    compaction_graph_checks()
    qdrant_neighbor_checks()
    print(json.dumps({"wiki_analysis": "ok", "conflicts": "ok", "compaction_graph": "ok"}))


if __name__ == "__main__":
    main()
