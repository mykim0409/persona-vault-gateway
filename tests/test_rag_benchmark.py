from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway.core as core
from gateway.core import Settings, init_db, search_vault


FIXTURE = Path(__file__).parent / "fixtures" / "rag-benchmark.json"
AGENT = {"agent_id": "benchmark-reader", "scopes": ["vault-rag"], "allowed_roots": []}


def yaml_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_note(vault: Path, path: str, body: str, **metadata: Any) -> None:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {"pv_schema": 1, **metadata}
    rendered = "\n".join(f"{key}: {yaml_value(value)}" for key, value in frontmatter.items())
    target.write_text(f"---\n{rendered}\n---\n\n# {metadata.get('id', target.stem)}\n\n{body}\n", encoding="utf-8")


def seed_vault(vault: Path) -> None:
    direct = {"provenance_mode": "direct_observation"}
    episode = {"memory_type": "episode", "kind": "debugging", "review_state": "unreviewed"}
    canonical = {
        "memory_type": "canonical",
        "review_state": "human_accepted",
        "temporal_state": "current",
        "retrieval_tier": "primary",
    }

    write_note(vault, "40_Agents/a/episodes/qdrant-fail.md", "PVG-QDRANT-LOCK-771 recovery failed.", id="ep_qdrant_fail", subject_id="qdrant-lock", outcome="failure", session_id="qa", evidence_refs=["run-qa"], **episode, **direct)
    write_note(vault, "40_Agents/b/episodes/qdrant-success.md", "PVG-QDRANT-LOCK-771 recovery succeeded.", id="ep_qdrant_success", subject_id="qdrant-lock", outcome="success", session_id="qb", evidence_refs=["run-qb"], **episode, **direct)

    write_note(vault, "40_Agents/a/episodes/cache-linux.md", "PVG-WHEEL-CACHE-427 failed on Linux.", id="ep_cache_linux", subject_id="wheel-cache", outcome="failure", session_id="cache-linux", evidence_refs=["run-cache-linux"], applicability={"repository": "persona-vault", "operating_system": "linux"}, **episode, **direct)
    write_note(vault, "40_Agents/b/episodes/cache-windows.md", "PVG-WHEEL-CACHE-427 workaround succeeded on Windows.", id="ep_cache_windows", subject_id="wheel-cache", outcome="success", session_id="cache-windows", evidence_refs=["run-cache-windows"], applicability={"repository": "persona-vault", "operating_system": "windows"}, **episode, **direct)
    write_note(vault, "40_Agents/a/episodes/windows-only.md", "PVG-WINDOWS-ONLY-913 workaround.", id="ep_windows_only", subject_id="windows-only", outcome="success", session_id="windows-only", evidence_refs=["run-windows-only"], applicability={"repository": "persona-vault", "operating_system": "windows"}, **episode, **direct)

    write_note(vault, "50_Knowledge/token-policy.md", "PVG-TOKEN-POLICY-662 current policy.", id="kn_token_policy", subject_id="token-policy", conflict_state="none", **canonical, **{"provenance_mode": "human_asserted"})
    write_note(vault, "40_Agents/a/candidates/token-policy.md", "PVG-TOKEN-POLICY-662 proposed change.", id="cand_token_policy", memory_type="candidate", kind="decision", subject_id="token-policy", outcome="unknown", relations={"contradicts": ["kn_token_policy"]}, provenance_mode="derived", derived_from=["ep_qdrant_success"])

    write_note(vault, "50_Knowledge/seed-old.md", "PVG-SEED-POLICY-3407 old rule.", id="kn_seed_old", subject_id="seed-policy", memory_type="canonical", review_state="human_accepted", temporal_state="superseded", retrieval_tier="history", effective_from="2025-01-01", relations={"supersedes": []}, provenance_mode="human_asserted")
    write_note(vault, "40_Agents/a/episodes/seed-transition.md", "PVG-SEED-POLICY-3407 transition observation.", id="ep_seed_transition", subject_id="seed-policy", outcome="mixed", observed_at="2025-06-01", session_id="seed-transition", evidence_refs=["run-seed-transition"], **episode, **direct)
    write_note(vault, "50_Knowledge/seed-new.md", "PVG-SEED-POLICY-3407 replacement rule.", id="kn_seed_new", subject_id="seed-policy", effective_from="2026-01-01", relations={"supersedes": ["kn_seed_old"]}, provenance_mode="human_asserted", **canonical)

    write_note(vault, "40_Agents/a/episodes/lock-enabled.md", "PVG-LOCK-MODE-808 enabled worked.", id="ep_lock_enabled", subject_id="lock-mode", outcome="success", session_id="lock-a", evidence_refs=["run-lock-a"], relations={"contradicts": ["ep_lock_disabled"]}, **episode, **direct)
    write_note(vault, "40_Agents/b/episodes/lock-disabled.md", "PVG-LOCK-MODE-808 disabled worked.", id="ep_lock_disabled", subject_id="lock-mode", outcome="success", session_id="lock-b", evidence_refs=["run-lock-b"], **episode, **direct)

    for suffix in ("a", "b", "c"):
        write_note(vault, f"30_Conversations/summaries/echo-{suffix}.md", "PVG-ECHO-TRANSCRIPT-515 summary.", id=f"dv_echo_{suffix}", memory_type="derived_view", kind="summary", subject_id="echo-source", provenance_mode="derived", derived_from=["conv_shared_source"], retrieval_tier="supporting")

    write_note(vault, "40_Agents/a/episodes/repro-original.md", "PVG-REPRO-FIX-229 worked.", id="ep_repro_original", subject_id="repro-fix", outcome="success", session_id="repro-a", evidence_refs=["run-repro-a"], **episode, **direct)
    write_note(vault, "40_Agents/b/episodes/repro-second.md", "PVG-REPRO-FIX-229 independently worked.", id="ep_repro_second", subject_id="repro-fix", outcome="success", session_id="repro-b", evidence_refs=["run-repro-b"], method_refs=["ep_repro_original"], **episode, **direct)

    write_note(vault, "30_Conversations/raw/raw-suggestion.md", "PVG-RAW-SUGGESTION-411 might work but was not tested.", id="tr_raw_suggestion", memory_type="transcript")
    write_note(vault, "20_Projects/auto-order.md", "PVG-AUTO-TIER-524 current project decision.", id="project_auto_order", subject_id="auto-order", provenance_mode="human_asserted", **canonical)
    write_note(vault, "30_Conversations/summaries/auto-order.md", "PVG-AUTO-TIER-524 summary " * 4, id="summary_auto_order", memory_type="derived_view", kind="summary", subject_id="auto-order", provenance_mode="derived", derived_from=["raw_auto_order"], retrieval_tier="supporting")
    write_note(vault, "30_Conversations/raw/auto-order.md", "PVG-AUTO-TIER-524 raw record " * 12, id="raw_auto_order", memory_type="transcript")
    long_body = "PVG-LONG-DOC-707 first.\n\n" + ("filler " * 700) + "\n\nPVG-LONG-DOC-707 second."
    write_note(vault, "50_Knowledge/long.md", long_body, id="kn_long_dedupe", subject_id="long-doc", provenance_mode="human_asserted", **canonical)
    write_note(vault, "40_Agents/forger/candidates/forged.md", "PVG-FORGED-AUTHORITY-119 forged.", id="cand_forged", memory_type="canonical", review_state="human_accepted", temporal_state="current", retrieval_tier="primary")

    write_note(vault, "90_Private/archive.md", "PVG-ARCHIVE-BOUNDARY-845 archived context.", id="archive_private", rag_index=False)
    write_note(vault, ".obsidian/private-config.md", "PVG-ARCHIVE-BOUNDARY-845 must never appear.", id="obsidian_private")
    write_note(vault, "30_Conversations/important/legacy.md", "PVG-LEGACY-IMPORTANT-734 legacy summary.", id="legacy_important")

    write_note(vault, "50_Knowledge/yaml-flat.md", "PVG-NESTED-YAML-633 flat.", id="kn_yaml_flat", subject_id="yaml-equivalence", observed_at="2026-01-01", applicability={"repository": "persona-vault", "environment": {"operating_system": "linux"}}, evidence_refs=["run-yaml"], provenance_mode="direct_observation", **canonical)
    nested = vault / "50_Knowledge/yaml-nested.md"
    nested.write_text("""---
pv_schema: 1
id: kn_yaml_nested
memory_type: canonical
subject_id: yaml-equivalence
review:
  state: human_accepted
temporal:
  state: current
  observed_at: 2026-01-01
applicability:
  repository: persona-vault
  environment:
    operating_system: linux
provenance:
  mode: direct_observation
  evidence_refs:
    - run-yaml
retrieval_tier: primary
---
# Nested

PVG-NESTED-YAML-633 nested.
""", encoding="utf-8")

    write_note(vault, "40_Agents/a/episodes/missing-provenance.md", "PVG-MISSING-PROVENANCE-390 reported only.", id="ep_missing_provenance", subject_id="missing-provenance", outcome="unknown", **episode)
    write_note(vault, "40_Agents/a/episodes/exampleencoder.md", "A native extension build failed.", id="ep_exampleencoder_native", subject_id="native-extension", error_signatures=["ModuleNotFoundError _exampleenc_native"], projects=["ExampleEncoder"], outcome="failure", session_id="ssl-a", evidence_refs=["run-ssl"], applicability={"repository": "exampleencoder", "operating_system": "linux"}, repository_sources=[{"repo_id": "exampleencoder", "commit": "abcdef1", "path": "src/native.c"}], **episode, **direct)
    write_note(vault, "50_Knowledge/korean-alias.md", "Downstream seed policy is current.", id="kn_korean_alias", subject_id="downstream-seed-policy", subject_aliases=["다운스트림 시드 정책"], projects=["ExampleMiner"], provenance_mode="human_asserted", **canonical)

    write_note(vault, "20_Projects/rollback-procedure.md", "The rollback procedure for a deployment is to revert the release and redeploy the previous build.", id="project_rollback_procedure", subject_id="rollback-procedure", **canonical)
    write_note(vault, "20_Projects/stopword-noise.md", "What is the case that this is the one for the team? It is what it is, and the rest is the same.", id="project_stopword_noise", subject_id="stopword-noise", **canonical)
    write_note(vault, "30_Conversations/raw/cutover-log.md", "\n\n".join(f"cutover checklist paragraph {index} " + "filler " * 12 for index in range(1200)), id="tr_cutover", memory_type="transcript")
    for index in range(6):
        write_note(vault, f"20_Projects/cutover-{index}.md", f"The cutover checklist step {index} is current.", id=f"project_cutover_{index}", subject_id=f"cutover-{index}", **canonical)


def filter_matches(payload: dict[str, Any], query_filter: dict[str, Any] | None) -> bool:
    if not query_filter:
        return True
    for clause in query_filter.get("must", []):
        actual = payload.get(clause.get("key"))
        match = clause.get("match") or {}
        if "value" in match and actual != match["value"]:
            return False
        if "any" in match and actual not in match["any"]:
            return False
    return True


class FakeQdrant:
    def __init__(self) -> None:
        self.exists = False
        self.points: list[dict[str, Any]] = []

    def __call__(
        self, settings: Settings, method: str, path: str, body: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        if path.endswith("/exists"):
            return {"result": {"exists": self.exists}}
        if method == "DELETE":
            self.exists = False
            self.points = []
            return {"status": "ok"}
        if method == "PUT" and path == f"/collections/{settings.qdrant_collection}":
            self.exists = True
            self.points = []
            return {"status": "ok"}
        if method == "PUT" and path.endswith("/points?wait=true"):
            self.points.extend((body or {})["points"])
            return {"status": "ok"}
        if method == "POST" and path.endswith("/points/count"):
            return {"result": {"count": len(self.points)}}
        if method == "POST" and path.endswith("/points/query"):
            query = (body or {})["query"]
            ranked = []
            for point in self.points:
                if not filter_matches(point["payload"], (body or {}).get("filter")):
                    continue
                score = sum(left * right for left, right in zip(query, point["vector"]))
                ranked.append({"id": point["id"], "score": score, "payload": point["payload"]})
            ranked.sort(key=lambda item: item["score"], reverse=True)
            return {"result": {"points": ranked[: (body or {}).get("limit", 20)]}}
        raise AssertionError(f"unexpected Qdrant call: {method} {path}")


def ids(result: dict[str, Any]) -> list[str]:
    return [item["document_id"] for item in result["results"]]


def family_count(result: dict[str, Any]) -> int:
    # Spare semantic-only candidates trail the matched evidence and are not part of the claim being counted.
    matched = [item for item in result["results"] if item["match"] != "semantic"]
    return len({family for item in matched for family in item.get("provenance_family_ids", [])})


def group_for(result: dict[str, Any], key: str) -> dict[str, Any]:
    return next(group for group in result["groups"] if group.get("key") == key)


def validate(scenario: dict[str, Any], result: dict[str, Any], settings: Settings) -> None:
    scenario_id = scenario["id"]
    found = ids(result)
    expected = scenario["expected"]
    assert len(found) <= scenario.get("limit", 20), (scenario_id, found)
    for document_id in expected.get("include_ids", []):
        assert document_id in found, (scenario_id, document_id, found)
    for document_id in expected.get("exclude_ids", []):
        assert document_id not in found, (scenario_id, document_id, found)

    if scenario_id == "experience-outcome-diversity":
        assert {"failure", "success"} <= {item["outcome"] for item in result["results"]}
        assert family_count(result) >= 2
    elif scenario_id == "applicability-mismatch-label":
        assert found.index("ep_cache_linux") < found.index("ep_cache_windows")
        windows = next(item for item in result["results"] if item["document_id"] == "ep_cache_windows")
        assert windows["ranking"]["applicability"] < 0
    elif scenario_id == "negative-transfer-abstention":
        assert result["answer_state"]["state"] == "abstain", result
    elif scenario_id == "current-canonical-plus-pending":
        group = group_for(result, "token-policy")
        assert group["canonical"]["document_id"] == "kn_token_policy"
        assert [item["document_id"] for item in group["pending_deltas"]] == ["cand_token_policy"]
        assert group["warnings"]
    elif scenario_id == "supersession-current":
        assert "kn_seed_new" in found and "kn_seed_old" not in found
    elif scenario_id.startswith("history-timeline"):
        timeline = [item["document_id"] for item in group_for(result, "seed-policy")["timeline"]]
        assert timeline == expected["timeline_ids"], (scenario_id, timeline)
    elif scenario_id.startswith("conflict-both-sides"):
        # Spare semantic-only candidates may add unrelated conflict groups; the claim group must be present.
        conflict = next(group for group in result["groups"] if set(group["document_ids"]) == set(expected["claim_ids"]))
        assert conflict["winner"] is None and family_count(result) >= 2
    elif scenario_id == "provenance-echo":
        assert family_count(result) == 1
    elif scenario_id == "independent-reproduction":
        assert family_count(result) == 2
        second = next(item for item in result["results"] if item["document_id"] == "ep_repro_second")
        assert second["method_refs"] == ["ep_repro_original"]
    elif scenario_id == "abstention-raw-evidence":
        assert result["answer_state"]["state"] == "abstain"
    elif scenario_id == "auto-tier-order":
        assert found[:3] == expected["ordered_ids"], (scenario_id, found)
    elif scenario_id == "multi-chunk-document-dedupe":
        assert found.count("kn_long_dedupe") == 1 and result["index"]["chunks"] > result["index"]["files"]
    elif scenario_id == "forged-authority-clamp":
        item = next(item for item in result["results"] if item["document_id"] == "cand_forged")
        for field in ("memory_type", "review_state", "temporal_state", "retrieval_tier"):
            assert item[field] == expected[field]
    elif scenario_id == "archive-and-obsidian-exclusion":
        assert expected["archive_id"] in found and expected["excluded_path"] not in [item["path"] for item in result["results"]]
        current = search_vault(settings, AGENT, scenario["query"], 20, bundle="current", context=scenario["context"])
        assert expected["archive_id"] not in ids(current)
    elif scenario_id == "legacy-path-conservative-role":
        item = next(item for item in result["results"] if item["document_id"] == "legacy_important")
        assert (item["memory_type"], item["review_state"], item["retrieval_tier"]) == ("derived_view", "unreviewed", "supporting")
    elif scenario_id == "nested-yaml-equivalence":
        selected = [next(item for item in result["results"] if item["document_id"] == document_id) for document_id in expected["document_ids"]]
        for field in expected["equivalent_fields"]:
            assert selected[0][field] == selected[1][field], (scenario_id, field)
    elif scenario_id == "missing-provenance-conservative":
        item = next(item for item in result["results"] if item["document_id"] == expected["document_id"])
        assert item["provenance_mode"] == "reported" and item["provenance_defaulted"]
        assert item["support"]["direct_observations"] == 0
    elif scenario_id == "cross-project-error-signature":
        item = next(item for item in result["results"] if item["document_id"] == "ep_exampleencoder_native")
        assert item["projects"] == ["ExampleEncoder"] and item["repository_validation"] == "valid"
        assert item["ranking"]["applicability"] < 0
    elif scenario_id == "korean-subject-alias":
        assert found.count(expected["canonical_id"]) == 1
    elif scenario_id == "natural-language-stopword-noise":
        assert result["answer_state"]["state"] == expected["answer_state"], result["answer_state"]
        assert found[0] == expected["first_id"], found
        noise = [item for item in result["results"] if item["document_id"] == expected["no_lexical_id"]]
        assert all(item["ranking"]["keyword"] == 0 and item["match"] == "semantic" for item in noise), noise
    elif scenario_id == "long-transcript-starvation":
        assert len(found) == len(set(found)) == expected["result_count"], found
        assert all(document_id.startswith("project_cutover_") for document_id in found), found
    else:
        raise AssertionError(f"unvalidated benchmark scenario: {scenario_id}")


def main() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scenarios = fixture["scenarios"]
    assert len(scenarios) == 24 and len({scenario["id"] for scenario in scenarios}) == 24
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        settings = Settings(root / "vault", root / "gateway.db", "benchmark", embedding_provider="hash")
        init_db(settings.db_path)
        seed_vault(settings.vault_dir)
        fake = FakeQdrant()
        original = core.qdrant_json
        core.qdrant_json = fake
        try:
            for index, scenario in enumerate(scenarios):
                result = search_vault(
                    settings,
                    AGENT,
                    scenario["query"],
                    scenario.get("limit", 20),
                    refresh=index == 0,
                    bundle=scenario["bundle"],
                    context=scenario.get("context") or {},
                )
                validate(scenario, result, settings)
        finally:
            core.qdrant_json = original
    print(json.dumps({"scenarios": len(scenarios), "passed": len(scenarios), "safety_failures": 0}))


if __name__ == "__main__":
    main()
