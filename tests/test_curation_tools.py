"""Local CLI/MCP parity on synthetic Git sources; no real Vault, network or index writes."""

from copy import deepcopy
from hashlib import sha256
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import cli
from gateway.compaction import annotate_drafts, review_binding
from gateway.core import Settings, vault_documents
from gateway.wiki import build_compaction_plan, check_compaction, review_targets
from test_compaction import no_network


def main():
    repo = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="pvg-curation-tools-") as tmp:
        vault = Path(tmp).resolve()
        staging = vault / ".tmp/curating"
        staging.mkdir(parents=True)
        plan_path = staging / "plan.json"
        source = "30_Conversations/raw/2026/01/01/raw.md"
        target = "50_Knowledge/Limits.md"
        long_body = "Keep SQLite for this release.\n" + "Only on Linux, not Windows. 한글🙂 " * 120 + "\nPrefer short answers."
        bodies = ["Question: what changed?", long_body, "I ran the benchmark.", "Keep all secrets outside the repository."]
        roles = ["user", "user", "assistant", "subagent"]
        raw = "---\nid: raw_test\nmemory_type: transcript\nprojects: [Demo]\n---\n\n" + "\n\n".join(
            f'### {role}\n\n<!-- pvg-event {{"event_id":"e{i}","role":"{role}"}} -->\n\n{body}'
            for i, (role, body) in enumerate(zip(roles, bodies))
        )
        (vault / source).parent.mkdir(parents=True)
        (vault / source).write_text(raw, encoding="utf-8")
        (vault / "unrelated.md").write_text("Unrelated body must not be loaded by source tools.", encoding="utf-8")
        def git(*args):
            return subprocess.run(["git", "-C", str(vault), *args], check=True, capture_output=True).stdout
        git("init", "-q", "-b", "main")
        git("config", "user.name", "Curation Test")
        git("config", "user.email", "test@localhost")
        git("add", ".")
        git("commit", "-qm", "Synthetic base")
        settings = Settings(vault, vault / "never.db", "test", embedding_provider="none")
        before = vault_documents(settings)
        plan = build_compaction_plan(before, before="2026-01-02", revision=git("rev-parse", "HEAD").decode().strip())
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        ids = {node["locator"]: node["id"] for node in plan["selected"]["graph"]["nodes"] if node["type"] == "raw_item"}
        def command(name, *args, payload=None):
            with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), patch.object(cli, "vault_documents", side_effect=AssertionError("source tools must not scan live Vault")), no_network():
                return cli.run(["--vault", str(vault), name, str(plan_path), *args])
        def record(items, expected=None):
            return command("compact-record", payload={"plan_sha256": expected or sha256(plan_path.read_bytes()).hexdigest(), "items": items})
        def discard(item_id):
            return {"id": item_id, "disposition": "discard", "targets": [], "reason": "Synthetic progress only."}

        original_plan = plan_path.read_bytes()
        e1_index = next(i for i, entry in enumerate(plan["review"]["items"]) if entry["id"] == ids["e1"])
        cursor, reconstructed, first_page = None, {}, None
        pages = 0
        while True:
            page = command("compact-read", "--max-characters", "256", *(["--cursor", cursor] if cursor else []))
            assert page["status"] == "ok", page
            first_page = first_page or page
            pages += 1
            assert sum(len(item["text"]) for item in page["primary"]) <= 256
            assert sum(len(item["text"]) for item in page["context_only"]) <= 800
            assert page["review_recorded"] is False
            for item in page["primary"]:
                previous = reconstructed.get(item["locator"], "")
                assert item["span"] == [len(previous), len(previous) + len(item["text"])]
                assert item["event_role"] == roles[int(item["locator"][1:])]
                reconstructed[item["locator"]] = previous + item["text"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert list(reconstructed.values()) == bodies and pages > 10
        assert first_page["context_only"][0]["event_role"] == "assistant"
        assert plan_path.read_bytes() == original_plan, "reading must never record judgments"
        assert command("compact-read", "--cursor", "bad:0:0")["status"] == "blocked"
        assert command("compact-read", "--max-characters", "20000")["status"] == "blocked"

        split = long_body.index("Prefer short answers")
        parts = {"id": ids["e1"], "parts": [
            {"span": [0, split], "disposition": "knowledge", "targets": [target], "reason": "Retain scope and platform restriction.",
             "quote": "Only on Linux, not Windows.", "anchors": {target: "SQLite runs on Linux; Windows is untested."}},
            {"span": [split, len(long_body)], "disposition": "hold", "targets": [], "reason": "Preference needs contextual review."},
        ]}
        result = record([{**parts, "parts": parts["parts"][:1]}])
        assert result["unreviewed_ranges"][ids["e1"]] == [[split, len(long_body)]]
        assert result["semantic_review"] == "not_recorded" and result["approval"] == "not_checked"
        assert record([parts], first_page["plan_sha256"])["reason"] == "plan_changed_before_record"
        assert record([parts])["unreviewed_ranges"][ids["e1"]] == []
        # Same source cursor survives judgment changes; it is bound to source hashes, not plan formatting.
        assert command("compact-read", "--cursor", first_page["next_cursor"])["status"] == "ok"

        invalid = []
        def mutation(change):
            value = deepcopy(parts)
            change(value)
            invalid.append(value)
        mutation(lambda value: value["parts"][0].update(span=[0, True]))
        mutation(lambda value: value["parts"][0].update(span=[0, len(long_body) + 1]))
        mutation(lambda value: value["parts"][0].update(quote="Prefer short answers"))
        mutation(lambda value: value["parts"][0].update(id=ids["e0"]))
        mutation(lambda value: value["parts"][0].update(targets=["50_Knowledge/../../secret.md"]))
        mutation(lambda value: value["parts"][0].update(disposition="imaginary"))
        invalid += [{**discard(ids["e0"]), "span": [0, 1]}, {**discard("unknown")}, {"ids": [ids["e0"], ids["e0"]], "disposition": "discard"},
                    {"id": ids["e3"], "parts": [{"span": [0, len(bodies[3])], "disposition": "user_constraint", "quote": "Keep all secrets", "reason": "Invalid attribution", "targets": ["10_User/PROFILE.md"]}]}]
        for value in invalid:
            previous = plan_path.read_bytes()
            assert record([value])["status"] == "blocked", value
            assert plan_path.read_bytes() == previous
        # Missing/held parts cannot pass final checks, even if the raw file were deleted.
        record([discard(ids["e0"]), discard(ids["e2"]), discard(ids["e3"])])
        plan = json.loads(plan_path.read_bytes())
        grouped = deepcopy(plan)
        grouped["review"]["items"] = [parts, {"ids": [ids["e0"], ids["e2"], ids["e3"]], "disposition": "discard", "targets": [], "reason": "Synthetic progress only."}]
        grouped["finish_checkpoint"] = {"test": "unchanged"}
        plan_path.write_text(json.dumps(grouped), encoding="utf-8")
        record([parts])
        saved = json.loads(plan_path.read_bytes())
        assert saved["review"]["items"] == grouped["review"]["items"] and saved["finish_checkpoint"] == grouped["finish_checkpoint"]
        record([discard(ids["e0"])])
        saved = json.loads(plan_path.read_bytes())
        assert saved["review"]["items"][1]["ids"] == [ids["e2"], ids["e3"]]
        assert "finish_checkpoint" not in saved
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        plan["review"].update(delete_paths=[source], user_knowledge={"status": "unchanged", "reason": "Synthetic material."})
        after = [doc for doc in before if doc["path"] != source]
        assert any("held source" in error for error in check_compaction(plan, before, after)["errors"])
        plan["review"]["items"][e1_index] = {**parts, "parts": parts["parts"][:1]}
        assert any("source ranges remain unreviewed" in error for error in check_compaction(plan, before, after)["errors"])

        # Two judgments in one event keep parent provenance, union targets, and one binding for all parts.
        second_target = "50_Knowledge/Context.md"
        parts["parts"][1].update(disposition="knowledge", targets=[second_target], quote="Prefer short answers", anchors={second_target: "Preference remains scoped to this request."})
        plan["review"]["items"][e1_index] = parts
        plan["review"].update(drafts={target: ".tmp/curating/a.md", second_target: ".tmp/curating/b.md"}, probes=[])
        (staging / "a.md").write_text("SQLite runs on Linux; Windows is untested.\n", encoding="utf-8")
        (staging / "b.md").write_text("Preference remains scoped to this request.\n", encoding="utf-8")
        assert review_targets(plan) == {target, second_target}
        annotate_drafts(vault, plan)
        assert all(ids["e1"] in (staging / name).read_text() for name in ("a.md", "b.md"))
        with patch("gateway.compaction.validated_probes", return_value=[]):
            binding = review_binding(plan, before, after)
            plan["review_checkpoint"] = binding["fingerprints"]
            parts["parts"][0]["reason"] += " Changed first judgment."
            assert review_binding(plan, before, after)["review_required_ids"] == [ids["e1"]]
        # Busy/stale writers cannot replace a plan. Locks are never treated as an approval bypass.
        lock = plan_path.with_name(plan_path.name + ".lock")
        lock.touch()
        assert record([discard(ids["e0"])])["status"] == "blocked"
        lock.unlink()
        try:
            cli.update_plan(plan_path, {}, b"stale")
        except ValueError:
            pass
        else:
            raise AssertionError("stale update was accepted")
        assert not lock.exists()

        # Real MCP stdio -> installed Python CLI, with no Gateway config. Same output as direct CLI.
        def mcp(name, args, *, path_env=None):
            env = {**os.environ, "PATH": path_env if path_env is not None else str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            output = subprocess.run([shutil.which("node"), str(repo / "plugins/persona-vault/scripts/pvg-mcp.js")], input=json.dumps(request) + "\n", encoding="utf-8", capture_output=True, env=env, timeout=15, check=True)
            assert output.stderr == "", output.stderr
            return json.loads(output.stdout)["result"]
        args = {"vault": str(vault), "plan": str(plan_path)}
        response = mcp("pvg_compact_read", {**args, "max_characters": 256})
        assert not response["isError"], response
        assert json.loads(response["content"][0]["text"]) == command("compact-read", "--max-characters", "256")
        response = mcp("pvg_compact_record", {**args, "plan_sha256": sha256(plan_path.read_bytes()).hexdigest(), "items": [discard(ids["e0"])]})
        assert not response["isError"], response
        assert json.loads(response["content"][0]["text"])["plan_sha256"] == sha256(plan_path.read_bytes()).hexdigest()
        response = mcp("pvg_compact_record", {**args, "plan_sha256": "0" * 64, "items": [discard(ids["e0"])]})
        assert response["isError"] and "plan_changed_before_record" in response["content"][0]["text"]
        response = mcp("pvg_compact_read", {**args, "plan": str(vault / "outside.json")})
        assert response["isError"] and "inside Vault .tmp/curating" in response["content"][0]["text"]
        response = mcp("pvg_compact_read", args, path_env=str(staging))
        assert response["isError"] and "pvg-wiki could not start" in response["content"][0]["text"]
        if os.name != "nt":
            external = vault / "outside.json"
            external.write_bytes(plan_path.read_bytes())
            link = staging / "link.json"
            link.symlink_to(external)
            response = mcp("pvg_compact_read", {**args, "plan": str(link)})
            assert response["isError"]
        assert git("diff", "HEAD", "--") == b""
        assert not settings.db_path.exists()
        git("commit", "--allow-empty", "-qm", "Changed base")
        assert command("compact-read")["reason"] == "plan_base_changed"
    print("curation tools: bounded Unicode paging, multipart coverage/binding, stale-write protection, CLI/MCP parity passed")


if __name__ == "__main__":
    main()
