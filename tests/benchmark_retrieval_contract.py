"""Measure source/quote retrieval and presentation; --check enforces the retrieval-only contract."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import core
from gateway.app import SEARCH_RESPONSE_BYTES, search_response
from test_rag_benchmark import AGENT, FakeQdrant, no_network, write_note


FIXTURE = Path(__file__).parent / "fixtures" / "retrieval-contract.json"
LIMITS = (1, 4, 20)


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def document_path(document):
    return document.get("path", f"20_Projects/{document['id']}.md")


def evidence_for(group, query):
    return query.get("evidence", group.get("evidence", []))


def normalized(text):
    return " ".join(text.split())


def evaluate(group, query, response, limit):
    """Check cited excerpts, not an inferred answer. Gold readings never drive retrieval."""
    retrieval, presentation, verdict = [], [], []
    items = response.get("results")
    context = response.get("context")
    if not isinstance(items, list) or not isinstance(context, str):
        return {"retrieval": ["invalid result envelope"], "presentation": [], "verdict": []}
    by_id = {item.get("document_id"): item for item in items}
    if len(items) > limit or len(by_id) != len(items):
        presentation.append("document count or uniqueness violated")
    if len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()) > SEARCH_RESPONSE_BYTES:
        presentation.append("response byte budget exceeded")
    for document_id in query.get("exclude_ids", []):
        if document_id in by_id:
            retrieval.append(f"excluded source returned: {document_id}")
    expected = evidence_for(group, query)
    extra = set(by_id) - {entry["id"] for entry in expected}
    if "max_extra_results" in query and len(extra) > query["max_extra_results"]:
        retrieval.append("irrelevant source budget exceeded")
    visible = context + "\n" + "\n".join(
        passage.get("snippet", "") for item in items for passage in item.get("passages", [])
    )
    for quote in query.get("forbid_quotes", []):
        if normalized(quote) in normalized(visible):
            retrieval.append(f"irrelevant excerpt returned: {quote}")
    shown = [entry for entry in expected if entry["id"] in by_id]
    if len(shown) < min(limit, len(expected)):
        retrieval.append("required sources missing")
    if len(expected) > limit:
        omitted = response.get("index", {}).get("results_truncated", 0)
        if omitted < len(expected) - len(shown) or "omitted" not in context.lower():
            presentation.append("required sources omitted without a visible notice")
    documents = {document["id"]: document for document in group["documents"]}
    for entry in shown:
        item = by_id[entry["id"]]
        path = document_path(documents[entry["id"]])
        if item.get("path") != path or path not in context:
            retrieval.append(f"source attribution missing: {entry['id']}")
        passages = [normalized(p.get("snippet", "")) for p in item.get("passages", [])]
        for quote in entry["quotes"]:
            if not any(normalized(quote) in passage for passage in passages) or normalized(quote) not in normalized(context):
                retrieval.append(f"required excerpt missing: {entry['id']}: {quote}")
        for key, value in entry.get("metadata", {}).items():
            if item.get(key) != value:
                retrieval.append(f"source metadata changed: {entry['id']}.{key}")
    state = response.get("answer_state", {}).get("state")
    if state in {"supported", "refuted"}:
        verdict.append(f"Gateway adjudicated the question: {state}")
    if response.get("winner") is not None:
        verdict.append("Gateway selected a winner")
    return {"retrieval": retrieval, "presentation": presentation, "verdict": verdict}


def run_fixture(dataset=None):
    dataset = load_fixture() if dataset is None else dataset
    rows = []
    with TemporaryDirectory() as temporary, no_network():
        for group in dataset["groups"]:
            root = Path(temporary) / group["id"]
            semantic = group.get("backend") == "semantic-fixture"
            settings = core.Settings(root / "vault", root / "gateway.db", "contract-test", embedding_provider="hash" if semantic else "none")
            core.init_db(settings.db_path)
            for document in group["documents"]:
                write_note(settings.vault_dir, document_path(document), document["body"], **{
                    "id": document["id"], "memory_type": "canonical", "review_state": "human_accepted",
                    "temporal_state": "current", "provenance_mode": "human_asserted", **document.get("metadata", {}),
                })
            with ExitStack() as stack:
                if semantic:
                    # Inject known candidates, not a semantic-quality score. No model or API is used.
                    vector = [1.0] + [0.0] * (core.HASH_EMBEDDING_DIMENSIONS - 1)
                    stack.enter_context(patch.object(core, "qdrant_json", FakeQdrant()))
                    stack.enter_context(patch.object(core, "embed_documents", side_effect=lambda _s, texts: [vector for _ in texts]))
                    stack.enter_context(patch.object(core, "embed_query", return_value=(vector, core.HASH_EMBEDDING_MODEL)))
                    core.index_vault(settings)
                for query in group["queries"]:
                    view = query.get("view", "current")
                    for limit in LIMITS:
                        result = core.search_vault(settings, AGENT, query["query"], limit, bundle="auto" if view == "all" else view)
                        public = search_response(result, view)
                        issues = evaluate(group, query, public, limit)
                        rows.append({
                            "case": f"{group['id']}/{query['id']}", "category": group["category"], "limit": limit,
                            "backend": "semantic-fixture" if semantic else "keyword", "reading": query["reading"],
                            "legacy_answer_state": public.get("answer_state", {}).get("state"),
                            "issues": issues,
                        })
    counts = {kind: sum(not row["issues"][kind] for row in rows) for kind in ("retrieval", "presentation", "verdict")}
    failures = [row for row in rows if any(row["issues"].values())]
    return {
        "cases": sum(len(group["queries"]) for group in dataset["groups"]), "runs": len(rows), "limits": list(LIMITS),
        "passed": counts, "target_ready": not failures,
        "legacy_supported_against_annotation": sum(
            row["legacy_answer_state"] == "supported" and row["reading"] in {"contradicts", "not_established", "conflicting", "no_source"}
            for row in rows
        ),
        "failures": failures,
        "measurement": "Synthetic source/quote coverage and no-adjudication contract, not entailment scoring or real embedding quality.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 unless every retrieval-only target check passes")
    args = parser.parse_args()
    report = run_fixture()
    print(json.dumps({"mode": "target-gate" if args.check else "report-only", **report}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if args.check and not report["target_ready"] else 0)
