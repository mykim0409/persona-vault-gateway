"""Offline evidence-selection ablation; no model downloads, network, or personal Vault."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import core
from test_rag_benchmark import AGENT, no_network, write_note


CASES = [
    {"id": "split-identifiers", "query": "Snapshot101 FP16", "state": "abstain",
     "notes": [("mixed", "Snapshot101 cleanup.\n\nFP16 comparison.", {})]},
    {"id": "same-paragraph", "query": "Snapshot101 FP16", "state": "supported", "first": "match",
     "notes": [("match", "Snapshot101 FP16 comparison uses the same data.", {})]},
    {"id": "heading-context", "query": "Snapshot101 FP16", "state": "supported", "first": "match",
     "notes": [("match", "## Snapshot101\n\nFP16 comparison uses the same data.", {})]},
    {"id": "sibling-heading", "query": "Snapshot101 FP16", "state": "abstain",
     "notes": [("mixed", "## Snapshot101\n\nCleanup is pending.\n\n## Snapshot202\n\nFP16 comparison succeeded.", {})]},
    {"id": "split-table-rows", "query": "Snapshot101 FP16", "state": "abstain",
     "notes": [("table", "| Subject | State |\n| --- | --- |\n| Snapshot101 | cleanup pending |\n| FP16 | comparison succeeded |", {})]},
    {"id": "same-table-row", "query": "Snapshot101 FP16", "state": "supported", "first": "table",
     "notes": [("table", "| Subject | State |\n| --- | --- |\n| Snapshot101 | FP16 comparison succeeded |", {})]},
    {"id": "unrelated-paragraph-conflict", "query": "ExampleMiner FP16 comparison snapshot", "state": "supported", "first": "match",
     "notes": [("match", "ExampleMiner FP16 comparison uses the same snapshot.", {}),
               ("noise", "ExampleMiner cleanup snapshot.\n\nFP16 adapter.\n\nUnrelated comparison.", {"conflict_state": "unresolved"})]},
    {"id": "unrelated-row-conflict", "query": "ExampleMiner FP16 comparison snapshot", "state": "supported", "first": "match",
     "notes": [("match", "ExampleMiner FP16 comparison uses the same snapshot.", {}),
               ("noise", "| Topic | Status |\n| --- | --- |\n| ExampleMiner snapshot | cleanup pending |\n| FP16 | adapter |\n| comparison | unrelated |", {"conflict_state": "unresolved"})]},
    {"id": "real-conflict", "query": "Snapshot101 FP16", "state": "review_required", "first": "match",
     "notes": [("match", "Snapshot101 FP16 comparison succeeded.", {}),
               ("objection", "That comparison has unresolved objections.", {"memory_type": "candidate", "relations": {"contradicts": ["match"]}})]},
    {"id": "late-relevant-block", "query": "ExampleMiner FP16 comparison snapshot", "state": "supported", "first": "match", "snippet": "FP16 comparison",
     "notes": [("match", "ExampleMiner snapshot notes. " + "Unrelated cleanup details. " * 100 + "\n\n## Precision\n\nExampleMiner FP16 comparison uses the same snapshot.", {})]},
    {"id": "ko-correct-actor", "query": "김민수는 ExampleMiner FP16 도입을 승인했나?", "state": "supported", "first": "approval",
     "notes": [("approval", "김민수가 ExampleMiner FP16 도입을 승인했다. 홍길동은 실험에 참여했다.", {})]},
    {"id": "ko-wrong-actor", "query": "홍길동은 ExampleMiner FP16 도입을 승인했나?", "state": "abstain",
     "notes": [("approval", "김민수가 ExampleMiner FP16 도입을 승인했다. 홍길동은 실험에 참여했다.", {})]},
    {"id": "ko-negated", "query": "김민수가 ExampleMiner FP16 도입을 승인했나?", "state": "abstain",
     "notes": [("approval", "김민수가 ExampleMiner FP16 도입을 승인하지 않았다.", {})]},
    {"id": "ko-partial-confirmation", "query": "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했나?", "state": "abstain",
     "notes": [("confirmation", "김민수가 ExampleMiner DNS 등록을 직접 확인했다.\n\nNFS 복구는 아직 미완료다.", {})]},
    {"id": "ko-full-confirmation", "query": "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했나?", "state": "supported", "first": "confirmation",
     "notes": [("confirmation", "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했다.", {})]},
    {"id": "en-correct-actor", "query": "Did Alice approve ProjectX FP16 adoption?", "state": "supported", "first": "approval",
     "notes": [("approval", "ProjectX FP16 adoption was approved by Alice. Bob ran the trial.", {})]},
    {"id": "en-wrong-actor", "query": "Did Bob approve ProjectX FP16 adoption?", "state": "abstain",
     "notes": [("approval", "Alice approved ProjectX FP16 adoption. Bob ran the trial.", {})]},
    {"id": "explicit-alias", "query": "undo deployment", "state": "supported", "first": "rollback",
     "notes": [("rollback", "Rollback the release to the prior build.", {"subject_aliases": ["undo deployment"]})]},
    {"id": "unknown-project", "query": "없는프로젝트QZV981", "state": "abstain",
     "notes": [("noise", "ExampleMiner FP16 cleanup snapshot.", {"conflict_state": "unresolved"})]},
    {"id": "context-not-removed", "query": "ExampleMiner NFS 복구 상태", "state": "supported", "first": "status", "snippet": "미확인",
     "notes": [("status", "ExampleMiner NFS 복구 상태\n완료 여부는 아직 미확인이다.", {})]},
]


def seed_case(settings, case):
    for name, body, metadata in case["notes"]:
        write_note(settings.vault_dir, f"20_Projects/{name}.md", body,
                   **{"id": name, "memory_type": "canonical", "review_state": "human_accepted",
                      "temporal_state": "current", "provenance_mode": "human_asserted", **metadata})


def check_case(case, result):
    return (result["answer_state"]["state"] == case["state"]
            and (not case.get("first") or result["results"][0]["document_id"] == case["first"])
            and (not case.get("snippet") or case["snippet"] in result["results"][0]["snippet"]))


def bm25_matches(settings, terms, limit, bundle="auto", context=None, documents=None):
    """Experimental ranking only. It cannot override eligibility or conflict gates."""
    hits = ORIGINAL_MATCHES(settings, terms, limit, bundle, context, documents)
    terms = sorted({core.evidence_term(term) for term in terms})
    docs = documents if documents is not None else core.analyzed_documents(core.vault_documents(settings))
    units = {
        doc["document_id"]: [core.searchable_text(doc, block).lower() for block in core.evidence_blocks(doc["text"], kind=doc.get("kind", ""))]
        for doc in docs if core.bundle_accepts(doc, bundle)
    }
    texts = [text for blocks in units.values() for text in blocks]
    lengths = {text: max(1, len(core.text_terms(text))) for text in texts}
    average = statistics.mean(lengths[text] for text in texts) if texts else 1
    frequency = {term: sum(bool(core.term_in(term, text)) for text in texts) for term in terms}
    for hit in hits:
        best = 0.0
        for text in units[hit["document_id"]]:
            score = 0.0
            positions = []
            for term in terms:
                count = core.term_in(term, text)
                if count:
                    idf = math.log(1 + (len(texts) - frequency[term] + .5) / (frequency[term] + .5))
                    score += idf * count * 2.2 / (count + 1.2 * (.25 + .75 * lengths[text] / average))
                    positions.append(text.find(term))
            # Tiny tie-break toward co-located terms, not a factual confidence score.
            if len(positions) > 1:
                score += .01 / (1 + max(positions) - min(positions))
            best = max(best, score)
        hit["score"] = best + hit["ranking"]["role"] + hit["ranking"]["applicability"]
    return hits


ORIGINAL_MATCHES = core.keyword_matches


def run(variant, baseline):
    module = core
    if variant == "baseline":
        spec = importlib.util.spec_from_file_location("gateway._baseline", baseline)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    with TemporaryDirectory() as temporary, no_network():
        root = Path(temporary)
        outcomes = []
        for case in CASES:
            settings = module.Settings(root / case["id"] / "vault", root / case["id"] / "db", "bench", embedding_provider="none")
            seed_case(settings, case)
            for limit in (1, 4, 20):
                result = module.search_vault(settings, AGENT, case["query"], limit, bundle="current")
                outcomes.append({"case": case["id"], "limit": limit, "passed": check_case(case, result),
                                 "expected": case["state"], "actual": result["answer_state"]["state"]})
        settings = module.Settings(root / "scale" / "vault", root / "scale" / "db", "bench", embedding_provider="none")
        for index in range(200):
            paragraphs = [f"## Task {task}\n\nProject{index} task {task}. " + "Unrelated maintenance notes and local diagnostics. " * 30 for task in range(18)]
            body = "\n\n".join(paragraphs) + f"\n\n## Recovery\n\nProject{index} NFS recovery uses the rollback procedure."
            write_note(settings.vault_dir, f"20_Projects/project-{index}.md", body, id=f"project-{index}",
                       memory_type="canonical", review_state="human_accepted", temporal_state="current", provenance_mode="human_asserted")
        query = "Project42 NFS recovery rollback procedure"
        for _ in range(2):
            module.search_vault(settings, AGENT, query, 4, bundle="current")
        samples = []
        cpu = time.process_time()
        for _ in range(10):
            start = time.perf_counter()
            result = module.search_vault(settings, AGENT, query, 4, bundle="current")
            samples.append(time.perf_counter() - start)
        cpu = time.process_time() - cpu
        tracemalloc.start()
        module.search_vault(settings, AGENT, query, 4, bundle="current")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result["results"][0]["document_id"] == "project-42"
        print(json.dumps({"variant": variant, "python": platform.python_version(), "arch": platform.machine(),
                          "cases": len(outcomes), "passed": sum(o["passed"] for o in outcomes),
                          "failures": [o for o in outcomes if not o["passed"]],
                          "false_supported": sum(o["actual"] == "supported" and o["expected"] != "supported" for o in outcomes),
                          "expected_supported": sum(o["expected"] == "supported" for o in outcomes),
                          "correct_supported": sum(o["actual"] == "supported" and o["expected"] == "supported" for o in outcomes),
                          "files": 200, "bytes": sum(p.stat().st_size for p in settings.vault_dir.rglob("*.md")),
                          "p50_ms": round(statistics.median(samples) * 1000, 2), "p95_ms": round(max(samples) * 1000, 2),
                          "process_cpu_ms_per_query": round(cpu * 100, 2), "python_peak_alloc_mib": round(peak / 1024**2, 2),
                          "context_chars": len(result["context"]), "response_bytes": len(json.dumps(result, ensure_ascii=False).encode())}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="Snapshot of gateway/core.py before the experiment")
    parser.add_argument("--variant", choices=("baseline", "units", "bm25"))
    args = parser.parse_args()
    if args.variant:
        with patch.object(core, "keyword_matches", bm25_matches if args.variant == "bm25" else ORIGINAL_MATCHES):
            run(args.variant, args.baseline)
    else:
        for variant in ("baseline", "units", "bm25"):
            subprocess.run([sys.executable, __file__, "--baseline", str(args.baseline), "--variant", variant], check=True)
