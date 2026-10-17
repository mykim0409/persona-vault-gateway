"""Question relevance and claim safety must not depend on the presentation limit."""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import core
from test_rag_benchmark import AGENT, FakeQdrant, no_network, write_note


class SearchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = core.Settings(root / "vault", root / "gateway.db", "test", embedding_provider="none")
        core.init_db(self.settings.db_path)

    def note(self, name, body, **metadata):
        write_note(
            self.settings.vault_dir, f"20_Projects/{name}.md", body,
            **{"id": name, "memory_type": "canonical", "review_state": "human_accepted",
               "temporal_state": "current", "provenance_mode": "human_asserted", **metadata},
        )

    def search(self, query, limit=4, bundle="current"):
        with no_network():
            return core.search_vault(self.settings, AGENT, query, limit=limit, bundle=bundle)

    def test_unknown_project_abstains(self):
        self.note("actual", "ExampleMiner FP16 comparison", conflict_state="unresolved")
        self.assertEqual(self.search("없는프로젝트QZV981")["answer_state"]["state"], "abstain")

    def test_wrong_actor_does_not_inherit_approval(self):
        self.note("approval", "김민수가 ExampleMiner FP16 도입을 최종 승인했다. 홍길동은 실험에 참여했다.")
        for limit in (1, 4, 20):
            result = self.search("홍길동이 ExampleMiner FP16 도입을 최종 승인했다는 기록", limit)
            self.assertTrue(result["results"], "Keep retrieval candidates, but do not endorse them")
            self.assertEqual(result["answer_state"]["state"], "abstain")
        self.assertEqual(self.search("김민수가 ExampleMiner FP16 도입을 최종 승인했다는 기록")["answer_state"]["state"], "supported")

    def test_trial_proposal_and_negation_are_not_approval(self):
        for body in (
            "홍길동이 ExampleMiner FP16을 시험했다.",
            "홍길동이 ExampleMiner FP16 도입을 승인하지 않았다.",
            "홍길동이 ExampleMiner FP16 도입을 승인했다고 말할 근거가 없다.",
            "홍길동이 ExampleMiner FP16 도입을 승인할 예정이다.",
        ):
            with self.subTest(body=body):
                self.note("trial", body)
                self.assertEqual(self.search("홍길동이 ExampleMiner FP16 도입을 최종 승인했다는 기록")["answer_state"]["state"], "abstain")

    def test_english_attribution(self):
        self.note("approval", "Alice approved ProjectX FP16 adoption. Bob ran the trial.")
        self.assertEqual(self.search("Did Bob approve ProjectX FP16 adoption?")["answer_state"]["state"], "abstain")
        self.assertEqual(self.search("Did Alice approve ProjectX FP16 adoption?")["answer_state"]["state"], "supported")

    def test_approval_of_another_project_or_a_trial_is_not_adoption(self):
        for body in (
            "김민수가 ExampleMiner FP16 도입을 승인했다. 홍길동이 OtherProject FP16 도입을 승인했다.",
            "홍길동이 ExampleMiner FP16 시험을 승인했다. 도입은 미결정이다.",
        ):
            self.note("approval", body)
            self.assertEqual(self.search("홍길동이 ExampleMiner FP16 도입을 최종 승인했다는 기록")["answer_state"]["state"], "abstain")

    def test_unrelated_keyword_conflict_does_not_gate_answer(self):
        self.note("comparison", "ExampleMiner FP16 comparison uses the same snapshot.", subject_id="precision-comparison")
        self.note("cleanup", "ExampleMiner cleanup snapshot " * 80, subject_id="cleanup", conflict_state="unresolved")
        for limit in (1, 4, 20):
            result = self.search("ExampleMiner FP16 comparison snapshot", limit)
            self.assertEqual(result["answer_state"]["state"], "supported")
            self.assertEqual(result["results"][0]["document_id"], "comparison")
            self.assertIn('"answer_relevance": "grounded"', result["context"])

    def test_related_conflict_survives_all_limits_and_reverse_only_relation(self):
        self.note("comparison", "ExampleMiner FP16 comparison uses the same snapshot.")
        self.note("objection", "Do not reuse that comparison.", memory_type="candidate", relations={"contradicts": ["comparison"]})
        for bundle in ("auto", "current"):
            for limit in (1, 4, 20):
                result = self.search("ExampleMiner FP16 comparison snapshot", limit, bundle)
                self.assertEqual(result["answer_state"]["state"], "review_required")
                if limit == 1:
                    self.assertEqual(result["index"]["results_truncated"], 1)
                else:
                    self.assertIn("objection", {item["document_id"] for item in result["results"]})

    def test_semantic_candidates_are_not_automatically_proof(self):
        self.note("rollback", "Rollback the release to the prior build.")
        documents = core.vault_documents(self.settings)
        item = {**documents[0], "match": "semantic"}
        evidence = core.query_evidence("how is deployment undone", [item], documents)
        self.assertEqual(evidence, [])
        self.assertEqual(core.rag_answer_state([item], "current", evidence), {
            "state": "evidence", "reason": "semantic_candidates_require_verification",
        })
        for query in ("없는프로젝트QZV981", "홍길동이 ExampleMiner FP16 도입을 최종 승인했다는 기록"):
            evidence = core.query_evidence(query, [item], documents)
            self.assertEqual(core.rag_answer_state([item], "current", evidence)["state"], "abstain")

    def test_aliases_preserve_paraphrase_support(self):
        self.note("rollback", "Rollback the release to the prior build.", subject_aliases=["undo deployment"])
        self.assertEqual(self.search("undo deployment")["answer_state"]["state"], "supported")

    def test_semantic_retrieval_keeps_candidates_without_overriding_question_guards(self):
        self.note("rollback", "Rollback the release to the prior build.")
        settings = replace(self.settings, embedding_provider="hash")
        vector = [1.0] + [0.0] * (core.HASH_EMBEDDING_DIMENSIONS - 1)
        with patch.object(core, "qdrant_json", FakeQdrant()), \
             patch.object(core, "embed_documents", side_effect=lambda _s, texts: [vector for _ in texts]), \
             patch.object(core, "embed_query", return_value=(vector, core.HASH_EMBEDDING_MODEL)):
            core.index_vault(settings)
            for query, expected in (
                ("how is deployment undone", "evidence"),
                ("없는프로젝트QZV981", "abstain"),
                ("홍길동이 ExampleMiner FP16 도입을 최종 승인했다는 기록", "abstain"),
            ):
                for limit in (1, 4, 20):
                    result = core.search_vault(settings, AGENT, query, limit=limit, bundle="current")
                    self.assertEqual(result["results"][0]["match"], "semantic")
                    self.assertEqual(result["answer_state"]["state"], expected)


if __name__ == "__main__":
    unittest.main()
