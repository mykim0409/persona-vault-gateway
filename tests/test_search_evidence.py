"""Question relevance and claim safety must not depend on the presentation limit."""
from __future__ import annotations

import json
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

    def test_multiple_passages_in_one_document_survive_document_limit(self):
        self.note("runbook", "## Decision\n\ndeploy rollback uses the saved build.\n\n"
                  + "unrelated filler " * 300 + "\n\n"
                  "## Failure\n\ndeploy rollback failed with a locked database.\n\n"
                  "## Correction\n\ndeploy rollback requires stopping the writer first.")
        for limit in (1, 4, 20):
            result = self.search("deploy rollback", limit)
            self.assertEqual(len(result["results"]), 1)
            passages = result["results"][0]["passages"]
            self.assertEqual(len(passages), 3)
            self.assertEqual([p["block_index"] for p in passages], [0, 2, 3])
            for section in ("Decision", "Failure", "Correction"):
                self.assertIn(section, result["context"])
            self.assertIn("locked database", result["context"])
            self.assertIn("stopping the writer", result["context"])
            self.assertEqual(result["results"][0]["snippet"], passages[0]["snippet"])

    def test_passage_dedupe_keeps_dates_conditions_and_negation(self):
        self.note("runbook", "## Policy\n\ndeploy rollback is allowed on Linux.\n\n"
                  "deploy rollback is allowed on Linux.\n\n"
                  "deploy rollback is not allowed on Windows.\n\n"
                  "## 2026-10-29\n\ndeploy rollback is allowed on Linux.\n\n"
                  "## 2026-10-30\n\ndeploy rollback is allowed on Linux.")
        result = self.search("deploy rollback")
        passages = result["results"][0]["passages"]
        self.assertEqual(len(passages), 4)
        self.assertIn("not allowed on Windows", result["context"])
        self.assertIn("2026-10-29", result["context"])
        self.assertIn("2026-10-30", result["context"])
        self.assertNotIn("_text", json.dumps(result))
        self.assertNotIn("_coverage", json.dumps(result))

    def test_weaker_passages_are_not_promoted_to_answer_anchors(self):
        self.note("runbook", "deploy rollback during an outage uses the saved build.\n\n"
                  "deploy rollback was also tested on a quiet day.")
        result = self.search("deploy rollback outage")
        self.assertEqual([p["answer_relevance"] for p in result["results"][0]["passages"]], ["grounded", "candidate"])

    def test_passage_budget_does_not_hide_conflict_verdict(self):
        self.note("runbook", "\n\n".join(f"deploy rollback observation {n}." for n in range(200)))
        self.note("objection", "That procedure is disputed.", memory_type="candidate", relations={"contradicts": ["runbook"]})
        for limit in (1, 4, 20):
            with patch.object(core, "SEARCH_PASSAGE_BYTES", 400):
                result = self.search("deploy rollback", limit)
            self.assertEqual(result["answer_state"]["state"], "review_required")
            self.assertGreater(result["index"]["passages_truncated"], 0)
            self.assertIn("omitted", result["context"])
            size = sum(len(json.dumps(p, ensure_ascii=False).encode("utf-8")) for item in result["results"] for p in item["passages"])
            self.assertLessEqual(size, 400)
            if limit > 1:
                self.assertTrue(all(item["passages"] for item in result["results"]))

    def test_passage_budget_counts_exact_exhaustion(self):
        passage = core.search_passage("deploy rollback", [], chunk_index=0)
        size = len(json.dumps({k: v for k, v in passage.items() if k != "_text"} | {"answer_relevance": "candidate"}, ensure_ascii=False).encode("utf-8"))
        items = [{"passages": [passage, core.search_passage("another excerpt", [], chunk_index=1)]}]
        index = {}
        with patch.object(core, "SEARCH_PASSAGE_BYTES", size):
            core.limit_search_passages(items, index)
        self.assertEqual(len(items[0]["passages"]), 1)
        self.assertEqual(index["passages_truncated"], 1)

    def test_semantic_only_results_keep_multiple_chunks_without_claiming_support(self):
        self.note("runbook", "Restore the previous build. " * 80 + "\n\n" + "Redeploy the saved artifact. " * 80)
        settings = replace(self.settings, embedding_provider="hash")
        vector = [1.0] + [0.0] * (core.HASH_EMBEDDING_DIMENSIONS - 1)
        with patch.object(core, "qdrant_json", FakeQdrant()), \
             patch.object(core, "embed_documents", side_effect=lambda _s, texts: [vector for _ in texts]), \
             patch.object(core, "embed_query", return_value=(vector, core.HASH_EMBEDDING_MODEL)):
            core.index_vault(settings)
            result = core.search_vault(settings, AGENT, "how is deployment undone", limit=1, bundle="current")
        self.assertEqual(len(result["results"]), 1)
        self.assertGreater(len(result["results"][0]["passages"]), 1)
        self.assertIn("Restore", result["context"])
        self.assertIn("Redeploy", result["context"])
        self.assertEqual(result["answer_state"]["state"], "evidence")

    def test_containment_cannot_drop_text_hidden_by_snippet_truncation(self):
        text = "deploy rollback " + "padding " * 100 + "Later distinct condition."
        items = [{"passages": [core.search_passage(text, ["deploy"], block_index=0),
                               core.search_passage("Later distinct condition.", [], block_index=1)]}]
        core.limit_search_passages(items, {})
        self.assertEqual(len(items[0]["passages"]), 2)
        self.assertTrue(items[0]["passages"][0]["truncated"])

    def test_wire_response_budget_and_passages_reach_all_clients(self):
        from gateway.app import SEARCH_RESPONSE_BYTES, search_response
        from fastapi.responses import JSONResponse
        for n in range(20):
            self.note(f"runbook{n}", "\n\n".join(f"deploy rollback {i} 단계의 한글 조건입니다. " + "설명 " * 50 for i in range(20)))
        result = self.search("deploy rollback", limit=20)
        public = search_response(result, "current")
        self.assertLessEqual(len(JSONResponse(public).body), SEARCH_RESPONSE_BYTES)
        self.assertTrue(public["results"][0]["passages"])
        self.assertIn("[block ", public["context"])
        self.assertIn("omitted", public["context"])
        self.assertGreater(public["index"]["passages_truncated"], 0)
        # Metadata also counts: pathological titles cannot bypass the Actions wire limit.
        result["results"][0]["title"] = "긴 제목" * SEARCH_RESPONSE_BYTES
        public = search_response(result, "current")
        self.assertLessEqual(len(JSONResponse(public).body), SEARCH_RESPONSE_BYTES)
        self.assertGreater(public["index"]["results_truncated"], 0)
        self.assertEqual(public["answer_state"], result["answer_state"])

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

    def test_list_siblings_cannot_pool_evidence(self):
        for first, second in (("-", "-"), ("*", "*"), ("+", "+"), ("1.", "2."), ("1)", "2)"), ("- [x]", "- [ ]"), ("  -", "  -"), ("\t-", "\t-")):
            for separator in ("\n", "\n\n"):
                self.note("network", f"{first} ExampleMiner DNS is active.{separator}{second} OtherProject NFS is active.")
                for limit in (1, 4, 20):
                    with self.subTest(first=first, separator=separator, limit=limit):
                        result = self.search("ExampleMiner NFS", limit)
                        self.assertEqual(result["answer_state"]["state"], "abstain")
                        self.assertTrue(all(item["answer_relevance"] == "candidate" for item in result["results"]))

    def test_list_item_continuations_and_parent_context(self):
        for body in (
            "- ExampleMiner NFS is active.\n- OtherProject DNS is active.",
            "1. ExampleMiner\n   NFS is active.\n2. OtherProject DNS is active.",
            "- ExampleMiner\nNFS is active.\n- OtherProject DNS is active.",
            "- ExampleMiner:\n\n  NFS is active.\n- OtherProject DNS is active.",
            "- ExampleMiner\n  storage services:\n  - DNS is active.\n  - NFS is active.\n- OtherProject:\n  - DNS is pending.",
            "10. ExampleMiner:\n    1) NFS is active.\n    2) DNS is pending.\n11. OtherProject:\n    1) DNS is active.",
            "- Platform:\n  - ExampleMiner:\n    - NFS is active.\n  - OtherProject:\n    - DNS is active.",
            "- ExampleMiner:\n\n  | Service | Status |\n  | --- | --- |\n  | NFS | active |\n  | DNS | pending |\n- OtherProject DNS is active.",
        ):
            self.note("network", body)
            for limit in (1, 4, 20):
                with self.subTest(body=body, limit=limit):
                    result = self.search("ExampleMiner NFS", limit)
                    self.assertEqual(result["answer_state"]["state"], "supported")
                    self.assertIn("NFS", result["results"][0]["snippet"])
                    self.assertNotIn("OtherProject", result["results"][0]["snippet"])

    def test_list_parent_context_does_not_leak(self):
        for body in (
            "- ExampleMiner:\n  - DNS is active.\n- OtherProject:\n  - NFS is active.",
            "- Platform:\n  - ExampleMiner:\n    - DNS is active.\n  - OtherProject:\n    - NFS is active.",
            "- ExampleMiner:\n  - DNS is active.\n\nOtherProject NFS is active.",
            "- ExampleMiner:\n  - DNS is active.\n\n## OtherProject\n\nNFS is active.",
            "- Services:\n  ## ExampleMiner\n\n  DNS is active.\n- OtherProject NFS is active.",
        ):
            self.note("network", body)
            for limit in (1, 4, 20):
                with self.subTest(body=body, limit=limit):
                    self.assertEqual(self.search("ExampleMiner NFS", limit)["answer_state"]["state"], "abstain")

    def test_list_blocks_preserve_fences_and_markers(self):
        code = "```text\n- ExampleMiner\n1. OtherProject\n```"
        self.assertEqual(core.evidence_blocks(code), [code])
        nested = "- ExampleMiner:\n\n  ```text\n  - DNS\n  - NFS\n  ```\n- OtherProject"
        self.assertEqual(core.evidence_blocks(nested), ["- ExampleMiner:", "- ExampleMiner:\n  ```text\n  - DNS\n  - NFS\n  ```", "- OtherProject"])
        ordered = "10. ExampleMiner:\n\n    ```text\n    - DNS\n    - NFS\n    ```\n11. OtherProject"
        self.assertEqual(core.evidence_blocks(ordered), ["10. ExampleMiner:", "10. ExampleMiner:\n    ```text\n    - DNS\n    - NFS\n    ```", "11. OtherProject"])
        inline = "- ```text\n  - Not a sibling\n  ```\n- OtherProject"
        self.assertEqual(core.evidence_blocks(inline), ["- ```text\n  - Not a sibling\n  ```", "- OtherProject"])
        marker = "<!-- pvg-src: item:0123456789abcdef -->"
        blocks = core.evidence_blocks(f"# Services\n\n- ExampleMiner NFS {marker}\n- OtherProject DNS")
        self.assertEqual(blocks, ["# Services\n- ExampleMiner NFS ", "# Services\n- OtherProject DNS"])

    def test_list_conflicts_remain_claim_scoped(self):
        self.note("network", "- ExampleMiner NFS is active.\n- OtherProject DNS is active.")
        self.note("objection", "- ExampleMiner DNS is active.\n- OtherProject NFS is active.", conflict_state="unresolved")
        for limit in (1, 4, 20):
            self.assertEqual(self.search("ExampleMiner NFS", limit)["answer_state"]["state"], "supported")
        self.note("objection", "- That claim is disputed.", memory_type="candidate", relations={"contradicts": ["network"]})
        for limit in (1, 4, 20):
            self.assertEqual(self.search("ExampleMiner NFS", limit)["answer_state"]["state"], "review_required")

    def test_unknown_project_abstains(self):
        self.note("actual", "ExampleMiner FP16 comparison", conflict_state="unresolved")
        self.assertEqual(self.search("없는프로젝트QZV981")["answer_state"]["state"], "abstain")

    def test_dated_nfs_question_cannot_use_an_unrelated_allowlist_decision(self):
        self.note("decisions", "## 2026-10-20\n\nNFS export allowlist keeps .51. This is not account isolation.")
        self.note("home", "H10: notebook-home has 15 Profile PVCs Bound. "
                  "Notebook 홈 파일 읽기는 미확인이다. NFS export, PV Bound, mount/read are separate checks.")
        query = "2026-10-20 NFS export·PV Bound 확인 뒤 남아 있는 Notebook 홈 mount/read 미확인 범위는?"
        for limit in (1, 4, 20):
            with self.subTest(limit=limit):
                result = self.search(query, limit)
                self.assertTrue(result["results"])
                self.assertEqual(result["answer_state"]["state"], "abstain")
                self.assertFalse(any(item["answer_relevance"] == "grounded" for item in result["results"]))
                result = self.search("H10 Notebook 홈 mount/read", limit)
                self.assertEqual(result["answer_state"]["state"], "supported")
                self.assertEqual(result["results"][0]["document_id"], "home")
        self.note("home", "## 2026-10-20\n\nNFS export, PV Bound, Notebook 홈 mount/read: "
                  "홈 파일 읽기는 미확인이다.")
        for limit in (1, 4, 20):
            result = self.search(query, limit)
            self.assertEqual(result["answer_state"]["state"], "supported")
            self.assertEqual(result["results"][0]["document_id"], "home")
            self.assertIn("미확인", result["results"][0]["snippet"])
        self.note("objection", "The home check is disputed.", memory_type="candidate", relations={"contradicts": ["home"]})
        for limit in (1, 4, 20):
            self.assertEqual(self.search(query, limit)["answer_state"]["state"], "review_required")

    def test_missing_property_cannot_be_grounded_by_shared_project_words(self):
        query = "Team Policy TF meeting minutes cover background color"
        for conflict in ("none", "unresolved"):
            self.note("minutes", "Team Policy TF meeting minutes cover the roadmap and business KPI.", conflict_state=conflict)
            for limit in (1, 4, 20):
                with self.subTest(conflict=conflict, limit=limit):
                    self.assertEqual(self.search(query, limit)["answer_state"]["state"], "abstain")
        self.note("minutes", "Team Policy TF meeting minutes cover background color: blue.")
        self.assertEqual(self.search(query)["answer_state"]["state"], "supported")
        self.note("minutes", "Team Policy TF meeting minutes cover roadmap.\n\nOther committee background color: blue.")
        self.assertEqual(self.search(query)["answer_state"]["state"], "abstain")

    def test_finalization_requires_the_actual_object_and_polarity(self):
        for body in (
            "# ExampleLab\n\ncontrol-plane 서버 A/B 견적 미정. 구매 확정 없음.",
            "# ExampleLab\n\n서버 구매를 확정했다. 양자컴퓨터 구매는 미정이다.",
            "# ExampleLab\n\n양자컴퓨터 구매 확정 여부는 미확인이다.",
            "# ExampleLab\n\n양자컴퓨터 구매를 확정할 예정이다.",
        ):
            self.note("purchase", body)
            for limit in (1, 4, 20):
                with self.subTest(body=body, limit=limit):
                    result = self.search("ExampleLab 양자컴퓨터 구매 확정", limit)
                    self.assertTrue(result["results"])
                    self.assertEqual(result["answer_state"]["state"], "abstain")
            documents = core.vault_documents(self.settings)
            for match in ("keyword", "hybrid", "semantic"):
                item = {**documents[0], "match": match}
                evidence = core.query_evidence("ExampleLab 양자컴퓨터 구매 확정", [item], documents)
                self.assertEqual(core.rag_answer_state([item], "current", evidence)["state"], "abstain")

    def test_finalization_keeps_positive_negative_and_open_status_queries(self):
        for body, query, expected in (
            ("서버 구매 확정.", "서버 구매 확정", "supported"),
            ("서버 구매를 확정했다. 양자컴퓨터 구매는 미정이다.", "서버 구매 확정", "supported"),
            ("서버 구매를 확정하지 않았다.", "서버 구매 확정", "abstain"),
            ("서버 구매 확정 없음.", "서버 구매 확정 없음", "supported"),
            ("서버 구매는 미정이다.", "서버 구매 미정", "supported"),
            ("서버 구매는 미정이다.", "서버 구매 확정 여부", "supported"),
            ("서버 구매는 미정이다.", "양자컴퓨터 구매 확정 여부", "abstain"),
            ("서버 구매는 미정이다.", "양자컴퓨터 구매 미정", "abstain"),
            ("서버 구매 확정.", "서버 구매 미정", "abstain"),
        ):
            self.note("purchase", "# ExampleLab\n\n" + body)
            for limit in (1, 4, 20):
                with self.subTest(body=body, query=query, limit=limit):
                    self.assertEqual(self.search("ExampleLab " + query, limit)["answer_state"]["state"], expected)

    def test_transfer_redirects_are_candidates_not_authoritative_anchors(self):
        header = "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        source = ("# ExampleLab\n\n" + header +
                  "| D-089 | 2026-10-20 | 결정 | 이관됨 | Ownership transferred; follow destinations. | "
                  "204 LAN guard 안전 검증이 아니다. | 20_Projects/ExampleLab/DECISIONS.md#D-057 |")
        self.note("origin", source, kind="decision_ledger", conflict_state="unresolved")
        query = "ExampleLab 204 LAN guard 안전 검증"
        self.assertEqual(self.search(query)["answer_state"]["state"], "abstain")
        self.note("destination", "# ExampleLab\n\n" + header +
                  "| D-057 | 2026-10-20 | 결정 | 유효 | 204 LAN guard 위험을 수용한다. | "
                  "안전 검증이 아니다. <!-- pvg-src: doc:20_Projects/origin.md#D-089 --> | - |",
                  kind="decision_ledger")
        for limit in (1, 4, 20):
            result = self.search(query, limit)
            self.assertEqual(result["answer_state"]["state"], "supported")
            self.assertFalse(any(r["document_id"] == "origin" and r["answer_relevance"] == "grounded" for r in result["results"]))
        self.note("objection", "The destination claim remains disputed.", memory_type="candidate",
                  relations={"contradicts": ["destination"]})
        for limit in (1, 4, 20):
            self.assertEqual(self.search(query, limit)["answer_state"]["state"], "review_required")

    def test_transfer_filter_keeps_live_rows_and_real_conflicts(self):
        header = "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        self.note("ledger", "# ExampleLab\n\n" + header +
                  "| D-089 | 2026-10-20 | 결정 | 이관됨 | Ownership transferred; follow destinations. | 204 LAN guard | 20_Projects/Other/DECISIONS.md#D-057 |\n"
                  "| D-090 | 2026-10-20 | 질문 | 보류 | 1분 strip 정책 | ±30초 merge 미해결 | - |",
                  kind="decision_ledger", conflict_state="unresolved")
        for limit in (1, 4, 20):
            self.assertEqual(self.search("ExampleLab 1분 strip", limit)["answer_state"]["state"], "review_required")

    def test_ledger_conflict_scope_preserves_disputed_claim_without_poisoning_other_decisions(self):
        header = "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        body = ("# ExampleLab\n\n" + header +
                "| D-056 | 2026-10-20 | 결정 | 유효 | Selector output은 고정 1분 strip. | 운영 계약; ±30초 merge는 충돌 미해결. | - |\n"
                "| D-067 | 2026-10-20 | 결정 | 유효 | 4pf queue 시험은 라벨 예산이 아니다. | 1,000h fill cap은 시험값이다. | - |\n"
                "| D-083 | 2026-10-20 | 결정 | 유효 | cycle 실행과 ops 현장 적용은 자동 배포에서 제외한다. | ops는 별도 저장소다. | - |")
        self.note("ledger", body, kind="decision_ledger", conflict_state="unresolved")
        for query, expected in (
            ("ExampleLab selector output 고정 1분 strip 길이 계약", "supported"),
            ("ExampleLab ops 별도 저장소", "supported"),
            ("ExampleLab cycle 실행 ops 현장 적용 자동 배포 제외", "supported"),
            ("ExampleLab 4pf queue 시험 1,000h fill cap 라벨 예산 아님", "supported"),
            ("ExampleLab ±30초 merge 미해결 충돌", "review_required"),
            ("ExampleLab D-056 merge", "review_required"),
            ("ExampleLab D-056", "review_required"),
        ):
            for limit in (1, 4, 20):
                with self.subTest(query=query, limit=limit):
                    result = self.search(query, limit)
                    self.assertEqual(result["answer_state"]["state"], expected)
                    self.assertEqual(result["results"][0]["conflict_state"], "unresolved")
                    self.assertEqual(result["results"][0]["answer_conflict_state"], "none" if expected == "supported" else "unresolved")
                    if expected == "supported":
                        self.assertFalse(result["groups"][0]["warnings"])
        self.note("objection", "The fixed strip contract is disputed.", memory_type="candidate",
                  relations={"contradicts": ["ledger"]})
        for limit in (1, 4, 20):
            self.assertEqual(self.search("ExampleLab selector output 고정 1분 strip", limit)["answer_state"]["state"], "review_required")

    def test_unlocalized_or_unstructured_conflict_cannot_be_cleared(self):
        header = "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        for body, kind in (
            ("ExampleLab selector 1분 strip", "procedure"),
            ("ExampleLab selector 1분 strip", "decision_ledger"),
            (header + "| D-001 | 2026-10-20 | 결정 | 유효 | ExampleLab selector 1분 strip | No local scope. | - |", "decision_ledger"),
        ):
            self.note("unknown", body, kind=kind, conflict_state="unresolved")
            self.assertEqual(self.search("ExampleLab selector 1분 strip")["answer_state"]["state"], "review_required")

    def test_ledger_scope_retains_same_subject_conflicts_with_different_numbers(self):
        from gateway.app import search_response

        header = "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        body = ("# ExampleLab\n\n" + header +
                "| D-001 | 2026-10-20 | 결정 | 유효 | Selector output은 고정 1분 strip. | 사용자 계약. | - |\n"
                "| D-002 | 2026-10-20 | 질문 | 보류 | Selector output은 2분 strip이라는 반대 주장. | strip 길이 충돌 미해결. | - |")
        self.note("ledger", body, kind="decision_ledger", conflict_state="unresolved")
        for limit in (1, 4, 20):
            result = self.search("ExampleLab selector output 고정 1분 strip", limit)
            self.assertEqual(result["answer_state"]["state"], "review_required")
            public = search_response(result, "current")
            self.assertEqual(public["results"][0]["answer_conflict_state"], "unresolved")
            self.assertIn('"answer_conflict_state": "unresolved"', public["context"])
        self.note("ledger", body.replace("strip 길이 충돌 미해결", "다른 주제의 충돌 미해결"),
                  kind="decision_ledger", conflict_state="unresolved", subject_id="strip-contract")
        self.assertEqual(self.search("ExampleLab selector output 고정 1분 strip")["answer_state"]["state"], "review_required")

    def test_ledger_anaphoric_disputes_and_tied_pending_rows_remain_gated(self):
        header = "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        for reason in ("This remains unresolved.", "해당 결정은 충돌 미해결."):
            self.note("ledger", "# ExampleLab\n\n" + header +
                      f"| D-001 | 2026-10-20 | 결정 | 유효 | selector 1분 strip | {reason} | - |",
                      kind="decision_ledger", conflict_state="unresolved")
            self.assertEqual(self.search("ExampleLab selector 1분 strip")["answer_state"]["state"], "review_required")
        self.note("ledger", "# ExampleLab\n\n" + header +
                  "| D-001 | 2026-10-20 | 결정 | 유효 | selector 1분 strip | 사용자 결정 | - |\n"
                  "| D-002 | 2026-10-20 | 질문 | 보류 | selector 1분 strip | 충돌 미해결 | - |",
                  kind="decision_ledger", conflict_state="unresolved")
        for limit in (1, 4, 20):
            self.assertEqual(self.search("ExampleLab selector 1분 strip", limit)["answer_state"]["state"], "review_required")

    def test_transfer_redirect_stays_non_authoritative_in_hybrid_search(self):
        header = "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        self.note("origin", "# ExampleLab\n\n" + header +
                  "| D-001 | 2026-10-20 | 결정 | 이관됨 | Ownership transferred; follow destinations. | 204 LAN guard | 20_Projects/Other/DECISIONS.md#D-002 |",
                  kind="decision_ledger", conflict_state="unresolved")
        self.note("destination", "ExampleLab 204 LAN guard 위험을 수용했지만 안전 검증은 아니다.")
        settings = replace(self.settings, embedding_provider="hash")
        vector = [1.0] + [0.0] * (core.HASH_EMBEDDING_DIMENSIONS - 1)
        with patch.object(core, "qdrant_json", FakeQdrant()), \
             patch.object(core, "embed_documents", side_effect=lambda _s, texts: [vector for _ in texts]), \
             patch.object(core, "embed_query", return_value=(vector, core.HASH_EMBEDDING_MODEL)):
            core.index_vault(settings)
            for limit in (1, 4, 20):
                result = core.search_vault(settings, AGENT, "ExampleLab 204 LAN guard", limit, bundle="current")
                self.assertEqual(result["answer_state"]["state"], "supported")
                self.assertFalse(any(r["document_id"] == "origin" and r["answer_relevance"] == "grounded" for r in result["results"]))

    def test_property_queries_preserve_known_unknowns_and_reject_semantic_guessing(self):
        for question in (
            "Team Policy TF meeting minutes include background color",
            "Team Policy TF meeting minutes describe background color",
            "What is the background color of Team Policy TF meeting minutes?",
        ):
            self.note("minutes", "Team Policy TF meeting minutes: background color is unspecified.")
            self.assertEqual(self.search(question)["answer_state"]["state"], "supported")
            self.note("minutes", "Team Policy TF meeting minutes discuss roadmap.")
            documents = core.vault_documents(self.settings)
            for match in ("keyword", "hybrid", "semantic"):
                with self.subTest(question=question, match=match):
                    item = {**documents[0], "match": match}
                    evidence = core.query_evidence(question, [item], documents)
                    self.assertEqual(core.rag_answer_state([item], "current", evidence)["state"], "abstain")

    def test_topic_lookup_still_allows_cross_language_keywords_and_current_conflicts(self):
        self.note("ledger", "# ExampleLab\n\n| id | 날짜 | 결정 |\n| --- | --- | --- |\n"
                  "| D-041 | 2026-10-20 | 브라우저 로그인과 검색 오류 수정을 별개로 다룬다. |", kind="decision_ledger")
        self.assertEqual(self.search("ExampleLab D-041 browser login separate search bug")["answer_state"]["state"], "supported")
        self.note("status", "## 2026-10-20\n\nH10 Model 현황")
        self.note("disputed", "## 2026-10-20\n\nH10 현황 상태", conflict_state="unresolved")
        for limit in (1, 4, 20):
            self.assertEqual(self.search("2026-10-20 H10 Model 현황 상태", limit)["answer_state"]["state"], "review_required")

    def test_adoption_requires_the_organization_action_and_complete_object(self):
        query = "Team Policy TF officially adopted the LMFM roadmap and business KPI from this meeting"
        for body, expected in (
            ("Team Policy TF meeting notes: the LMFM roadmap and business KPI are an assistant draft, not an adopted organizational decision.", "abstain"),
            ("Team Policy TF officially adopted the LMFM roadmap and business KPI from this meeting.", "supported"),
            ("Team Policy TF has officially adopted the LMFM roadmap and business KPI from this meeting.", "supported"),
            ("The LMFM roadmap and business KPI from this meeting were officially adopted by Team Policy TF.", "supported"),
            ("Other Policy TF officially adopted the LMFM roadmap and business KPI from this meeting. Team Policy TF attended.", "abstain"),
            ("Team Policy TF has not officially adopted the LMFM roadmap and business KPI from this meeting.", "abstain"),
            ("Team Policy TF officially adopted the LMFM roadmap from this meeting. Business KPI remains a draft.", "abstain"),
            ("Team Policy TF officially adopted the LMFM roadmap from this meeting and Other Policy TF adopted the business KPI.", "abstain"),
            ("Team Policy TF proposed the LMFM roadmap and business KPI from this meeting for official adoption.", "abstain"),
            ("Team Policy TF officially adopted the LMFM roadmap and business KPI from this meeting?", "abstain"),
            ("Whether Team Policy TF officially adopted the LMFM roadmap and business KPI from this meeting is unknown.", "abstain"),
        ):
            self.note("minutes", body)
            for limit in (1, 4, 20):
                with self.subTest(body=body, limit=limit):
                    result = self.search(query, limit)
                    self.assertTrue(result["results"])
                    self.assertEqual(result["answer_state"]["state"], expected)
            if expected == "abstain":
                documents = core.vault_documents(self.settings)
                item = {**documents[0], "match": "semantic"}
                evidence = core.query_evidence(query, [item], documents)
                self.assertEqual(core.rag_answer_state([item], "current", evidence)["state"], "abstain")

    def decision_note(self, reason="사용자가 정확히 이 명령을 무시하라고 재확인했다.", date="2026-10-20"):
        self.note(
            "decisions",
            "# ExampleLab 결정 장부\n\n"
            "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            f"| D-009 | {date} | 결정 | 유효 | 204의 `/data`에는 제안된 `sudo chmod +t /data`를 하지 않고 "
            "기존 `root:sharegroup` mode 777로 진행한다. | " + reason +
            " 여러 사용자가 속한 공유 그룹이며, sticky bit가 없는 상위 디렉터리의 교체·이름변경 위험을 "
            "수용한 것이지 안전성 검증은 아니다. | - |\n"
            "| D-010 | 2026-10-21 | 결정 | 유효 | 205 mode 755. | 김민수가 다른 설정을 선택했다. | - |",
            kind="decision_ledger",
        )

    def test_decision_dates_and_particles_preserve_actor_constraints(self):
        self.decision_note()
        for query, expected in (
            ("2026-10-20 ExampleLab D-009 사용자는 204 /data의 chmod +t를 제외하고 기존 mode 777로 진행하기로 결정했나?", "supported"),
            ("10월 20일 사용자가 204 /data의 sticky bit 변경을 제외하고 777 권한을 유지하기로 한 결정은?", "supported"),
            ("2026년 10월 20일 사용자가 ExampleLab D-009 204 /data mode 777로 유지하기로 한 결정은?", "supported"),
            ("2026-10-20 김민수가 ExampleLab 204 /data의 chmod +t를 제외하고 기존 mode 777로 진행하기로 직접 결정했나?", "abstain"),
            ("ExampleLab D-009 204 /data chmod +t mode 777 결정 내용", "supported"),
            ("2026-10-20 ExampleLab D-009 204 /data mode 777로 진행하기로 결정했나?", "supported"),
            ("ExampleLab D-009 사용자의 결정 내용은?", "supported"),
            ("2026-10-20 ExampleLab D-009 사용자는 204 /data의 chmod +u를 제외하고 mode 777로 진행하기로 결정했나?", "abstain"),
            ("2026-10-20 ExampleLab D-009 사용자는 chmod +u 제외 후 mode 777로 진행하기로 결정했나?", "abstain"),
            ("2026-10-20 ExampleLab D-009 사용자는 chmod -t 제외 후 mode 777로 진행하기로 결정했나?", "abstain"),
            ("2026-10-20 ExampleLab D-009 사용자는 204 /data mode 755로 진행하기로 결정했나?", "abstain"),
            ("2026-10-20 ExampleLab D-009 사용자는 204 /data mode 7770으로 진행하기로 결정했나?", "abstain"),
            ("2026-10-21 ExampleLab D-009 사용자는 mode 777로 진행하기로 결정했나?", "abstain"),
            ("10월 21일 사용자가 ExampleLab D-009 mode 777로 진행하기로 한 결정은?", "abstain"),
            ("2025년 10월 20일 사용자가 ExampleLab D-009 mode 777로 진행하기로 한 결정은?", "abstain"),
        ):
            for limit in (1, 4, 20):
                with self.subTest(query=query, limit=limit):
                    result = self.search(query, limit)
                    self.assertTrue(result["results"])
                    self.assertEqual(result["answer_state"]["state"], expected)
                    if expected == "supported":
                        self.assertIn("D-009", result["results"][0]["snippet"])
                        self.assertNotIn("D-010", result["results"][0]["snippet"])

    def test_decision_attribution_is_not_participation_or_negated_choice(self):
        query = "2026-10-20 ExampleLab D-009 사용자는 204 /data mode 777로 진행하기로 결정했나?"
        for reason, expected in (
            ("사용자가 정확히 이 명령을 무시하라고 재확인했다.", "supported"),
            ("사용자가 이 설정을 선택했다. 김민수는 회의에 참여했다.", "supported"),
            ("김민수가 이 설정을 선택했다. 사용자는 회의에 참여했다.", "abstain"),
            ("김민수가 이 설정을 선택했고 사용자는 회의에 참여했다.", "abstain"),
            ("사용자가 이 설정을 결정하지 않았다.", "abstain"),
            ("사용자가 이 설정을 결정했다는 근거가 없다.", "abstain"),
            ("사용자가 이 설정을 결정할 예정이다.", "abstain"),
            ("사용자가 이 설정을 안 결정했다.", "abstain"),
            ("사용자가 이 설정을 결정했다?", "abstain"),
        ):
            self.decision_note(reason)
            for limit in (1, 4, 20):
                with self.subTest(reason=reason, limit=limit):
                    self.assertEqual(self.search(query, limit)["answer_state"]["state"], expected)

    def test_calendar_notation_keeps_explicit_year(self):
        for date in ("2026-10-20", "2026년 10월 20일", "2026년 10월20일"):
            self.decision_note(date=date)
            for query in ("2026-10-20 ExampleLab D-009 mode 777", "10월20일 ExampleLab D-009 mode 777"):
                with self.subTest(date=date, query=query):
                    self.assertEqual(self.search(query)["answer_state"]["state"], "supported")
        self.note("decisions", "2025-10-20 ExampleLab mode 777")
        self.assertEqual(self.search("2026-10-20 ExampleLab mode 777")["answer_state"]["state"], "abstain")

    def test_short_technical_terms_keep_particle_free_identity(self):
        for term, expected in (("t를", "t"), ("777로", "777"), ("NFS를", "NFS"), ("사용자는", "사용자"), ("이", "이")):
            with self.subTest(term=term):
                self.assertEqual(core.evidence_term(term), expected)

    def test_calendar_normalization_does_not_move_dates_between_actions(self):
        self.note("approval", "2026-10-21 김민수가 ExampleMiner FP16 도입을 승인했다. 2026-10-20 홍길동은 회의에 참여했다.")
        self.assertEqual(self.search("2026-10-20 김민수가 ExampleMiner FP16 도입을 승인했나?")["answer_state"]["state"], "abstain")
        self.note("approval", "2026년 10월 20일 김민수가 ExampleMiner FP16 도입을 승인했다.")
        self.assertEqual(self.search("2026-10-20 김민수가 ExampleMiner FP16 도입을 승인했나?")["answer_state"]["state"], "supported")

    def test_invalid_calendar_dates_are_not_normalized(self):
        for date in ("2026-02-29", "2026년 13월 20일", "0000-10-20", "10월 32일"):
            with self.subTest(date=date):
                self.assertEqual(core.normalize_evidence_dates(date), date)
        self.assertEqual(core.normalize_evidence_dates("02월 29일"), "02-29")

    def test_decision_constraints_also_reject_semantic_only_candidates(self):
        self.decision_note()
        documents = core.vault_documents(self.settings)
        for query in (
            "2026-10-20 김민수가 ExampleLab D-009 mode 777로 진행하기로 결정했나?",
            "2025-10-20 사용자가 ExampleLab D-009 mode 777로 진행하기로 결정했나?",
            "2026-10-20 사용자가 ExampleLab D-009 mode 755로 진행하기로 결정했나?",
        ):
            with self.subTest(query=query):
                item = {**documents[0], "match": "semantic"}
                evidence = core.query_evidence(query, [item], documents)
                self.assertEqual(core.rag_answer_state([item], "current", evidence)["state"], "abstain")

    def test_plain_decisions_keep_their_own_actor_and_object(self):
        for body, query, expected in (
            ("사용자가 ExampleLab mode 777을 유지하기로 결정했다. 김민수는 회의에 참여했다.", "사용자는 ExampleLab mode 777을 유지하기로 결정했나?", "supported"),
            ("김민수가 ExampleLab mode 777을 유지하기로 결정했다. 사용자는 회의에 참여했다.", "사용자는 ExampleLab mode 777을 유지하기로 결정했나?", "abstain"),
            ("사용자가 ExampleLab mode 755를 선택했다. 사용자가 OtherProject mode 777을 선택했다.", "사용자는 ExampleLab mode 777을 선택하기로 결정했나?", "abstain"),
            ("Alice decided to keep ProjectX mode 777. Bob attended.", "Did Alice decide to keep ProjectX mode 777?", "supported"),
            ("Alice decided to keep ProjectX mode 777. Bob attended.", "Did Bob decide to keep ProjectX mode 777?", "abstain"),
            ("Alice had not decided to keep ProjectX mode 777.", "Did Alice decide to keep ProjectX mode 777?", "abstain"),
        ):
            self.note("decisions", body)
            with self.subTest(body=body, query=query):
                self.assertEqual(self.search(query)["answer_state"]["state"], expected)

    def test_decision_scope_subject_is_not_a_person(self):
        self.note(
            "scope", "# ExampleLab 결정 장부\n\n"
            "| id | 날짜 | 유형 | 내용 | 근거 |\n| --- | --- | --- | --- | --- |\n"
            "| D-016 | 2026-10-21 | 결정 | Google 표시 이름을 key로 쓰지 않는다. "
            "profile scope 제거까지 뜻하지는 않는다. edge/Argo scope 제거를 되돌린다. | "
            "사용자가 name 식별자 사용에 대한 범위를 교정했다. |",
            kind="decision_ledger",
        )
        for query, expected in (
            ("2026-10-21 ExampleLab D-016 Google 표시 이름을 key로 쓰지 않는다는 결정이 profile scope 제거까지 뜻했나?", "supported"),
            ("2026-10-21 ExampleLab D-016 Google 표시 이름을 key로 쓰지 않는다는 결정은 profile scope 제거까지 뜻했나?", "supported"),
            ("2026-10-21 ExampleLab D-016 김민수의 결정이 profile scope 제거까지 뜻했나?", "abstain"),
            ("2026-10-21 ExampleLab D-016 김민수가 Google 표시 이름을 key로 쓰지 않기로 결정했나?", "abstain"),
            ("2026-10-21 ExampleLab D-999 Google 표시 이름을 key로 쓰지 않는다는 결정이 profile scope 제거까지 뜻했나?", "abstain"),
        ):
            for limit in (1, 4, 20):
                with self.subTest(query=query, limit=limit):
                    result = self.search(query, limit)
                    self.assertEqual(result["answer_state"]["state"], expected)
                    if expected == "supported":
                        self.assertIn("profile scope 제거까지 뜻하지는 않는다", result["results"][0]["snippet"])

    def test_field_units_and_totals_are_not_inferred(self):
        self.note(
            "metrics", "# ExampleLab\n\n## 2026-10-21\n\n"
            "gpusystem-40 DCGM liveness를 initialDelaySeconds: 180으로 변경했다. "
            "40/50/52의 GPU metric은 10/2/2이며 up==0은 빈 배열이었다.",
        )
        for query, expected in (
            ("2026-10-21 ExampleLab gpusystem-40 DCGM liveness 180초 패치 뒤 14 GPU metric과 up==0 결과는?", "abstain"),
            ("2026-10-21 ExampleLab DCGM initialDelaySeconds 180 뒤 14 GPU metric 결과", "abstain"),
            ("2026-10-21 ExampleLab DCGM liveness 180초 뒤 GPU metric 10/2/2 결과", "abstain"),
            ("2026-10-21 ExampleLab gpusystem-40 DCGM initialDelaySeconds 180 GPU metric 40/50/52=10/2/2 up==0 결과", "supported"),
            ("2026-10-21 ExampleLab DCGM initialDelaySeconds 181 GPU metric 10/2/2 결과", "abstain"),
        ):
            for limit in (1, 4, 20):
                with self.subTest(query=query, limit=limit):
                    self.assertEqual(self.search(query, limit)["answer_state"]["state"], expected)

    def test_numeric_values_are_not_replaced_by_matching_decimal_tails(self):
        for source, query_value in (
            ("6.5배", "5.5배"), ("5.5배", "5배"), ("15.5배", "5.5배"),
            (".715", ".620"), (".620", "620"), ("1.620", ".620"),
            ("-5.5배", "5.5배"), ("5.5배", "-5.5배"),
            ("6개", "5개"), ("6", "5"), ("2,000개", "1,000개"),
            ("FP32", "FP16"), ("FP160", "FP16"), ("D-107", "D-106"), ("1.2.3", "1.2.4"),
            ("5.5배", "5.5%"), ("5.5", "5.5배"),
            ("0.123456789012345678901234567891", "0.123456789012345678901234567892"),
        ):
            self.note("metrics", f"ExampleModel 측정 결과는 {source}였다.")
            query = f"ExampleModel 측정 결과 {query_value}"
            for limit in (1, 4, 20):
                with self.subTest(source=source, query=query, limit=limit):
                    self.assertEqual(self.search(query, limit)["answer_state"]["state"], "abstain")
            documents = core.vault_documents(self.settings)
            candidate = {**documents[0], "match": "semantic"}
            evidence = core.query_evidence(query, [candidate], documents)
            self.assertEqual(core.rag_answer_state([candidate], "current", evidence)["state"], "abstain")

    def test_numeric_surface_variants_preserve_exact_value_and_unit(self):
        for source, query_value in (
            (".620", ".620보다"), (".715였다", ".715이며"), ("5.5배로", "5.5배만"),
            ("0.620", ".620"), ("5.50배", "5.5배"), ("1,000개", "1000개"),
            ("180초", "180초만"), ("5.5배로", "5.5"), ("5.5%", "5.5%"),
        ):
            self.note("metrics", f"ExampleModel 측정 결과는 {source} 기록이다.")
            for limit in (1, 4, 20):
                with self.subTest(source=source, query_value=query_value, limit=limit):
                    self.assertEqual(self.search(f"ExampleModel 측정 결과 {query_value}", limit)["answer_state"]["state"], "supported")
        self.note("metrics", "ExampleModel 측정 결과는 5.5% 기록이다.")
        self.assertEqual(self.search("ExampleModel 측정 결과 5.5배")["answer_state"]["state"], "abstain")

    def test_numeric_suffixes_do_not_drop_units_or_korean_magnitudes(self):
        for source, query_value, expected in (
            ("0건이나", "0건", "supported"), ("2028년까지", "2028년", "supported"),
            ("1만", "1", "abstain"), ("1만명", "1명", "abstain"),
            ("2억4천만", "2억4천", "abstain"), ("2억4천만", "2억4천만", "supported"),
            ("40", "gpusystem-40", "abstain"), ("s3-timing", "S3", "abstain"),
        ):
            self.note("metrics", f"ExampleModel 측정 결과: `{source}`.")
            for limit in (1, 4, 20):
                with self.subTest(source=source, query=query_value, limit=limit):
                    self.assertEqual(self.search(f"ExampleModel 측정 결과 {query_value}", limit)["answer_state"]["state"], expected)

    def test_suffixes_keep_ledger_refutation_and_roadmap_conflicts(self):
        self.note(
            "decisions", "# ExampleLab\n\n"
            "| id | 날짜 | 내용 |\n| --- | --- | --- |\n"
            "| D-018 | 2026-10-21 | generalize branch leak-check 45/45 통과는 site 값 0건이나 "
            "Bitbucket push 완료를 뜻하지 않는다. 실제 site 값은 2,814/1,836건이다. |",
            kind="decision_ledger",
        )
        self.note(
            "roadmap", "# ExampleForms\n\n"
            "assistant 회의록 초안의 로드맵은 2026년 Level 1, 다음 해 Level 2–3, 2028년까지 Level 4–5다. "
            "KPI는 개발·배포 시간과 인건비다. GPU 서버 2대 약 2억4천만 원 재견적은 조직 채택이 아닌 제안이다.",
            conflict_state="unresolved",
        )
        for query, expected, snippet in (
            ("2026-10-21 ExampleLab D-018 generalize branch leak-check 45/45 통과가 site 값 0건과 Bitbucket push 완료를 뜻했나?",
             "supported", "뜻하지 않는다"),
            ("ExampleForms assistant 회의록 초안의 Level 1 2026년 Level 2~3 다음 해 Level 4~5 2028년 KPI 개발 배포 시간 인건비 GPU 서버 2대 약 2억4천만 원 재견적",
             "review_required", "조직 채택이 아닌 제안"),
        ):
            for limit in (1, 4, 20):
                with self.subTest(query=query, limit=limit):
                    result = self.search(query, limit)
                    self.assertEqual(result["answer_state"]["state"], expected)
                    self.assertIn(snippet, result["results"][0]["snippet"])

    def test_presentation_numeric_suffixes_keep_document_conflict_gate(self):
        self.note(
            "presentation", "# ExampleModel\n\n## 2026-10-22\n\n"
            "| 비교 | Random F1 | Model F1 | 속도 |\n| --- | --- | --- | --- |\n"
            "| 임시 시험 | .620 | .715였다 | 5.5배로 측정 |",
            conflict_state="unresolved",
        )
        for query, expected in (
            ("2026-10-22 ExampleModel Random F1 .620보다 Model F1 .715이며 속도 5.5배만 기록됐나?", "review_required"),
            ("2026-10-22 ExampleModel Random F1 .620, Model F1 .715, 속도 5.5배", "review_required"),
            ("2026-10-22 ExampleModel Random F1 .620보다 Model F1 .715이며 속도 6.5배만 기록됐나?", "abstain"),
            ("2026-10-23 ExampleModel Random F1 .620 Model F1 .715 속도 5.5배", "abstain"),
        ):
            for limit in (1, 4, 20):
                with self.subTest(query=query, limit=limit):
                    result = self.search(query, limit)
                    self.assertEqual(result["answer_state"]["state"], expected)
                    if expected == "review_required":
                        self.assertIn(".715였다", result["results"][0]["snippet"])

    def test_decimal_constraints_stay_with_the_actor_action_clause(self):
        query = "김민수가 ExampleModel 속도 5.5배를 확인했나?"
        for body, expected in (
            ("김민수가 ExampleModel 속도 5.5배를 확인했다.", "supported"),
            ("김민수가 ExampleModel 속도 6.5배를 확인했다.", "abstain"),
            ("이영희가 ExampleModel 속도 5.5배를 확인했다. 김민수는 회의에 참여했다.", "abstain"),
            ("김민수가 ExampleModel 속도 5.5배를 확인하지 않았다.", "abstain"),
            ("김민수가 ExampleModel 속도 6.5배를 확인했다. 이영희가 ExampleModel 속도 5.5배를 확인했다.", "abstain"),
            ("김민수가 ExampleModel 속도 6.5배를 확인했고 이영희가 ExampleModel 속도 5.5배를 확인했다.", "abstain"),
        ):
            self.note("metrics", body)
            for limit in (1, 4, 20):
                with self.subTest(body=body, limit=limit):
                    self.assertEqual(self.search(query, limit)["answer_state"]["state"], expected)

    def test_numeric_attribution_does_not_pool_multiple_actions(self):
        for body, query in (
            ("Alice confirmed ExampleModel speed 6.5 and Bob confirmed ExampleModel speed 5.5.",
             "Did Alice confirm ExampleModel speed 5.5?"),
            ("Alice confirmed ExampleModel speed 6.5 and Bob approved ExampleModel speed 5.5.",
             "Did Alice confirm ExampleModel speed 5.5?"),
            ("김민수가 ExampleModel 속도 6.5배 도입을 승인했고 이영희가 ExampleModel 속도 5.5배 도입을 승인했다.",
             "김민수가 ExampleModel 속도 5.5배 도입을 승인했나?"),
            ("김민수가 ExampleModel 속도 6.5배 도입을 승인했고 이영희가 ExampleModel 속도 5.5배를 확인했다.",
             "김민수가 ExampleModel 속도 5.5배 도입을 승인했나?"),
        ):
            self.note("metrics", body)
            for limit in (1, 4, 20):
                with self.subTest(body=body, limit=limit):
                    self.assertEqual(self.search(query, limit)["answer_state"]["state"], "abstain")

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
            "alice approved ProjectX FP16 adoption. Bob ran the trial.",
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
