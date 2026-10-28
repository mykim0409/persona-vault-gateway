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
from benchmark_search_evidence import CASES, check_case, seed_case


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

    def test_evidence_unit_cases(self):
        for case in CASES:
            settings = replace(self.settings, vault_dir=self.settings.vault_dir / case["id"])
            seed_case(settings, case)
            for limit in (1, 4, 20):
                with self.subTest(case=case["id"], limit=limit), no_network():
                    result = core.search_vault(settings, AGENT, case["query"], limit, bundle="current")
                    self.assertTrue(check_case(case, result), result)

    def test_evidence_blocks_keep_local_context(self):
        self.assertEqual(core.evidence_blocks(""), [])
        self.assertEqual(core.evidence_blocks("# Snapshot101\n\n# FP16"), [])
        blocks = core.evidence_blocks("# Root\n\n## First\n\nAlpha\ncontinued\n\n## Second\n\nBeta")
        self.assertEqual(blocks, ["# Root\n## First\nAlpha\ncontinued", "# Root\n## Second\nBeta"])
        blocks = core.evidence_blocks("# Root\n\nColumn | Status\n--- | ---\nAlpha | pending\nBeta | completed")
        self.assertEqual(blocks, ["# Root\nColumn | Status\nAlpha | pending", "# Root\nColumn | Status\nBeta | completed"])

    def test_fences_do_not_create_headings_or_table_rows(self):
        code = "```text\n# Not a heading\n\n| A | B |\n| --- | --- |\n| C | D |\n```"
        self.assertEqual(core.evidence_blocks("# Root\n\n" + code + "\n\nAfter"), ["# Root\n" + code, "# Root\nAfter"])

    def test_evidence_blocks_strip_provenance_markers(self):
        marker = "<!-- pvg-src: item:0123456789abcdef doc:Snapshot101 -->"
        self.assertEqual(core.evidence_blocks(f"# Root {marker}\n\nFP16 claim line {marker}"), ["# Root \nFP16 claim line"])
        self.note("marked", f"FP16 claim line {marker}")
        self.assertNotIn("pvg-src", self.search("FP16")["results"][0]["snippet"])
        self.assertEqual(self.search("Snapshot101 FP16")["answer_state"]["state"], "abstain")

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

    def test_korean_approval_actor_question_variants(self):
        self.note("approval", "김민수가 ExampleMiner FP16 도입을 최종 승인했다. 홍길동은 실험에 참여했다.")
        for actor, expected in (("홍길동", "abstain"), ("김민수", "supported")):
            questions = (
                f"{actor}은 ExampleMiner FP16 도입을 최종 승인했나?",
                f"{actor}는 ExampleMiner FP16 도입을 최종 승인했나?",
                f"{actor}님께서 ExampleMiner FP16 도입을 최종 승인했나?",
                f"{actor}님이 ExampleMiner FP16 도입을 최종 승인했나?",
                f"ExampleMiner FP16 도입을 최종 승인한 사람은 {actor}인가?",
                f"ExampleMiner FP16 도입을 최종 승인한 사람은 {actor}인가요?",
                f"ExampleMiner FP16 도입을 최종 승인한 사람은 {actor}인지 확인해줘",
                f"ExampleMiner FP16 도입을 최종 승인자는 {actor}입니까?",
            )
            for question in questions:
                for limit in (1, 4, 20):
                    with self.subTest(question=question, limit=limit):
                        self.assertEqual(self.search(question, limit)["answer_state"]["state"], expected)

    def test_approval_actor_must_be_linked_to_the_verb(self):
        for body in (
            "김민수가 ExampleMiner FP16 도입을 최종 승인했고 홍길동은 실험에 참여했다.",
            "홍길동은 실험에 참여했고 김민수가 ExampleMiner FP16 도입을 최종 승인했다.",
        ):
            self.note("approval", body)
            for actor, expected in (("홍길동", "abstain"), ("김민수", "supported")):
                with self.subTest(body=body, actor=actor):
                    result = self.search(f"ExampleMiner FP16 도입을 최종 승인한 사람은 {actor}인가?")
                    self.assertEqual(result["answer_state"]["state"], expected)

    def test_unparsed_or_ambiguous_approval_actor_cannot_pass(self):
        self.note("approval", "김민수가 ExampleMiner FP16 도입을 최종 승인했다. 홍길동은 실험에 참여했다.")
        for question in (
            "ExampleMiner FP16 도입 최종 승인 기록",
            "ExampleMiner FP16 도입 최종 승인: 홍길동?",
            "홍길동(검토자) ExampleMiner FP16 도입을 최종 승인했나?",
            "홍길동이 ExampleMiner FP16 도입을 최종 승인한 사람이 김민수인가?",
        ):
            with self.subTest(question=question):
                result = self.search(question)
                self.assertTrue(result["results"])
                self.assertEqual(result["answer_state"]["state"], "abstain")

    def test_english_attribution(self):
        for body in (
            "Alice approved ProjectX FP16 adoption. Bob ran the trial.",
            "Alice has approved ProjectX FP16 adoption and Bob ran the trial.",
            "ProjectX FP16 adoption was approved by Alice. Bob ran the trial.",
        ):
            with self.subTest(body=body):
                self.note("approval", body)
                self.assertEqual(self.search("Did Bob approve ProjectX FP16 adoption?")["answer_state"]["state"], "abstain")
                self.assertEqual(self.search("Did Alice approve ProjectX FP16 adoption?")["answer_state"]["state"], "supported")

    def test_unparsed_actor_does_not_become_provisional_semantic_evidence(self):
        self.note("approval", "김민수가 ExampleMiner FP16 도입을 최종 승인했다.")
        documents = core.vault_documents(self.settings)
        result = {**documents[0], "match": "semantic"}
        evidence = core.query_evidence("ExampleMiner FP16 도입 최종 승인: 홍길동?", [result], documents)
        self.assertEqual(result["answer_relevance"], "candidate")
        self.assertEqual(core.rag_answer_state([result], "current", evidence)["state"], "abstain")

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

    def test_reported_dns_and_unconfirmed_nfs_do_not_prove_combined_confirmation(self):
        self.note("network", "사용자는 CoverageMiner LAN DNS A 레코드 일곱 개 등록을 보고했다.\n"
                  "NFS 복구 완료와 showmount/mount 성공은 미확인이다. 김민수는 회의에 참여했다.")
        query = "김민수가 CoverageMiner LAN DNS A 레코드 일곱 개와 NFS 복구 완료를 직접 확인했나?"
        for limit in (1, 4, 20):
            with self.subTest(limit=limit):
                result = self.search(query, limit)
                self.assertTrue(result["results"])
                self.assertEqual(result["answer_state"]["state"], "abstain")

    def test_confirmation_binds_actor_and_full_object_instead_of_document_words(self):
        query = "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했나?"
        for body in (
            "이영희가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했다. 김민수는 회의에 참여했다.",
            "이영희가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했고 김민수는 회의에 참여했다.",
            "김민수가 ExampleMiner DNS 등록을 직접 확인했다. NFS 복구 완료는 미확인이다.",
            "김민수가 ExampleMiner DNS 등록을 직접 확인했고 이영희가 NFS 복구 완료를 확인했다.",
            "김민수가 ExampleMiner DNS 등록을 직접 확인했다. 김민수가 OtherProject NFS 복구 완료를 확인했다.",
            "김민수가 ExampleMiner DNS 등록과 NFS 복구 계획을 직접 확인했다. 완료 여부는 미확인이다.",
            "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료 여부를 직접 확인했다.",
            "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료 보고를 전달받았다.",
        ):
            self.note("network", body)
            with self.subTest(body=body):
                self.assertEqual(self.search(query)["answer_state"]["state"], "abstain")

    def test_confirmation_negation_uncertainty_and_plans_are_not_completion(self):
        for ending in (
            "확인하지 않았다", "확인할 예정이다", "확인한 적이 없다", "확인했는지 알 수 없다",
            "확인했다는 근거는 없다", "확인했다고 가정했다", "확인했다고 추정했다",
            "확인했다는 보고는 미확인이다", "확인했다는 주장은 부인됐다",
            "확인했다?", "확인한 것 같다",
        ):
            self.note("network", f"김민수가 ExampleMiner NFS 복구 완료를 직접 {ending}.")
            with self.subTest(ending=ending):
                self.assertEqual(self.search("김민수가 ExampleMiner NFS 복구 완료를 직접 확인했나?")["answer_state"]["state"], "abstain")

    def test_confirmed_actor_and_compound_object_are_supported(self):
        self.note("network", "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했다. 이영희는 회의에 참여했다.")
        for query in (
            "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했나?",
            "김민수는 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인했나?",
            "김민수님께서 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인하셨나?",
            "김민수가 ExampleMiner DNS 등록과 NFS 복구 완료를 직접 검증했나?",
            "ExampleMiner DNS 등록과 NFS 복구 완료를 직접 확인한 사람은 김민수인가?",
        ):
            for limit in (1, 4, 20):
                with self.subTest(query=query, limit=limit):
                    self.assertEqual(self.search(query, limit)["answer_state"]["state"], "supported")
        self.note("network", "김민수가 ExampleMiner DNS 등록을 직접 확인했다. NFS 복구 완료는 미확인이다.")
        self.assertEqual(self.search("김민수가 ExampleMiner DNS 등록을 직접 확인했나?")["answer_state"]["state"], "supported")
        self.assertEqual(self.search("ExampleMiner NFS 복구 상태 확인해줘")["answer_state"]["state"], "supported")

    def test_unparsed_confirmation_actor_and_semantic_candidates_cannot_pass(self):
        self.note("network", "김민수가 ExampleMiner NFS 복구 완료를 직접 확인했다. 이영희는 회의에 참여했다.")
        documents = core.vault_documents(self.settings)
        for query in (
            "이영희는 ExampleMiner NFS 복구 완료를 직접 확인했나?",
            "이영희님께서 ExampleMiner NFS 복구 완료를 직접 확인하셨나?",
            "이영희가 ExampleMiner NFS 복구 완료를 직접 검증했나?",
            "ExampleMiner NFS 복구 완료를 직접 확인한 사람은 이영희인가?",
            "ExampleMiner NFS 복구 완료를 직접 확인했나?",
            "이영희(검토자) ExampleMiner NFS 복구 완료를 직접 확인했나?",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.search(query)["answer_state"]["state"], "abstain")
                result = {**documents[0], "match": "semantic"}
                evidence = core.query_evidence(query, [result], documents)
                self.assertEqual(core.rag_answer_state([result], "current", evidence)["state"], "abstain")

    def test_english_confirmation_attribution_and_scope(self):
        for body, expected in (
            ("Alice personally confirmed ProjectX DNS registration and NFS recovery completion.", "supported"),
            ("Bob personally confirmed ProjectX DNS registration and NFS recovery completion. Alice attended.", "abstain"),
            ("Alice personally confirmed ProjectX DNS registration. NFS recovery completion remains unconfirmed.", "abstain"),
            ("Alice personally confirmed ProjectX DNS registration and NFS recovery completion is pending.", "abstain"),
            ("Alice did not confirm ProjectX DNS registration and NFS recovery completion.", "abstain"),
            ("Alice will confirm ProjectX DNS registration and NFS recovery completion.", "abstain"),
        ):
            self.note("network", body)
            with self.subTest(body=body):
                self.assertEqual(self.search("Did Alice personally confirm ProjectX DNS registration and NFS recovery completion?")["answer_state"]["state"], expected)

    def test_confirmation_keeps_related_conflicts_across_limits(self):
        self.note("network", "김민수가 ExampleMiner NFS 복구 완료를 직접 확인했다.")
        self.note("objection", "Recovery is disputed.", memory_type="candidate", relations={"contradicts": ["network"]})
        for limit in (1, 4, 20):
            self.assertEqual(self.search("김민수가 ExampleMiner NFS 복구 완료를 직접 확인했나?", limit)["answer_state"]["state"], "review_required")

    def test_original_confirmation_question_requires_each_object_qualifier(self):
        query = "김민수가 CoverageMiner LAN DNS A 레코드 일곱 개와 NFS 복구 완료를 직접 확인했나?"
        body = "김민수가 CoverageMiner LAN DNS A 레코드 일곱 개와 NFS 복구 완료를 직접 확인했다."
        for text, expected in (
            (body, "supported"),
            (body.replace("일곱", "다섯"), "abstain"),
            (body.replace("DNS A", "DNS CNAME"), "abstain"),
            (body.replace("직접 ", ""), "abstain"),
            (body.replace("완료", "미완료"), "abstain"),
        ):
            self.note("network", text)
            for limit in (1, 4, 20):
                with self.subTest(text=text, limit=limit):
                    self.assertEqual(self.search(query, limit)["answer_state"]["state"], expected)

    def test_approval_does_not_bypass_a_confirmation_claim(self):
        self.note("approval", "김민수가 ExampleMiner FP16 도입을 승인했다.")
        self.assertEqual(self.search("김민수가 ExampleMiner FP16 도입 승인을 직접 확인했나?")["answer_state"]["state"], "abstain")
        self.note("approval", "김민수가 ExampleMiner FP16 도입 승인을 직접 확인했다.")
        self.assertEqual(self.search("김민수가 ExampleMiner FP16 도입 승인을 직접 확인했나?")["answer_state"]["state"], "supported")

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
