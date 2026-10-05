from __future__ import annotations

from copy import deepcopy
import json
import re
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway.core as core
from gateway.cli import analysis_for, run
from gateway.core import Settings
from gateway.wiki import (
    BRIEF_CAP, CURATOR_KINDS, _brief_chars, _cells, _sess, _src_docs, _src_ids, analyze_documents, brief_sections,
    build_compaction_plan, check_compaction, health_report, ledger_entries,
)


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


DEMO, TOPIC = "20_Projects/Demo", "20_Projects/Demo/topic.md"
LEDGER, BRIEF = f"{DEMO}/DECISIONS.md", f"{DEMO}/BRIEF.md"
PROFILE, OBSERVATIONS = "10_User/PROFILE.md", "10_User/OBSERVATIONS.md"
HEADERS = {
    "decision_ledger": "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |",
    "user_ledger": "| id | 날짜 | 유형 | 상태 | 내용 | 근거 | 독립 세션 |",
}
SEPARATOR = "|" + "---|" * 7
LEDGER_PATHS = {"decision_ledger": LEDGER, "user_ledger": OBSERVATIONS}
SECTIONS = {
    "brief": ("현재 목표", "유효한 결정", "미뤄진 것", "대체된 것", "열린 질문", "최근 변화"),
    "user_profile": ("역할·맥락", "확인된 선호", "가설", "제약", "에이전트에 준 피드백", "반례·철회", "최근 변화"),
}
OLD = "item:aaaaaaaaaaaaaaaa"
FILLER = "x " * 1500  # keeps the raw batch larger than the curated result, as the active-character gate requires


def mark(*tokens: str) -> str:
    return f"<!-- pvg-src: {' '.join(sorted(tokens))} -->"


def document(path: str, text: str) -> dict[str, Any]:
    """A document as cli.documents_at_revision builds it."""
    return {
        **core.document_metadata(path, text), "path": path, "text": core.markdown_body(text),
        "document_hash": sha256(text.encode()).hexdigest(),
    }


def note(path: str, body: str, kind: str = "note", **metadata: Any) -> dict[str, Any]:
    front = {
        "pv_schema": 1, "id": re.sub(r"\W", "_", path), "memory_type": "canonical", "review_state": "human_accepted",
        "temporal_state": "current", "provenance_mode": "human_asserted", "retrieval_tier": "primary", "kind": kind,
        **({"projects": ["Demo"]} if path.startswith("20_Projects/") else {}), **metadata,
    }
    text = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front.items())
    return document(path, f"{text}\n---\n\n{body}\n")


def raw(path: str, session: str, *events: tuple[str, str, Any], projects: tuple[str, ...] = ("Demo",), **metadata: Any) -> dict[str, Any]:
    """Framed raw capture of (event_id, role, text); role subagent takes (request, result) and renders a delegation."""
    parts = []
    for event_id, role, content in events:
        heading, text = role, content
        if role == "subagent":
            heading = "subagent: reviewer"
            text = f"**Agent delegation (not a user statement)**\n\n{content[0]}\n\n**Result**\n\n{content[1]}"
        parts.append(f"### {heading}\n\n<!-- pvg-event {json.dumps({'event_id': event_id, 'role': role})} -->\n\n{text}")
    extra = {"session_id": session} if session else {}
    return note(path, "\n\n".join(parts), kind="conversation", projects=list(projects), **extra, **metadata)


def row(
    row_id: str, status: str = "유효", content: str = "내용", *, kind: str = "결정", day: str = "2026-01-01",
    last: str = "-", tokens: tuple[str, ...] = (),
) -> str:
    tail = f"{last} {mark(*tokens)}" if tokens else last
    return f"| {row_id} | {day} | {kind} | {status} | {content} | 이유 | {tail} |"


def ledger(kind: str, *rows: str, path: str = "", header: str = "", **metadata: Any) -> dict[str, Any]:
    return note(path or LEDGER_PATHS[kind], "# 장부\n\n" + "\n".join([header or HEADERS[kind], SEPARATOR, *rows]), kind=kind, **metadata)


def listed(*row_ids: str, tokens: tuple[str, ...] = ()) -> str:
    """A BRIEF/PROFILE table whose first cells are ledger ids."""
    return "\n".join([
        "| id | 날짜 | 결정 | 이유 |", "|---|---|---|---|",
        *(f"| {row_id} | 2026-01-01 | 내용 | 이유{' ' + mark(*tokens) if tokens else ''} |" for row_id in row_ids),
    ])


def brief_text(kind: str = "brief", skip: str = "", **sections: str) -> str:
    return "# 요약\n\n" + "\n\n".join(f"## {name}\n\n{sections.get(name, '-')}" for name in SECTIONS[kind] if name != skip)


def brief(kind: str = "brief", text: str = "", **sections: str) -> dict[str, Any]:
    return note({"brief": BRIEF, "user_profile": PROFILE}[kind], text or brief_text(kind, **sections), kind=kind)


def scenario(sources: list[dict[str, Any]], *base: dict[str, Any]):
    """Plan over framed raw sources. check(items, targets) is check_compaction on the after-state; unnamed items are discarded."""
    before = [*sources, *base]
    plan = build_compaction_plan(before, before="2026-02-01", revision="1" * 40)
    ids = {node["locator"]: node["id"] for node in plan["selected"]["graph"]["nodes"] if node["type"] == "raw_item"}
    hashes = {doc["path"]: doc["document_hash"] for doc in before}

    def check(items: list[dict[str, Any]], targets: list[dict[str, Any]], deleted: list[str] | None = None, **options: Any) -> dict[str, Any]:
        deleted = [source["path"] for source in sources] if deleted is None else deleted
        named = {locator for item in items for locator in item.get("ids", [item.get("id")])}
        resolved = [
            {**{key: value for key, value in item.items() if key not in {"id", "ids"}},
             **({"ids": [ids[locator] for locator in item["ids"]]} if "ids" in item else {"id": ids[item["id"]]})}
            for item in items
        ] + [
            {"id": node_id, "disposition": "discard", "targets": [], "reason": "No lasting value."}
            for locator, node_id in ids.items() if locator not in named
        ]
        after = {doc["path"]: doc for doc in before if doc["path"] not in deleted}
        after.update((doc["path"], doc) for doc in targets)
        user_changed = any(
            path.startswith("10_User/") and hashes.get(path) != doc["document_hash"] for path, doc in after.items()
        )
        reviewed = deepcopy(plan)
        reviewed["review"].update(
            items=resolved, delete_paths=deleted,
            user_knowledge={"status": "updated" if user_changed else "unchanged", "reason": "Checked."},
        )
        return check_compaction(reviewed, before, list(after.values()), **options)

    return ids, check


def expect(result: dict[str, Any], *errors: str) -> None:
    """No texts: the batch must pass. Otherwise it is blocked and every text is part of some error."""
    if not errors:
        assert result["status"] == "ok" and not result["errors"], result["errors"]
        return
    assert result["status"] == "blocked", result
    for text in errors:
        assert any(text in error for error in result["errors"]), (text, result["errors"])


def ledger_parser_checks() -> None:
    token = "item:0123456789abcdef"
    assert _cells("not a row") is None and _cells("| a | b") is None
    assert _cells("| a | b <!-- pvg-src: item:0123456789abcdef --> |") == ["a", "b"]
    assert _cells(f"|a|b| {mark(token)}") == ["a", "b"] and _cells("| a \\| b | c |") == ["a \\| b", "c"]
    assert _src_ids(f"x {mark(token)} {mark('doc:a%20b#H', _sess('s1'))}") == {token}
    assert _src_docs(mark(token, "doc:a%20b#H")) == {"doc:a%20b#H"} and _src_ids("<!-- pvg-src: item:abc -->") == set()
    assert _src_docs("<!-- pvg-src: doc:a>b -->") == set()
    assert _sess("") == "" and _sess("a") == "sess:" + sha256(b"a").hexdigest()[:8]

    header = HEADERS["decision_ledger"]
    text = "\n".join([
        "Intro.", "", header.replace(" | ", "  |  "), "|:--|:-:|--:|---|---|---|---|",
        f"| D-001 | 2026-01-01 | 결정 | 대체됨 | Use \\| in text | 이유 | D-002 {mark(token)} |",
        f"| D-002 | - | 대체 | 유효 | 내용 | 이유 | — | {mark('doc:20_Projects/Demo/a.md#H%20One')}",
        "", "| D-003 | 2026-01-03 | 결정 | 유효 | after the table | 이유 | - |",
    ])
    rows, problems = ledger_entries(text, "decision_ledger", LEDGER)
    assert problems == [] and [r["id"] for r in rows] == ["D-001", "D-002"], (rows, problems)
    assert rows[0]["successor"] == "D-002" and rows[0]["items"] == {token} and rows[0]["cells"][4] == "Use \\| in text"
    assert rows[1]["successor"] == "" and rows[1]["docs"] == {"doc:20_Projects/Demo/a.md#H%20One"} and rows[1]["date"] == "-"
    assert rows[1]["cells"][6] == "—" and rows[1]["items"] == set() and rows[1]["sess"] == set()
    assert ledger_entries(f"{header}\n{SEPARATOR}", "decision_ledger") == ([], [])

    def problems_of(*lines: str, kind: str = "decision_ledger", head: str = "") -> list[str]:
        return ledger_entries("\n".join([head or HEADERS[kind], SEPARATOR, *lines]), kind, LEDGER)[1]

    missing = [f"ledger table missing or header differs: {LEDGER}"]
    assert ledger_entries("No table.", "decision_ledger", LEDGER) == ([], missing)
    for bad in (header.replace("이유·근거", "이유"), header.replace("id", "ID")):
        assert problems_of(head=bad) == missing, bad
    assert ledger_entries("\n".join([header, SEPARATOR, header, SEPARATOR]), "decision_ledger", LEDGER)[1] == missing
    assert ledger_entries(f"{header}\n{row('D-001')}", "decision_ledger", LEDGER)[1] == [f"ledger separator row missing: {LEDGER}"]
    assert problems_of(row("D-001"), row("D-001")) == [f"ledger id duplicated: {LEDGER}: D-001"]
    assert problems_of(row("D-1"), row("U-001")) == [f"ledger id invalid: {LEDGER}: D-1", f"ledger id invalid: {LEDGER}: U-001"]
    assert problems_of(row("D-0001")) == []
    assert problems_of(row("D-001", "완료")) == [f"ledger 상태 invalid: {LEDGER}: D-001"]
    assert problems_of(row("D-001", kind="결단")) == [f"ledger 유형 invalid: {LEDGER}: D-001"]
    for day in ("2026-13-01", "2026/01/01", "26-01-01", ""):
        assert problems_of(row("D-001", day=day)) == [f"ledger 날짜 invalid: {LEDGER}: D-001"], day
    assert problems_of("| D-001 | 2026-01-01 | 결정 | 유효 | 내용 | 이유 |")[0].startswith(f"ledger row needs 7 cells: {LEDGER}: | D-001")
    assert problems_of(row("D-001", "대체됨")) == [f"ledger 대체됨 requires 후속: {LEDGER}: D-001"]
    assert problems_of(row("D-001", "대체됨", last="D-009")) == [f"ledger 후속 not found: {LEDGER}: D-001"]
    assert problems_of(row("D-001", last="D-001")) == [f"ledger 후속 not found: {LEDGER}: D-001"]
    assert problems_of(row("D-001", "대체됨", last="D-002"), row("D-002"), row("D-003", last="")) == []
    assert problems_of(row("D-001", last="–"), row("D-002", last="—")) == []

    # The same parser reads the user ledger: another header, vocabularies and id prefix; the last cell is a count.
    user = lambda *lines: problems_of(*lines, kind="user_ledger")
    assert user(row("U-001", "확인", kind="선호(명시)", last="2"), row("U-002", "철회됨", kind="철회", last="0")) == []
    assert user(row("U-001", "유효", kind="선호(추론)")) == [f"ledger 상태 invalid: {LEDGER}: U-001"]
    assert user(row("D-001", "가설", kind="맥락")) == [f"ledger id invalid: {LEDGER}: D-001"]
    assert problems_of(row("D-001"), kind="user_ledger", head=header) == missing
    rows = ledger_entries("\n".join([HEADERS["user_ledger"], SEPARATOR, row("U-001", "가설", kind="선호(추론)", last="3")]), "user_ledger")[0]
    assert rows[0]["successor"] == "" and rows[0]["cells"][6] == "3"


def brief_checks() -> None:
    assert BRIEF_CAP == 8000
    sections = brief_sections("# T\n\n## A\n\none\n### sub\n\n## B ##x\n\ntwo\n")
    assert [(name, body.strip()) for name, body in sections] == [("A", "one\n### sub"), ("B ##x", "two")]
    assert _brief_chars(f"  body {mark('item:0123456789abcdef')}  ") == len("body")
    source = "30_Conversations/raw/2026/01/01/a.md"
    ids, check = scenario([raw(source, "s1", ("e0", "user", "We will use SQLite for storage."), ("f", "user", FILLER))], note(TOPIC, "Topic."))
    token = ids["e0"]
    decision = {"id": "e0", "disposition": "decision", "targets": [LEDGER, BRIEF], "quote": "use SQLite for storage", "reason": "Decided."}
    good_ledger = ledger("decision_ledger", row("D-001", tokens=(token,)))

    def good(**sections: str) -> dict[str, Any]:
        return brief(**{"유효한 결정": listed("D-001", tokens=(token,)), **sections})

    result = check([decision], [good_ledger, good()])
    expect(result)
    assert result["warnings"] == [] and result["provenance"] == {"unmarked_items": 0, "examples": []}
    assert result["curation"] == {
        "brief_cap": BRIEF_CAP, "brief_chars": {BRIEF: _brief_chars(good()["text"])},
        "ledger_rows": {LEDGER: 1}, "superseded_rows": {LEDGER: 0},
    }, result["curation"]

    # Headings: all required ones, once each, in order; extra level-2 headings are allowed.
    wrong = "brief headings missing or out of order"
    expect(check([decision], [good_ledger, brief(text=brief_text(skip="미뤄진 것", **{"유효한 결정": listed("D-001", tokens=(token,))}))]), wrong)
    reversed_text = "\n\n".join(f"## {name}\n\n-" for name in reversed(SECTIONS["brief"]))
    expect(check([decision], [good_ledger, brief(text=reversed_text)]), wrong)
    expect(check([decision], [good_ledger, brief(text=good()["text"] + "\n\n## 최근 변화\n\n-\n")]), wrong)
    expect(check([decision], [good_ledger, brief(text=good()["text"] + "\n\n## 메모\n\n-\n")]))

    # Cap: body characters without markers. Markers stay in the file and in the active-character count.
    chars = _brief_chars(good()["text"])
    expect(check([decision], [good_ledger, good()], brief_cap=chars))
    expect(check([decision], [good_ledger, good()], brief_cap=chars - 1), f"brief over the cap: {BRIEF} ({chars} > {chars - 1})")
    marked = brief(text=good()["text"] + "\n" + "\n".join(f"- 메모 {index} {mark(token)}" for index in range(5)))
    marked_chars = _brief_chars(marked["text"])
    assert marked_chars <= len(marked["text"]) - 5 * len(mark(token))
    result = check([decision], [good_ledger, marked], brief_cap=marked_chars)
    expect(result)
    assert result["active_characters"]["after"] == len(marked["text"]) + len(good_ledger["text"]) + len(note(TOPIC, "Topic.")["text"])
    assert result["curation"]["brief_cap"] == marked_chars
    expect(check([decision], [good_ledger, marked], brief_cap=marked_chars - 1), "brief over the cap")
    for cap in (0, -5):
        for call in (lambda: check([decision], [good_ledger, good()], brief_cap=cap), lambda: build_compaction_plan([], before="2026-02-01", brief_cap=cap)):
            try:
                call()
            except ValueError:
                pass
            else:
                raise AssertionError("a brief cap below 1 was accepted")

    # 유효한 결정 may only cite 유효 ledger rows; 현재 목표 and 열린 질문 only warn.
    two_rows = ledger("decision_ledger", row("D-001", "대체됨", last="D-002", tokens=(token,)), row("D-002", tokens=(token,)))
    expect(check([decision], [two_rows, good(**{"유효한 결정": listed("D-002", tokens=(token,))})]))
    expect(check([decision], [two_rows, good()]), f"brief 유효한 결정 lists a non-valid entry: {BRIEF}: D-001")
    expect(check([decision], [good_ledger, good(**{"유효한 결정": listed("D-009")})]), f"brief 유효한 결정 lists a non-valid entry: {BRIEF}: D-009")
    result = check([decision], [good_ledger, good(**{"현재 목표": "- D-009 없음\n- D-001 유효", "열린 질문": "| D-001 | q |\n|---|---|\n| D-009 | q |"})])
    expect(result)
    assert result["warnings"] == [f"brief references ledger id D-009 that is not 유효: {BRIEF}"], result["warnings"]
    result = check([decision], [two_rows, good(**{"유효한 결정": listed("D-002", tokens=(token,)), "현재 목표": "- D-001 대체됨"})])
    expect(result)
    assert result["warnings"] == [f"brief references ledger id D-001 that is not 유효: {BRIEF}"]
    elsewhere = ledger("decision_ledger", row("D-001", tokens=(token,)), path="20_Projects/Elsewhere/DECISIONS.md")
    expect(check([{**decision, "targets": [BRIEF, elsewhere["path"]]}], [elsewhere, good()]), f"brief needs a decision_ledger in its folder: {BRIEF}")

    # The profile is a brief too: 확인된 선호 may only cite 확인 rows of the user ledger.
    preference = {"id": "e0", "disposition": "user_preference", "targets": [OBSERVATIONS, PROFILE], "quote": "use SQLite", "reason": "Stated."}
    tokens = (token, _sess("s1"))
    profile = brief("user_profile", **{"확인된 선호": listed("U-001", tokens=(token,))})
    observed = lambda status: ledger("user_ledger", row("U-001", status, kind="선호(명시)", last="1", tokens=tokens))
    expect(check([preference], [observed("확인"), profile]))
    expect(check([preference], [observed("가설"), profile]), f"profile 확인된 선호 lists a non-confirmed entry: {PROFILE}: U-001")
    expect(check([preference], [profile]), f"profile needs a user_ledger: {PROFILE}")
    expect(check([preference], [observed("확인"), brief("user_profile", text=brief_text("user_profile", skip="가설"))]), f"profile headings missing or out of order: {PROFILE}")

    # A ledger-only change re-checks the brief beside it: a row that just became 대체됨 or 철회됨 may not stay listed.
    ids, check = scenario(
        [raw(source, "s1", ("e0", "user", "We now use Postgres instead of SQLite."), ("f", "user", FILLER))],
        ledger("decision_ledger", row("D-001", tokens=(OLD,))), brief(**{"유효한 결정": listed("D-001", tokens=(OLD,))}),
        ledger("user_ledger", row("U-001", "확인", kind="선호(명시)", last="1", tokens=(OLD, _sess("s0")))),
        brief("user_profile", **{"확인된 선호": listed("U-001", tokens=(OLD,))}),
    )
    replacing = {"id": "e0", "disposition": "supersession", "targets": [LEDGER], "quote": "use Postgres", "reason": "Replaced."}
    replaced = ledger("decision_ledger", row("D-001", "대체됨", last="D-002", tokens=(OLD,)), row("D-002", tokens=(ids["e0"],)))
    expect(check([replacing], [replaced]), f"brief 유효한 결정 lists a non-valid entry: {BRIEF}: D-001")
    retracting = {"id": "e0", "disposition": "user_retraction", "targets": [OBSERVATIONS], "quote": "use Postgres", "reason": "Retracted."}
    retracted = ledger("user_ledger", row("U-001", "철회됨", kind="선호(명시)", last="2", tokens=(OLD, _sess("s0"), ids["e0"], _sess("s1"))))
    expect(check([retracting], [retracted]), f"profile 확인된 선호 lists a non-confirmed entry: {PROFILE}: U-001")


def typed_review_checks() -> None:
    a, b = "30_Conversations/raw/2026/01/01/a.md", "30_Conversations/raw/2026/01/02/b.md"
    ids, check = scenario(
        [
            raw(a, "s1", ("e0", "user", "We will use SQLite for storage."), ("e1", "user", "Postpone   the cache\nlayer until traffic grows."),
                ("e2", "user", "나는 짧고 명확한 답변을 선호한다."), ("f", "user", FILLER)),
            raw(b, "s2", ("sa", "subagent", ("Review the schema carefully.", "The schema looks fine to me.")), ("e4", "user", "Keep the API stable.")),
        ],
        note(TOPIC, "Topic."),
    )
    d0 = {"id": "e0", "disposition": "decision", "targets": [LEDGER, BRIEF], "quote": "use SQLite for storage", "reason": "Decided."}
    d1 = {"id": "e1", "disposition": "deferral", "targets": [LEDGER], "quote": "Postpone the cache   layer", "reason": "Deferred."}
    d2 = {"id": "e2", "disposition": "user_preference", "targets": [OBSERVATIONS], "quote": "짧고 명확한 답변", "reason": "Stated."}
    sess = _sess("s1")

    def docs(*, e1_tokens: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        return [
            ledger("decision_ledger", row("D-001", tokens=(ids["e0"],)), row("D-002", kind="미룸", tokens=(ids["e1"],) if e1_tokens is None else e1_tokens)),
            note(BRIEF, brief_text(**{"유효한 결정": listed("D-001", tokens=(ids["e0"],))}), kind="brief"),
            ledger("user_ledger", row("U-001", "확인", kind="선호(명시)", last="1", tokens=(ids["e2"], sess))),
        ]

    run = lambda *items, **options: check(list(items), docs(), **options)
    result = run(d0, d1, d2)
    expect(result)
    assert result["warnings"] == [] and result["curation"]["ledger_rows"] == {LEDGER: 2, OBSERVATIONS: 1}

    # Quote: whitespace/case-insensitive substring of the item's own words, 4 to 200 characters, required for claims.
    expect(run(d0, {**d1, "quote": "Postpone the queue"}, d2), "quote not found in item")
    for quote in ("use", "a" * 201, "", 123):
        expect(run(d0, {**d1, "quote": quote}, d2), f"quote must be 4 to 200 characters: {ids['e1']}")
    expect(run(d0, d1, {k: v for k, v in d2.items() if k != "quote"}), f"claim item requires a quote: {ids['e2']}")
    discard = {"id": "f", "disposition": "discard", "targets": [], "reason": "Filler."}
    expect(run(d0, d1, d2, {**discard, "quote": "x x x x"}))
    expect(run(d0, d1, d2, {**discard, "quote": "words that are not there"}), "quote not found in item")
    # A delegation event is two items; each quote is checked against its own component only.
    knowledge = lambda locator, quote: {"id": locator, "disposition": "knowledge", "targets": [TOPIC], "quote": quote, "reason": "Reusable."}
    result = run(d0, d1, d2, knowledge("sa#delegation", "Review the schema"), knowledge("sa#result", "looks fine to me"))
    expect(result)
    assert result["provenance"]["unmarked_items"] == 2  # a plain topic document only counts missing markers
    expect(run(d0, d1, d2, knowledge("sa#delegation", "looks fine")), "quote not found in item")
    expect(run(d0, d1, d2, knowledge("sa#result", "Review the schema")), "quote not found in item")

    # Claim dispositions take one id each; the other dispositions may still be grouped.
    expect(run({**d0, "id": None, "ids": ["e0", "e1"]}, d2), "claim items cannot be grouped under ids: decision")
    tail = {"targets": [TOPIC], "reason": "Grouped."}
    expect(run(d0, d1, d2, {"ids": ["f", "e4"], "disposition": "already-covered", **tail}))
    expect(run(d0, d1, d2, {"ids": ["f", "e4"], "disposition": "discard", "targets": [], "reason": "Grouped."}))

    # Subagent items may only be knowledge, already-covered, discard or hold; legacy merge/replace is refused too.
    refused = {
        **dict.fromkeys(("decision", "deferral", "supersession", "goal_change", "question"), [LEDGER]),
        **dict.fromkeys(("user_preference", "user_constraint", "user_feedback", "user_context", "user_retraction"), [OBSERVATIONS]),
        "merge": [TOPIC], "replace": [TOPIC],
    }
    for locator in ("sa#delegation", "sa#result"):
        for disposition, targets in refused.items():
            item = {"id": locator, "disposition": disposition, "targets": targets, "quote": "schema", "reason": "x"}
            expect(run(d0, d1, d2, item), f"subagent item may only be knowledge, already-covered, discard or hold: {ids[locator]}")
        for disposition, targets in (("knowledge", [TOPIC]), ("already-covered", [TOPIC]), ("discard", []), ("hold", [])):
            expect(run(d0, d1, d2, {"id": locator, "disposition": disposition, "targets": targets, "quote": "schema", "reason": "x"}, deleted=[a]))

    # Each disposition family has its own target kinds.
    expect(run({**d0, "targets": [TOPIC]}, d1, d2), f"project claim must target a brief or decision_ledger: {TOPIC}")
    expect(run(d0, d1, {**d2, "targets": [BRIEF]}), f"user claim must target a user_profile or user_ledger: {BRIEF}")
    expect(run(d0, d1, d2, {**knowledge("e4", "Keep the API"), "targets": [LEDGER]}), f"knowledge item cannot target a curator document: {LEDGER}")
    expect(run(d0, d1, d2, {"id": "e4", "disposition": "merge", "targets": [LEDGER], "reason": "x"}), f"legacy merge/replace cannot target a curator document: {LEDGER}")

    # Legacy merge/replace still works for protocol 23, with a warning.
    result = run(d0, d1, d2, {"id": "e4", "disposition": "merge", "targets": [TOPIC], "reason": "Legacy."})
    expect(result)
    assert result["warnings"] == ["legacy disposition merge/replace is deprecated: use a typed disposition"]
    assert result["provenance"]["unmarked_items"] == 1

    # Claim coverage: a missing marker is an error in a curator document and a count elsewhere; markers need a review.
    result = check([d0, d1, d2], docs(e1_tokens=(ids["e0"],)))
    expect(result, f"claim without provenance marker: {LEDGER}")
    expect(check([d0, d1, d2], docs(e1_tokens=(ids["e1"], ids["e4"]))), f"provenance marker without a claim review: {LEDGER}")


def ledger_base_checks() -> None:
    source = "30_Conversations/raw/2026/01/01/a.md"
    gone = "doc:20_Projects/Demo/gone.md"  # a base token is never re-validated, only tokens a batch adds
    old = (
        row("D-001", content="하나", tokens=(OLD, gone)), row("D-002", content="둘", tokens=(OLD,)), row("D-004", content="넷", tokens=(OLD,)),
    )
    ids, check = scenario(
        [raw(source, "s1", ("e0", "user", "We will use SQLite for storage."), ("f", "user", FILLER))],
        ledger("decision_ledger", *old), note("20_Projects/Demo/design notes.md", "# Design\n\n## Open questions\n\nNone."),
        note("10_User/NOTES.md", "Notes."), note("50_Knowledge/other.md", "Other."),
    )
    decision = {"id": "e0", "disposition": "decision", "targets": [LEDGER], "quote": "use SQLite", "reason": "Decided."}
    new = row("D-005", content="SQLite", tokens=(ids["e0"],))
    after = lambda *rows, **metadata: [ledger("decision_ledger", *rows, **metadata)]
    expect(check([decision], after(*old, new)))
    expect(check([decision], after(new, *reversed(old))))
    # Only 상태 and 후속 may change on a base row; nothing is deleted and ids only grow.
    expect(check([decision], after(old[0], old[2], new)), f"ledger row deleted: {LEDGER}: D-002")
    for changed in (
        old[1].replace("| 둘 |", "| 둘? |"), old[1].replace("2026-01-01", "2026-01-02"),
        old[1].replace("| 결정 |", "| 미룸 |"), old[1].replace("| 이유 |", "| 다른 이유 |"),
    ):
        expect(check([decision], after(old[0], changed, old[2], new)), f"ledger row changed outside its mutable columns: {LEDGER}: D-002")
    superseded = row("D-002", "대체됨", content="둘", last="D-005", tokens=(OLD,))
    result = check([decision], after(old[0], superseded, old[2], new))
    expect(result)
    assert result["curation"]["ledger_rows"] == {LEDGER: 4} and result["curation"]["superseded_rows"] == {LEDGER: 1}
    expect(check([decision], after(old[0], row("D-002", "대체됨", content="둘", tokens=(OLD,)), old[2], new)), f"ledger 대체됨 requires 후속: {LEDGER}: D-002")
    expect(check([decision], after(*old, row("D-003", tokens=(ids["e0"],)))), f"ledger id not above the base maximum: {LEDGER}: D-003")
    expect(check([decision], after(*old, row("D-005"))), f"ledger row without provenance: {LEDGER}: D-005", f"claim without provenance marker: {LEDGER}")
    expect(check([decision], after(row("D-001", content="하나", tokens=(OLD,)), old[1], old[2], new)), f"ledger row lost provenance: {LEDGER}: D-001")
    renamed = note(LEDGER, "# 장부\n\n" + "\n".join([HEADERS["decision_ledger"], SEPARATOR, *old, new]), kind="note")
    expect(check([decision], [renamed]), f"curator document kind changed: {LEDGER}")
    misplaced = ledger("decision_ledger", row("D-001", tokens=(ids["e0"],)), path="50_Knowledge/DECISIONS.md")
    expect(check([{**decision, "targets": [misplaced["path"]]}], [misplaced]), f"curator document not allowed here: {misplaced['path']}")

    # Migration: doc:<path>[#<heading>] names a Git-base 20_Projects/10_User document and, if given, one of its headings.
    covered = {"id": "e0", "disposition": "already-covered", "targets": [LEDGER, BRIEF], "reason": "Migrated from the topic document."}

    def sourced(token: str) -> dict[str, Any]:
        page = note(BRIEF, brief_text(**{"유효한 결정": listed("D-005", tokens=(token,))}), kind="brief")
        return check([covered], [*after(*old, row("D-005", tokens=(token,))), page])

    for good in (
        "doc:20_Projects/Demo/design%20notes.md#Open%20questions", "doc:20_Projects/Demo/design%20notes.md#open%20QUESTIONS",
        "doc:20_Projects/Demo/design%20notes.md#Design", "doc:20_Projects/Demo/design%20notes.md", "doc:10_User/NOTES.md",
    ):
        expect(sourced(good))
    for bad in (
        "doc:20_Projects/Demo/missing.md", "doc:20_Projects/Demo/design%20notes.md#Nope", f"doc:{source}",
        "doc:50_Knowledge/other.md", "doc:20_Projects/Demo/design%20notes.md%23Design", f"doc:{LEDGER}",
    ):
        expect(sourced(bad), f"doc source invalid: {LEDGER}: {bad}", f"doc source invalid: {BRIEF}: {bad}")


def user_ledger_checks() -> None:
    names = ("s1", "s2", "s3")
    paths = {name: f"30_Conversations/raw/2026/01/0{index}/{name}.md" for index, name in enumerate(names, 1)}
    sources = [
        raw(paths["s1"], "s1", ("u0", "user", "I always want answers in Korean."), ("f", "user", FILLER)),
        raw(paths["s2"], "s2", ("u1", "user", "Please answer in Korean again.")),
        raw(paths["s3"], "s3", ("u2", "user", "Korean answers please.")),
        raw("30_Conversations/raw/2026/01/04/other-agent.md", "s1", ("u3", "user", "Korean please, same session as the first.")),
        raw("30_Conversations/raw/2026/01/05/helper.md", "s4", ("sa", "subagent", ("Ask the user.", "Korean is preferred."))),
        raw("30_Conversations/raw/2026/01/06/no-session.md", "", ("u4", "user", "My role is data engineer.")),
    ]
    quotes = {"u0": "answers in Korean", "u1": "answer in Korean", "u2": "Korean answers", "u3": "Korean please", "u4": "data engineer"}
    ids, check = scenario(sources)
    pref = lambda locator, disposition="user_preference": {
        "id": locator, "disposition": disposition, "targets": [OBSERVATIONS], "quote": quotes[locator], "reason": "Stated.",
    }
    s1, s2, s3 = (_sess(name) for name in names)
    sources_of = lambda *locators: tuple(ids[locator] for locator in locators)
    observed = lambda *tokens, status="확인", kind="선호(추론)", last: ledger(
        "user_ledger", row("U-001", status, kind=kind, last=last, tokens=tokens),
    )

    # Three independent sessions confirm an inference; the count and the sess: tokens are what the tool computes.
    result = check([pref("u0"), pref("u1"), pref("u2")], [observed(*sources_of("u0", "u1", "u2"), s1, s2, s3, last="3")])
    expect(result)
    assert result["curation"]["ledger_rows"] == {OBSERVATIONS: 1} and result["curation"]["superseded_rows"] == {}
    # Two sessions, one of them seen from two agents (counts once): 가설 passes, 확인 does not.
    twice = [pref("u0"), pref("u1"), pref("u3")]
    expect(check(twice, [observed(*sources_of("u0", "u1", "u3"), s1, s2, status="가설", last="2")]))
    expect(check(twice, [observed(*sources_of("u0", "u1", "u3"), s1, s2, last="2")]), f"ledger 확인 needs 3 independent sessions: {OBSERVATIONS}: U-001")
    differs = f"ledger 독립 세션 differs from the tool count: {OBSERVATIONS}: U-001"
    for locators, tokens, last in ((("u0", "u1", "u2"), (s1, s2, s3), "2"), (("u0", "u1"), (s1, s2, s3), "3"), (("u0", "u1"), (s1,), "2")):
        row_tokens = (*sources_of(*locators), *tokens)
        expect(check([pref(locator) for locator in locators], [observed(*row_tokens, status="가설", last=last)]), differs)
    # An explicit statement needs only a source; a 확인 row with none is refused.
    expect(check([pref("u0")], [observed(*sources_of("u0"), s1, kind="선호(명시)", last="1")]))
    expect(check([pref("u0")], [observed(kind="선호(명시)", last="0")]), f"ledger 확인 needs a source: {OBSERVATIONS}: U-001", f"claim without provenance marker: {OBSERVATIONS}")
    # A source without session_id adds no sess: token (conservative): the count is 0 and the row still stands.
    context = pref("u4", "user_context")
    expect(check([context], [observed(*sources_of("u4"), kind="맥락", last="0")]))
    expect(check([context], [observed(*sources_of("u4"), kind="맥락", last="1")]), differs)
    # An agent memo's session_id is a fresh note id, not the caller's session: three memos add no sess: token.
    memos = [
        raw(f"30_Conversations/raw/2026/01/0{n}/memo{n}.md", f"note_{n}", (f"m{n}", "assistant", f"Remember: answer in Korean ({n}). {FILLER}"), capture_kind="agent_note")
        for n in (1, 2, 3)
    ]
    memo_ids, memo_check = scenario(memos)
    assert not any("session" in node for node in build_compaction_plan(memos, before="2026-02-01")["selected"]["graph"]["nodes"] if node["type"] == "source_file")
    remember = [{"id": f"m{n}", "disposition": "user_preference", "targets": [OBSERVATIONS], "quote": "answer in Korean", "reason": "Stated."} for n in (1, 2, 3)]
    memoed = lambda *tokens, status, last: [ledger("user_ledger", row("U-001", status, kind="선호(추론)", last=last, tokens=(*memo_ids.values(), *tokens)))]
    expect(memo_check(remember, memoed(status="가설", last="0")))
    expect(memo_check(remember, memoed(status="확인", last="0")), f"ledger 확인 needs 3 independent sessions: {OBSERVATIONS}: U-001")
    expect(memo_check(remember, memoed(*(_sess(f"note_{n}") for n in (1, 2, 3)), status="확인", last="3")), differs)
    # Subagent words never count toward the person.
    helper = {"id": "sa#result", "disposition": "user_context", "targets": [OBSERVATIONS], "quote": "Korean is preferred", "reason": "x"}
    expect(
        check([helper], [observed(*sources_of("sa#result"), kind="맥락", last="0")]),
        f"subagent item may only be knowledge, already-covered, discard or hold: {ids['sa#result']}",
    )

    # Counting is cumulative across curations: the base row's sess: tokens ride along and only grow.
    previous = ("item:bbbbbbbbbbbbbbbb", OLD, s1, s2)
    _, check = scenario(sources, observed(*previous, status="가설", last="2"))
    expect(check([pref("u2")], [observed(*previous, *sources_of("u2"), s3, last="3")]))
    expect(check([pref("u3")], [observed(*previous, *sources_of("u3"), status="가설", last="2")]))
    expect(check([pref("u3")], [observed(*previous, *sources_of("u3"), last="2")]), "ledger 확인 needs 3 independent sessions")
    expect(check([pref("u3")], [observed(*previous, *sources_of("u3"), s3, status="가설", last="3")]), differs)
    expect(check([pref("u2")], [observed(OLD, *sources_of("u2"), s3, last="2")]), f"ledger row lost provenance: {OBSERVATIONS}: U-001")


def discovery_checks() -> None:
    first = "30_Conversations/raw/2026/01/01/a.md"
    source = raw(first, "s1", ("e0", "user", "Hello."), ("sa", "subagent", ("Ask the librarian.", "Done quickly.")))
    plan = build_compaction_plan([source, note(TOPIC, "Topic.")], before="2026-02-01")
    selected = plan["selected"]
    nodes = selected["graph"]["nodes"]
    created = {node["path"]: node for node in nodes if node.get("create")}
    assert set(created) == {BRIEF, LEDGER, PROFILE, OBSERVATIONS}, created.keys()
    assert created[BRIEF] == {
        "id": f"create:{BRIEF}", "type": "canonical_candidate", "path": BRIEF, "create": True, "kind": "brief",
        "template": "docs/templates/BRIEF.md",
    }
    assert created[OBSERVATIONS]["template"] == "docs/templates/OBSERVATIONS.md" and created[PROFILE]["kind"] == "user_profile"
    edges = [edge for edge in selected["graph"]["edges"] if edge["type"] == "curator_target"]
    assert sorted(edge["target"] for edge in edges) == sorted(node["id"] for node in created.values()), edges
    assert len({edge["source"] for edge in edges}) == 1
    assert [(entry["kind"], entry["path"], entry["create"], entry["adopt"]) for entry in selected["ledgers"]] == [
        ("brief", BRIEF, True, False), ("decision_ledger", LEDGER, True, False),
        ("user_profile", PROFILE, True, False), ("user_ledger", OBSERVATIONS, True, False),
    ]
    assert selected["ledgers"][0] == {
        "path": BRIEF, "kind": "brief", "create": True, "adopt": False, "template": "docs/templates/BRIEF.md", "chars": 0, "cap": BRIEF_CAP,
    }
    assert selected["ledgers"][1]["next_id"] == "D-001" and selected["ledgers"][3]["rows"] == 0 and selected["ledgers"][3]["next_id"] == "U-001"
    assert build_compaction_plan([source], before="2026-02-01", brief_cap=300)["selected"]["ledgers"][0]["cap"] == 300
    # Raw items carry the event role (both halves of a delegation), never their text; sessions are hashed.
    assert sorted(node["event_role"] for node in nodes if node["type"] == "raw_item") == ["subagent", "subagent", "user"]
    assert selected["subagent_items"] == 2 and not any("body" in node for node in nodes)
    assert "librarian" not in json.dumps(plan) and "Done quickly" not in json.dumps(plan)
    assert next(node for node in nodes if node["type"] == "source_file")["session"] == _sess("s1")
    sessionless = build_compaction_plan([raw(first, "", ("e0", "user", "Hi."))], before="2026-02-01")["selected"]["graph"]["nodes"]
    assert "session" not in next(node for node in sessionless if node["type"] == "source_file")
    unframed = {"document_id": "plain", "path": first, "memory_type": "transcript", "projects": ["Demo"], "document_hash": "a" * 64, "text": "No framing."}
    unframed_nodes = build_compaction_plan([unframed], before="2026-02-01")["selected"]["graph"]["nodes"]
    assert [node.get("event_role") for node in unframed_nodes if node["type"] == "raw_item"] == [None]

    # Existing documents are found by kind, never by name; only PROFILE/OBSERVATIONS are ever added under 10_User.
    ledger_doc, plain = ledger("decision_ledger", row("D-001"), row("D-005"), row("D-012")), note(PROFILE, "Plain.")
    present = [source, note(TOPIC, "Topic."), brief(), ledger_doc, plain, note("10_User/김철수.md", "Detailed record."), note("10_User/WORKING_AGREEMENT.md", "Rules.")]
    selected = build_compaction_plan(present, before="2026-02-01")["selected"]
    by_kind = {entry["kind"]: entry for entry in selected["ledgers"]}
    paths = {node["path"] for node in selected["graph"]["nodes"] if node["type"] != "source_file" and "path" in node and node["type"] != "raw_item"}
    assert not paths & {"10_User/김철수.md", "10_User/WORKING_AGREEMENT.md"} and OBSERVATIONS in paths
    assert by_kind["brief"] == {"path": BRIEF, "kind": "brief", "create": False, "adopt": False, "chars": _brief_chars(brief()["text"]), "cap": BRIEF_CAP}
    assert by_kind["decision_ledger"] == {"path": LEDGER, "kind": "decision_ledger", "create": False, "adopt": False, "rows": 3, "next_id": "D-013"}
    assert by_kind["user_profile"] == {
        "path": PROFILE, "kind": "user_profile", "create": False, "adopt": True, "template": "docs/templates/PROFILE.md",
        "chars": _brief_chars(plain["text"]), "cap": BRIEF_CAP,
    }
    existing = {node["path"]: node for node in selected["graph"]["nodes"] if node["type"] == "canonical_candidate"}
    assert set(existing) >= {BRIEF, LEDGER, PROFILE, TOPIC} and not any("create" in existing[path] for path in (BRIEF, LEDGER, PROFILE, TOPIC))
    named = note("10_User/김철수.md", brief_text("user_profile"), kind="user_profile")
    selected = build_compaction_plan([source, named], before="2026-02-01")["selected"]
    assert [(entry["path"], entry["create"], entry["adopt"]) for entry in selected["ledgers"] if entry["kind"] == "user_profile"] == [(named["path"], False, False)]
    assert PROFILE not in {node.get("path") for node in selected["graph"]["nodes"]}

    # Without a project only the user documents are offered; the project folder follows existing documents, then the label.
    unscoped = build_compaction_plan([raw(first, "s1", ("e0", "user", "Hi."), projects=())], before="2026-02-01")["selected"]
    assert [entry["kind"] for entry in unscoped["ledgers"]] == ["user_profile", "user_ledger"]
    slashed = build_compaction_plan([raw(first, "s1", ("e0", "user", "Hi."), projects=("Foo/Bar",))], before="2026-02-01")["selected"]
    assert slashed["ledgers"][0]["path"] == "20_Projects/Foo-Bar/BRIEF.md"
    alternative = note("20_Projects/Alt/topic.md", "Alt.")
    folders = lambda *docs: [entry["path"] for entry in build_compaction_plan([source, *docs], before="2026-02-01")["selected"]["ledgers"][:2]]
    assert folders(note(TOPIC, "T."), alternative) == ["20_Projects/Alt/BRIEF.md", "20_Projects/Alt/DECISIONS.md"]
    assert folders(note(TOPIC, "T."), alternative, ledger_doc) == [BRIEF, LEDGER]


def template_checks() -> None:
    """The shipped templates are what the tool expects: valid metadata, the right kind, parseable tables, fixed headings."""
    root = Path(__file__).resolve().parents[1]
    paths = {"decision_ledger": LEDGER, "user_ledger": OBSERVATIONS, "brief": BRIEF, "user_profile": PROFILE}

    def read(kind: str) -> str:
        text = (root / CURATOR_KINDS[kind]["template"]).read_text(encoding="utf-8")
        return text.replace("<project-slug>", "demo").replace("<Project>", "Demo")

    def edited(kind: str, old: str, new: str) -> dict[str, Any]:
        assert old in read(kind), (kind, old)
        return document(paths[kind], read(kind).replace(old, new))

    for kind, path in paths.items():
        fresh = document(path, read(kind))
        assert fresh["metadata_valid"] and fresh["kind"] == kind and fresh["memory_type"] == "canonical", (kind, fresh)
        assert (fresh["retrieval_tier"], fresh["review_state"], fresh["temporal_state"]) == ("primary", "human_accepted", "current")
        if "header" in CURATOR_KINDS[kind]:
            assert ledger_entries(fresh["text"], kind, path) == ([], []), kind
        else:
            assert [name for name, _body in brief_sections(fresh["text"])] == list(CURATOR_KINDS[kind]["headings"]), kind
            assert _brief_chars(fresh["text"]) < 1000

    # A first curation that creates all four documents from the templates passes the checks.
    source = "30_Conversations/raw/2026/01/01/a.md"
    ids, check = scenario([raw(source, "s1", ("e0", "user", "We will use SQLite for storage."), ("e1", "user", "I prefer short answers."), ("f", "user", FILLER))])
    decision, preference = ids["e0"], ids["e1"]
    ledger_end, observed_end = "| 내용 | 이유·근거 | 후속 |\n| --- | --- | --- | --- | --- | --- | --- |", "| 근거 | 독립 세션 |\n| --- | --- | --- | --- | --- | --- | --- |"
    table = "| id | 날짜 | 결정 | 이유 |\n| --- | --- | --- | --- |\n"
    created = [
        edited("decision_ledger", ledger_end, f"{ledger_end}\n{row('D-001', tokens=(decision,))}"),
        edited("brief", table, f"{table}| D-001 | 2026-01-01 | SQLite | 단순 {mark(decision)} |\n"),
        edited("user_ledger", observed_end, f"{observed_end}\n{row('U-001', '확인', kind='선호(명시)', last='1', tokens=(preference, _sess('s1')))}"),
        edited("user_profile", "## 확인된 선호\n\n- (없음)", f"## 확인된 선호\n\n- U-001 짧은 답변 {mark(preference)}"),
    ]
    items = [
        {"id": "e0", "disposition": "decision", "targets": [LEDGER, BRIEF], "quote": "use SQLite", "reason": "Decided."},
        {"id": "e1", "disposition": "user_preference", "targets": [OBSERVATIONS, PROFILE], "quote": "short answers", "reason": "Stated."},
    ]
    result = check(items, created)
    expect(result)
    assert result["warnings"] == [] and result["curation"]["ledger_rows"] == {LEDGER: 1, OBSERVATIONS: 1}


def main() -> None:
    pure_analysis_checks()
    conflict_list_checks()
    compaction_graph_checks()
    qdrant_neighbor_checks()
    ledger_parser_checks()
    brief_checks()
    typed_review_checks()
    ledger_base_checks()
    user_ledger_checks()
    discovery_checks()
    template_checks()
    print(json.dumps({"wiki_analysis": "ok", "conflicts": "ok", "compaction_graph": "ok", "curator": "ok"}))


if __name__ == "__main__":
    main()
