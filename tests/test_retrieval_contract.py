"""Validate the retrieval-only corpus, evaluator mutations, and live offline contract."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import PurePosixPath

from benchmark_retrieval_contract import LIMITS, document_path, evaluate, evidence_for, load_fixture, normalized, run_fixture


def reference_response(group, query, limit=4):
    documents = {doc["id"]: doc for doc in group["documents"]}
    expected = evidence_for(group, query)
    items = [{"document_id": entry["id"], "path": document_path(documents[entry["id"]]),
              "passages": [{"snippet": quote} for quote in entry["quotes"]], **entry.get("metadata", {})}
             for entry in expected[:limit]]
    context = "\n\n".join(item["path"] + "\n" + "\n".join(p["snippet"] for p in item["passages"]) for item in items)
    omitted = max(0, len(expected) - limit)
    if omitted:
        context += f"\n[Search results omitted: {omitted}. Narrow the query for more detail.]"
    return {"results": items, "context": context, "index": {"results_truncated": omitted}}


class RetrievalContractTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load_fixture()
        self.group = self.dataset["groups"][0]
        self.query = self.group["queries"][0]

    def test_corpus_integrity_and_contrast_pairs(self):
        self.assertEqual(self.dataset["schema"], 1)
        group_ids = [group["id"] for group in self.dataset["groups"]]
        self.assertEqual(len(group_ids), len(set(group_ids)))
        for group in self.dataset["groups"]:
            documents = {doc["id"]: doc for doc in group["documents"]}
            self.assertEqual(len(documents), len(group["documents"]))
            paths = [document_path(doc) for doc in group["documents"]]
            self.assertEqual(len(paths), len(set(paths)))
            for path in paths:
                self.assertFalse(PurePosixPath(path).is_absolute())
                self.assertNotIn("..", PurePosixPath(path).parts)
            query_ids = [query["id"] for query in group["queries"]]
            self.assertEqual(len(query_ids), len(set(query_ids)))
            for query in group["queries"]:
                self.assertIn(query["reading"], {"supports", "contradicts", "not_established", "conflicting", "lookup", "no_source"})
                self.assertTrue(query["query"])
                self.assertIn(query.get("view", "current"), {"all", "current", "evidence", "history", "conflicts"})
                expected = evidence_for(group, query)
                self.assertEqual(len(expected), len({entry["id"] for entry in expected}))
                for entry in expected:
                    self.assertIn(entry["id"], documents)
                    self.assertTrue(entry["quotes"])
                    for quote in entry["quotes"]:
                        self.assertTrue(quote)
                        self.assertIn(normalized(quote), normalized(documents[entry["id"]]["body"]))
                self.assertFalse(set(query.get("exclude_ids", [])) & {entry["id"] for entry in expected})
        for name in ("actor-ko", "actor-en", "decimal", "units", "time", "environment", "condition"):
            group = next(g for g in self.dataset["groups"] if g["id"] == name)
            readings = {q["reading"] for q in group["queries"]}
            self.assertIn("supports", readings)
            self.assertTrue(readings & {"contradicts", "not_established"})

    def test_reference_excerpts_pass_without_any_answer_verdict(self):
        for group in self.dataset["groups"]:
            for query in group["queries"]:
                for limit in LIMITS:
                    with self.subTest(group=group["id"], query=query["id"], limit=limit):
                        result = evaluate(group, query, reference_response(group, query, limit), limit)
                        self.assertFalse(any(result.values()), result)

    def test_empty_abstention_cannot_pass_retrieval(self):
        response = {"results": [], "context": "", "answer_state": {"state": "abstain"}}
        result = evaluate(self.group, self.query, response, 4)
        self.assertTrue(result["retrieval"])
        self.assertFalse(result["verdict"])

    def test_positive_and_wrong_actor_queries_both_reject_server_adjudication(self):
        for query in self.group["queries"]:
            response = reference_response(self.group, query)
            response["answer_state"] = {"state": "supported"}
            result = evaluate(self.group, query, response, 4)
            self.assertFalse(result["retrieval"])
            self.assertTrue(result["verdict"])

    def test_passages_must_preserve_qualifiers_and_source_binding(self):
        for mutation in ("drop-quote", "wrong-source", "wrong-path", "context-only", "passages-only"):
            response = reference_response(self.group, self.query)
            if mutation == "drop-quote":
                response["results"][0]["passages"].pop()
            elif mutation == "wrong-source":
                response["results"][0]["document_id"] = "unrelated"
            elif mutation == "wrong-path":
                response["results"][0]["path"] = "20_Projects/unrelated.md"
            elif mutation == "context-only":
                response["results"][0]["passages"] = []
            else:
                response["context"] = ""
            self.assertTrue(evaluate(self.group, self.query, response, 4)["retrieval"], mutation)

    def test_conflict_omission_metadata_and_winner_are_checked_separately(self):
        group = next(g for g in self.dataset["groups"] if g["id"] == "same-claim-conflict")
        query = group["queries"][0]
        response = reference_response(group, query, 1)
        response["index"] = {}
        self.assertTrue(evaluate(group, query, response, 1)["presentation"])
        response = reference_response(group, query)
        response["results"][0]["conflict_state"] = "none"
        self.assertTrue(evaluate(group, query, response, 4)["retrieval"])
        response = reference_response(group, query)
        response["winner"] = "enabled"
        self.assertTrue(evaluate(group, query, response, 4)["verdict"])

    def test_view_authority_count_and_byte_boundaries(self):
        group = next(g for g in self.dataset["groups"] if g["id"] == "raw-authority")
        query = group["queries"][0]
        response = reference_response(group, query)
        response["results"][0]["review_state"] = "human_accepted"
        self.assertTrue(evaluate(group, query, response, 4)["retrieval"])
        self.assertTrue(evaluate(group, group["queries"][1], response, 4)["retrieval"])
        response = reference_response(self.group, self.query)
        response["results"].append(copy.deepcopy(response["results"][0]))
        self.assertTrue(evaluate(self.group, self.query, response, 1)["presentation"])
        response = reference_response(self.group, self.query)
        response["context"] += "x" * 60_000
        self.assertTrue(evaluate(self.group, self.query, response, 4)["presentation"])

    def test_noise_budget_cannot_be_bypassed_by_returning_everything(self):
        group = next(g for g in self.dataset["groups"] if g["id"] == "table-distractors")
        query = group["queries"][0]
        response = reference_response(group, query)
        response["results"].append({"document_id": "noise0", "path": "20_Projects/noise0.md", "passages": []})
        self.assertTrue(evaluate(group, query, response, 20)["retrieval"])
        response = reference_response(group, query)
        response["results"][0]["passages"].append({"snippet": "Orion unrelated housekeeping 0."})
        self.assertTrue(evaluate(group, query, response, 20)["retrieval"])

    def test_runtime_passes_retrieval_only_contract(self):
        report = run_fixture(self.dataset)
        self.assertEqual(report["runs"], report["cases"] * len(LIMITS))
        self.assertTrue(report["target_ready"], report["failures"])
        self.assertTrue(all(0 <= value <= report["runs"] for value in report["passed"].values()))
        print(json.dumps({key: report[key] for key in ("cases", "runs", "passed", "target_ready", "legacy_supported_against_annotation")}))


if __name__ == "__main__":
    unittest.main()
