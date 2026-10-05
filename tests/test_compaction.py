"""Read-only compaction planning/checking against disposable Git repositories."""

from __future__ import annotations

import contextlib
from copy import deepcopy
from argparse import Namespace
from dataclasses import replace
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import cli, core
from gateway.compaction import annotate_drafts, concurrent_raw_captures, finish_search, review_binding, staging_path
from gateway.core import Settings, document_metadata, rag_answer_state, vault_documents
from gateway.wiki import _src_ids, build_compaction_plan, check_compaction


@contextlib.contextmanager
def no_network():
    """Poison every Qdrant/embedding/socket entry point; any call is recorded and fails the test."""
    calls: list[str] = []

    def blocked(name: str):
        def fail(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} must not run when EMBEDDING_PROVIDER=none")

        return fail

    targets = (
        (core, "qdrant_json"), (core, "cloudflare_embedding_request"), (core, "cloudflare_embeddings"),
        (core, "embed_documents"), (core, "embed_query"), (urllib.request, "urlopen"),
        (socket, "create_connection"), (socket.socket, "connect"),
    )
    with contextlib.ExitStack() as stack:
        for owner, name in targets:
            stack.enter_context(patch.object(owner, name, blocked(f"{getattr(owner, '__name__', 'socket.socket')}.{name}")))
        yield calls
    assert not calls, calls


def efficiency_checks() -> None:
    documents = []
    for index in range(8):
        documents.append({
            "document_id": f"raw_{index}", "path": f"30_Conversations/raw/2026/01/01/source-{index}.md",
            "memory_type": "transcript", "projects": ["Demo"], "subject_id": "demo.subject",
            "document_hash": "a" * 64,
            "text": "\n\n".join(
                '### user\n\n<!-- pvg-event {"event_id":"' + f"event-{index}-{event:03}" + '"} -->\n\n'
                'A scoped synthetic request, not a real user statement.' for event in range(25)
            ),
        })
    plan = build_compaction_plan(documents, before="2026-01-02")
    preview = cli.plan_output(plan, Namespace(output=None, full=False), Path("/unused"))
    encoded_size = lambda value: len(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode())
    full_size, preview_size = encoded_size(plan), encoded_size(preview)
    assert plan["selected"]["source_items"] == 200
    assert preview_size < full_size / 20, (full_size, preview_size)
    assert "graph" not in preview["selected"] and "review" not in preview and "queue" not in preview
    assert cli.plan_output(plan, Namespace(output=None, full=True), Path("/unused")) == plan
    print(json.dumps({"synthetic_items": 200, "full_plan_bytes": full_size, "preview_bytes": preview_size}))


def workflow_checks() -> None:
    with TemporaryDirectory(prefix="pvg-review-finish-") as temporary:
        root = Path(temporary)
        staging = root / ".tmp/curating"
        staging.mkdir(parents=True)
        plan_path = staging / "plan.json"
        sources = [f"30_Conversations/raw/2026/01/0{i + 1}/source.md" for i in range(2)]
        targets = [f"20_Projects/Demo/topic-{i}.md" for i in range(2)]

        def note(path: Path, identity: str, body: str, memory_type: str, temporal_state: str = "current") -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"---\nid: {identity}\nmemory_type: {memory_type}\nreview_state: human_accepted\n"
                "provenance_mode: human_asserted\n"
                f"temporal_state: {temporal_state}\nprojects: [Demo]\nsubject_id: demo\n---\n\n{body}\n",
                encoding="utf-8",
            )

        def git(*args: str) -> bytes:
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout

        def capture(session: str, contents: list[str], *, relation: str = "") -> tuple[Path, str]:
            messages = [
                {"event_id": f"{session}-{index}", "timestamp": f"2026-01-03T00:{index:02}:00+09:00",
                 "role": "user", "content": content}
                for index, content in enumerate(contents)
            ]
            with TemporaryDirectory(prefix="pvg-native-capture-") as fixture:
                captured = Settings(Path(fixture), Path(fixture) / "gateway.db", "test", embedding_provider="hash")
                agent = {"agent_id": "capture-agent", "scopes": ["conversation-log"], "allowed_roots": ["30_Conversations/raw"]}
                result = core.save_conversation(captured, agent, {
                    "session_id": session, "title": "capture", "messages": messages,
                    "relations": {"related": [relation]} if relation else {}, "mode": "merge",
                })
                text = (captured.vault_dir / result["path"]).read_text(encoding="utf-8")
                assert len(core.parse_conversation(text)[2]) == len(contents)
                return root / result["path"], text

        for i in range(2):
            note(root / sources[i], f"raw_{i}", "Repeated scoped evidence. " * 100, "transcript")
            note(root / targets[i], f"kn_{i}", "Old scoped knowledge.", "canonical")
        today_path, today_base = capture("original", ["First captured event."])
        today_path.parent.mkdir(parents=True, exist_ok=True)
        today_path.write_text(today_base, encoding="utf-8")
        git("init", "-q", "-b", "main")
        git("config", "user.name", "Workflow Test")
        git("config", "user.email", "test@localhost")
        git("add", "30_Conversations", "20_Projects")
        git("commit", "-qm", "Test base")
        settings = Settings(root, root / "existing.db", "test", embedding_provider="hash")
        settings.db_path.touch()
        before = vault_documents(settings)
        plan = build_compaction_plan(before, before="2026-01-03", revision=git("rev-parse", "HEAD").decode().strip())
        items = {node["id"]: node["path"] for node in plan["selected"]["graph"]["nodes"] if node["type"] == "raw_item"}
        for entry in plan["review"]["items"]:
            entry.update(disposition="merge", targets=[targets[sources.index(items[entry["id"]])]], reason="Preserves scoped evidence.")
        plan["review"].update(
            delete_paths=sources, user_knowledge={"status": "unchanged", "reason": "No new preference."},
            drafts={target: f".tmp/curating/target-{i}.md" for i, target in enumerate(targets)},
            probes=[
                {"query": "Scoped knowledge", "bundle": "current", "expect_paths": [targets[0]], "answer_state": "supported"},
                {"query": "Nonexistent claim", "bundle": "current", "forbid_paths": targets, "answer_state": "abstain"},
                {"query": "Historical knowledge", "bundle": "history", "expect_paths": [targets[1]], "answer_state": "evidence"},
            ],
        )
        for i in range(2):
            note(staging / f"target-{i}.md", f"kn_{i}", "Consolidated scoped knowledge.", "canonical", "historical" if i else "current")
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        with patch.object(cli, "settings_for", return_value=settings):
            def command(name: str, *extra: str) -> dict:
                return cli.run(["--vault", str(root), name, str(plan_path), *extra])

            checked = command("compact-review")
            assert checked["status"] == "ok", checked
            assert checked["review_binding"]["reused_items"] == 0
            base_scope = checked["review_binding"]["scope_sha256"]
            recorded_review = command("compact-review", "--record")
            assert recorded_review["semantic_review"] == "recorded"
            assert recorded_review["review_binding"]["scope_sha256"] == checked["review_binding"]["scope_sha256"]
            assert git("diff", "--name-only") == b"", "review modified tracked Markdown"
            assert command("compact-review")["review_binding"]["reused_items"] == 2
            today_path.write_text(capture("original", ["First captured event.", "Later captured event."])[1], encoding="utf-8")
            late_path, late_text = capture("new", ["New same-day capture. " * 500])
            late_path.write_text(late_text, encoding="utf-8")
            concurrent = command("compact-review")
            assert concurrent["status"] == "ok" and concurrent["review_binding"]["scope_sha256"] == base_scope, concurrent
            assert concurrent["review_binding"]["reused_items"] == 2
            assert concurrent["active_characters"] == checked["active_characters"]
            assert concurrent["concurrent_raw_captures"] == {
                today_path.relative_to(root).as_posix(): 1, late_path.relative_to(root).as_posix(): 1,
            }
            linked_path, linked_text = capture("linked", ["Live inbound reference."], relation="raw_0")
            linked_path.write_text(linked_text, encoding="utf-8")
            linked = command("compact-review")
            assert linked["status"] == "blocked" and "new Wiki integrity errors" in linked["errors"], linked
            linked_path.unlink()
            dependency_plan = deepcopy(json.loads(plan_path.read_text()))
            dependency_plan["review"]["dependencies"] = [today_path.relative_to(root).as_posix()]
            _normalized, permitted = concurrent_raw_captures(root, dependency_plan, before, vault_documents(settings))
            assert today_path.relative_to(root).as_posix() not in permitted
            for guard in ("cutoff", "source", "target", "graph"):
                guarded = deepcopy(plan)
                capture_path = today_path.relative_to(root).as_posix()
                if guard == "cutoff":
                    guarded["before"] = "2026-01-04"
                elif guard == "source":
                    guarded["selected"]["source_paths"].append(capture_path)
                elif guard == "target":
                    guarded["review"]["items"][0]["targets"].append(capture_path)
                else:
                    guarded["selected"]["graph"]["nodes"].append({"path": capture_path, "sha256": "f" * 64})
                _normalized, permitted = concurrent_raw_captures(root, guarded, before, vault_documents(settings))
                assert capture_path not in permitted, guard
            today_path.write_text(capture("original", ["Mutated old event.", "Later captured event."])[1], encoding="utf-8")
            assert command("compact-review")["status"] == "blocked"
            stale_snapshot = vault_documents(settings)
            today_path.write_text(capture("original", ["First captured event.", "Later captured event."])[1], encoding="utf-8")
            raced = cli.run_compaction(Namespace(command="compact-review", plan=str(plan_path), record=False), settings, stale_snapshot)
            assert raced["status"] == "blocked" and "raw capture changed during validation" in raced["reason"], raced
            tampered = today_path.read_text().replace('ended_at: "2026-01-03T00:01:00+09:00"', 'ended_at: "2026-01-03T00:09:00+09:00"')
            today_path.write_text(tampered, encoding="utf-8")
            assert command("compact-review")["status"] == "blocked"
            today_path.write_text(capture("original", ["First captured event.", "Later captured event."])[1], encoding="utf-8")
            today_path.unlink()
            assert command("compact-review")["status"] == "blocked"
            today_path.write_text(capture("original", ["First captured event.", "Later captured event."])[1], encoding="utf-8")
            impostor = root / "30_Conversations/raw/2026/01/03/not-a-native-capture.md"
            note(impostor, "raw_impostor", "Unplanned current-day raw.", "transcript")
            assert command("compact-review")["status"] == "blocked"
            impostor.unlink()
            old_path = root / "30_Conversations/raw/2026/01/01/unplanned-old.md"
            note(old_path, "raw_old", "Unplanned old raw.", "transcript")
            assert command("compact-review")["status"] == "blocked"
            old_path.unlink()
            note(root / "50_Knowledge/unplanned.md", "kn_unplanned", "Outside this plan.", "canonical")
            assert command("compact-review")["reason"] == "unplanned_changes_during_review"
            (root / "50_Knowledge/unplanned.md").unlink()
            note(staging / "target-0.md", "kn_0", "Corrected scoped knowledge.", "canonical")
            changed = command("compact-review")
            assert changed["review_binding"]["reused_items"] == 1, changed
            assert len(changed["review_binding"]["review_required_ids"]) == 1
            assert command("compact-review", "--record")["status"] == "ok"
            bound = json.loads(plan_path.read_text())
            projected = cli.projected_documents(root, bound, before)
            scope = review_binding(bound, before, projected)["scope_sha256"]
            for dependency in (sources[0], targets[0]):
                drifted = [{**doc, "document_hash": "f" * 64} if doc["path"] == dependency else doc for doc in before]
                assert review_binding(bound, drifted, projected)["reused_items"] == 0
            for source in sources:
                (root / source).unlink()
            for i, target in enumerate(targets):
                (root / target).write_bytes((staging / f"target-{i}.md").read_bytes())
            assert command("compact-check")["status"] == "ok"
            assert command("compact-review")["semantic_review"] == "reusable"
            applied_status = git("status", "--porcelain=v1")
            recorded = plan_path.read_bytes()
            with patch.object(core, "qdrant_index_current", side_effect=lambda _s, **kwargs: kwargs.get("allow_stale", False)), patch.object(
                core, "index_vault", side_effect=core.EmbeddingLimitError("quota blocked until reset")
            ) as quota:
                failed = command("compact-finish")
                assert failed["status"] == "blocked" and "quota" in failed["reason"], failed
                assert quota.call_count == 1 and plan_path.read_bytes() == recorded

            current = False
            def index(_settings: Settings) -> dict:
                nonlocal current
                current = True
                return {"updated": 2}

            def search(_settings: Settings, _agent: dict, query: str, **kwargs: object) -> dict:
                assert kwargs["refresh"] is False
                results = [
                    {**doc, "match": "keyword"} for doc in vault_documents(_settings)
                    if doc["path"] in targets and core.bundle_accepts(doc, kwargs["bundle"])
                ] if query in {"Scoped knowledge", "Historical knowledge"} else []
                return {
                    "index": {"stale": False}, "results": results,
                    "answer_state": rag_answer_state(results, kwargs["bundle"]),
                }

            with patch.object(core, "qdrant_index_current", side_effect=lambda _s, **kwargs: current or kwargs.get("allow_stale", False)), patch.object(
                core, "index_vault", side_effect=index
            ) as indexing, patch.object(core, "qdrant_json", return_value={"result": {"count": 0}}), patch.object(
                core, "search_vault", side_effect=search
            ) as searching:
                result = command("compact-finish")
                assert result["status"] == "ok" and result["index_verification"] == "passed", result
                assert result["approval"] == "not_checked"
                assert result["search"]["probes"][2]["answer_state"] == "evidence"
                again = command("compact-finish")
                assert again["search"]["reused"] is True, again
                assert again["review_binding"]["scope_sha256"] == scope
                assert indexing.call_count == 1 and searching.call_count == 3
                late_path.write_text(capture("new", ["New same-day capture. " * 500, "Another event after finish."])[1], encoding="utf-8")
                refreshed = command("compact-finish")
                assert refreshed["status"] == "ok" and refreshed["search"]["reused"] is False, refreshed
                assert refreshed["review_binding"]["scope_sha256"] == scope
                assert searching.call_count == 6
                assert command("compact-finish")["search"]["reused"] is True
                saved = plan_path.read_bytes()
                invalid = json.loads(saved)
                invalid["review"]["probes"][2]["answer_state"] = "unknown"
                plan_path.write_text(json.dumps(invalid), encoding="utf-8")
                for name in ("compact-review", "compact-finish"):
                    failed = command(name)
                    assert failed["status"] == "blocked" and "expected answer_state" in failed["reason"], failed
                assert indexing.call_count == 1 and searching.call_count == 6
                plan_path.write_bytes(saved)
                note(root / targets[0], "kn_0", "Unreviewed edit.", "canonical")
                assert command("compact-finish")["reason"] == "applied_patch_differs_from_drafts"
                assert indexing.call_count == 1 and plan_path.read_bytes() == saved
                (root / targets[0]).write_bytes((staging / "target-0.md").read_bytes())
                invalid = json.loads(saved)
                invalid["review"]["probes"][0]["query"] = "Changed question"
                plan_path.write_text(json.dumps(invalid), encoding="utf-8")
                assert command("compact-finish")["reason"] == "semantic_review_changed_or_missing"
                assert indexing.call_count == 1
                plan_path.write_bytes(saved)
                with patch.object(core, "qdrant_json", return_value={"result": {"count": 1}}):
                    assert "retired sources" in command("compact-finish")["reason"]
                uncached = json.loads(saved)
                uncached.pop("finish_checkpoint")
                plan_path.write_text(json.dumps(uncached), encoding="utf-8")
                pending = plan_path.read_bytes()
                for failure in ("fallback", "probe", "drift"):
                    def failed_search(*args: object, **kwargs: object) -> dict:
                        response = search(*args, **kwargs)
                        if failure == "fallback":
                            response["index"]["fallback_reason"] = "embedding_limit"
                        elif failure == "probe":
                            response["answer_state"]["state"] = "review_required"
                        else:
                            note(root / targets[0], "kn_0", "Concurrent unreviewed edit.", "canonical")
                        return response
                    with patch.object(core, "search_vault", side_effect=failed_search):
                        failed = command("compact-finish")
                        assert failed["status"] == "blocked", (failure, failed)
                        assert plan_path.read_bytes() == pending
                    (root / targets[0]).write_bytes((staging / "target-0.md").read_bytes())
                with patch.object(core, "qdrant_index_current", return_value=False):
                    assert "compatible existing index" in command("compact-finish")["reason"]
                    assert indexing.call_count == 1
                note(staging / "target-0.md", "kn_0", "Revised after a failed apply.", "canonical")
                rereview = command("compact-review")
                assert rereview["review_binding"]["reused_items"] == 1, rereview
                assert command("compact-review", "--record")["status"] == "ok"
                assert command("compact-finish")["reason"] == "applied_patch_differs_from_drafts"
                assert indexing.call_count == 1
                note(root / sources[0], "raw_0", "Altered raw is not a valid partial apply.", "transcript")
                assert command("compact-review")["reason"] == "unplanned_changes_during_review"
                (root / sources[0]).unlink()
            assert git("status", "--porcelain=v1") == applied_status
            assert settings.db_path.read_bytes() == b"", "mock workflow touched the database"

            # Explicit keyword-only mode: the same validated probes run against the unchanged vault with no
            # index, Qdrant or embedding call, and previously stored vector/index metadata is never touched.
            note(staging / "target-0.md", "kn_0", "Consolidated scoped knowledge.", "canonical")
            (root / targets[0]).write_bytes((staging / "target-0.md").read_bytes())
            assert command("compact-review", "--record")["status"] == "ok"
            legacy_db = root / "legacy-index.db"
            core.init_db(legacy_db)
            core.set_rag_index_meta(replace(settings, db_path=legacy_db), {
                "rebuild_state": "ready", "provider": "cloudflare", "chunks": "42",
                "embedding_blocked_until": "2020-01-01T00:00:00+00:00",
            })
            legacy_bytes = legacy_db.read_bytes()
            keyword_only = replace(settings, db_path=legacy_db, embedding_provider="none")
            real_search = core.search_vault
            unchecked = plan_path.read_bytes()
            assert "finish_checkpoint" not in json.loads(unchecked)

            def finish_as(mode: Settings) -> dict:
                with patch.object(cli, "settings_for", return_value=mode):
                    return command("compact-finish")

            def stored_binding() -> str:
                return json.loads(plan_path.read_text())["finish_checkpoint"]["binding"]

            with no_network(), patch.object(core, "search_vault", wraps=real_search) as searching:
                first = finish_as(keyword_only)
                assert first["status"] == "ok" and first["index_verification"] == "passed", first
                assert first["search"]["reused"] is False and first["search"]["semantic"] == "disabled", first
                assert [(c["query"], c["passed"], c["answer_state"]) for c in first["search"]["probes"]] == [
                    ("Scoped knowledge", True, "supported"), ("Nonexistent claim", True, "abstain"),
                    ("Historical knowledge", True, "evidence"),
                ], first
                assert searching.call_count == 3 and all(c.kwargs["refresh"] is False for c in searching.call_args_list)
                keyword_binding = stored_binding()
                # Repeating the unchanged finish reuses the bound checkpoint without searching again.
                again = finish_as(keyword_only)
                assert again["status"] == "ok" and again["search"]["reused"] is True, again
                assert searching.call_count == 3 and stored_binding() == keyword_binding
                # Same Markdown, new mtime: the fingerprint is stale, so the checkpoint is not reused.
                stat = (root / targets[0]).stat()
                os.utime(root / targets[0], ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
                touched = finish_as(keyword_only)
                assert touched["status"] == "ok" and touched["search"]["reused"] is False, touched
                assert searching.call_count == 6 and stored_binding() != keyword_binding
                keyword_binding = stored_binding()

                # Mode change invalidates the checkpoint in both directions; semantic mode keeps its own checks.
                with patch.object(core, "qdrant_index_current", return_value=True), patch.object(
                    core, "qdrant_json", return_value={"result": {"count": 0}}
                ), patch.object(core, "search_vault", side_effect=search) as semantic_search:
                    semantic = finish_as(settings)
                    assert semantic["status"] == "ok" and semantic["search"]["reused"] is False, semantic
                    assert "semantic" not in semantic["search"] and semantic_search.call_count == 3
                assert stored_binding() != keyword_binding
                back = finish_as(keyword_only)
                assert back["status"] == "ok" and back["search"]["reused"] is False, back
                assert searching.call_count == 9 and stored_binding() == keyword_binding

                # Failures stay failures: failed probe, any non-disabled fallback, and source mutation mid-probe.
                def degraded(change: str):
                    def search_with(*args: object, **kwargs: object) -> dict:
                        response = real_search(*args, **kwargs)
                        if change == "probe":
                            response["answer_state"]["state"] = "review_required"
                        elif change == "fallback":
                            response["index"]["fallback_reason"] = "embedding_limit"
                        elif change == "stale":
                            response["index"]["stale"] = True
                        elif change == "mode":
                            response["index"]["search_mode"] = "hybrid"
                        elif change == "forbidden":
                            response["results"].append({**vault_documents(keyword_only)[0], "path": sources[0], "match": "keyword"})
                        else:
                            current = (root / targets[0]).stat()
                            os.utime(root / targets[0], ns=(current.st_atime_ns, current.st_mtime_ns + 10**9))
                        return response

                    return search_with

                for change in ("probe", "fallback", "stale", "mode", "forbidden", "mutation"):
                    plan_path.write_bytes(unchecked)
                    with patch.object(core, "search_vault", side_effect=degraded(change)):
                        failed = finish_as(keyword_only)
                    assert failed["status"] == "blocked", (change, failed)
                    assert (failed["reason"] == "search_probe_failed") == (change in {"probe", "forbidden"}), (change, failed)
                    assert plan_path.read_bytes() == unchecked, change

                # A retired source still in the vault and the cloudflare/hash fail-closed path are not relaxed.
                plan_value = json.loads(unchecked)
                try:
                    finish_search(keyword_only, plan_value, [*vault_documents(keyword_only), {"path": sources[0], "document_hash": "a" * 64}])
                except RuntimeError as exc:
                    assert "retired sources remain in the vault" in str(exc), exc
                else:
                    raise AssertionError("retired source accepted")
            for provider in ("cloudflare", "hash"):
                strict = replace(settings, db_path=legacy_db, embedding_provider=provider)
                with patch.object(core, "qdrant_json", side_effect=RuntimeError("Qdrant down")), patch.object(
                    core, "search_vault", side_effect=AssertionError("semantic modes must not degrade to keyword")
                ):
                    failed = finish_as(strict)
                assert failed["status"] == "blocked" and "compatible existing index" in failed["reason"], failed
                assert plan_path.read_bytes() == unchecked
            assert legacy_db.read_bytes() == legacy_bytes, "keyword-only finish changed stored index metadata"
            assert core.rag_index_meta(replace(settings, db_path=legacy_db))["chunks"] == "42"

            # The real CLI entry point, configured only through the environment.
            absent_db = root / "absent.db"
            plan_path.write_bytes(unchecked)
            before_cli = git("status", "--porcelain=v1")
            process = subprocess.run(
                [sys.executable, "-m", "gateway.cli", "--vault", str(root), "compact-finish", str(plan_path)],
                capture_output=True, text=True, env={
                    **os.environ, "EMBEDDING_PROVIDER": "none", "DB_PATH": str(absent_db), "VAULT_DIR": str(root),
                    "QDRANT_URL": "http://127.0.0.1:1", "CLOUDFLARE_API_TOKEN": "", "CLOUDFLARE_ACCOUNT_ID": "",
                },
            )
            output = json.loads(process.stdout)
            assert process.returncode == 0 and output["status"] == "ok" and output["search"]["semantic"] == "disabled", (process.stderr, output)
            assert not absent_db.exists() and git("status", "--porcelain=v1") == before_cli
            assert settings.db_path.read_bytes() == b""
        try:
            staging_path(root, root / "outside.json")
        except ValueError:
            pass
        else:
            raise AssertionError("review checkpoint escaped staging")
    print("compaction workflow: review reuse/invalidation, quota retry, single index/search and no Markdown mutation passed")


def provenance_checks() -> None:
    """compact-annotate writes the only provenance markers; check_compaction rejects markers no review backs."""
    with TemporaryDirectory(prefix="pvg-provenance-") as temporary:
        root = Path(temporary)
        staging = root / ".tmp/curating"
        staging.mkdir(parents=True)
        plan_path, draft_path = staging / "plan.json", staging / "cur.md"
        source, target, other = "30_Conversations/raw/2026/01/01/s.md", "20_Projects/Demo/cur.md", "20_Projects/Demo/other.md"
        old = "item:9999999999999999"

        def git(*args: str) -> bytes:
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout

        def note(path: str, identity: str, memory_type: str, body: str) -> None:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(
                f"---\nid: {identity}\nmemory_type: {memory_type}\nreview_state: human_accepted\n"
                f"provenance_mode: human_asserted\ntemporal_state: current\nprojects: [Demo]\nsubject_id: demo\n---\n\n{body}\n",
                encoding="utf-8",
            )

        retry, timeout = "- Retries stop after 3 attempts.", "- Timeout is 30 seconds."
        events = [retry[2:] + " Repeated filler." * 40, timeout[2:], "Smoke output.", "Covered elsewhere."]
        note(source, "raw_s", "transcript", "\n\n".join(
            f'### user\n\n<!-- pvg-event {{"event_id":"e{index}"}} -->\n\n{text}' for index, text in enumerate(events)
        ))
        note(target, "kn_cur", "canonical", f"Old retry policy.\n\n- Retries stop after 5 attempts. <!-- pvg-src: {old} -->\n- Timeout is unknown.")
        note(other, "kn_other", "canonical", "Unrelated knowledge.")
        git("init", "-q", "-b", "main")
        git("config", "user.name", "Provenance Test")
        git("config", "user.email", "test@localhost")
        git("add", "30_Conversations", "20_Projects")
        git("commit", "-qm", "Test base")
        settings = Settings(root, root / "unused.db", "test", embedding_provider="hash")
        plan = build_compaction_plan(vault_documents(settings), before="2026-01-02", revision=git("rev-parse", "HEAD").decode().strip())
        ids = {node["locator"]: node["id"] for node in plan["selected"]["graph"]["nodes"] if node["type"] == "raw_item"}
        plan["review"].update(
            items=[
                {"id": ids["e0"], "disposition": "merge", "targets": [target], "anchors": {target: "Retries stop after 3"}, "reason": "Keeps the retry limit."},
                {"id": ids["e1"], "disposition": "replace", "targets": [target], "anchors": {target: "Timeout is 30"}, "reason": "Fixes the timeout."},
                {"id": ids["e2"], "disposition": "discard", "targets": [], "reason": "Smoke output."},
                {"id": ids["e3"], "disposition": "already-covered", "targets": [other], "reason": "Same meaning."},
            ],
            delete_paths=[source], user_knowledge={"status": "unchanged", "reason": "No new preference."},
            drafts={target: ".tmp/curating/cur.md"},
            probes=[{"query": "Timeout seconds", "bundle": "current", "expect_paths": [target], "answer_state": "supported"}],
        )
        plain = "# Retry policy\n\nConsolidated retry policy.\n\n" + f"{retry} <!-- pvg-src: {old} -->\n{timeout}"
        note(".tmp/curating/cur.md", "kn_cur", "canonical", plain)
        plain_bytes = draft_path.read_bytes()

        def command(name: str, value: dict | None = None) -> dict:
            plan_path.write_text(json.dumps(plan if value is None else value), encoding="utf-8")
            return cli.run(["--vault", str(root), name, str(plan_path)])

        def marker(*item_ids: str) -> str:
            return f"<!-- pvg-src: {' '.join(sorted(item_ids))} -->"

        assert _src_ids("<!-- pvg-src: item:abc -->") == set() and _src_ids(f"x {marker(old, ids['e0'])}") == {old, ids["e0"]}
        annotated = command("compact-annotate")
        assert annotated == {"status": "ok", "annotated": {target: 2}}, annotated
        marked = draft_path.read_text(encoding="utf-8")
        # Only the two anchored lines change; frontmatter, heading and metadata stay byte-identical.
        assert marked == plain_bytes.decode().replace(
            f"{retry} <!-- pvg-src: {old} -->", f"{retry} {marker(old, ids['e0'])}"
        ).replace(timeout, f"{timeout} {marker(ids['e1'])}"), marked
        assert core.document_metadata(target, marked) == core.document_metadata(target, plain_bytes.decode())
        assert command("compact-annotate")["status"] == "ok" and draft_path.read_text(encoding="utf-8") == marked
        reviewed = command("compact-review")
        assert reviewed["status"] == "ok" and reviewed["provenance"] == {"unmarked_items": 0, "examples": []}, reviewed
        # Markers are body text: they count as active characters instead of hiding from the gate.
        assert reviewed["active_characters"]["after"] == len(core.markdown_body(marked)) + len(core.markdown_body((root / other).read_text()))
        assert git("diff", "--name-only") == b"", "annotate/review modified tracked Markdown"

        # A second id on an already marked line merges into the same marker; annotate never removes markers.
        merged = deepcopy(plan)
        merged["review"]["items"][1]["anchors"] = {target: "Retries stop after 3"}
        assert command("compact-annotate", merged)["status"] == "ok"
        union = draft_path.read_text(encoding="utf-8")
        assert union.count("<!-- pvg-src:") == 2 and f"{retry} {marker(old, ids['e0'], ids['e1'])}" in union, union
        assert f"{timeout} {marker(ids['e1'])}" in union

        # Fail closed: no draft changes, even when the first target resolves and the second does not.
        def anchor(quote: str):
            return lambda value: value["review"]["items"][0]["anchors"].update({target: quote})

        def late_failure(value: dict) -> None:
            note(".tmp/curating/other.md", "kn_other", "canonical", "Unrelated knowledge.")
            first = value["review"]["items"][0]
            first["targets"].append(other)
            first["anchors"][other] = "Missing"
            value["review"]["drafts"][other] = ".tmp/curating/other.md"

        cases = {
            "no match": anchor("Nonexistent claim"), "two lines": anchor("- "), "heading": anchor("Retry policy"),
            "frontmatter only": anchor("memory_type"), "empty": anchor("  "),
            "not an object": lambda value: value["review"]["items"][0].update(anchors="Timeout"),
            "unknown id": lambda value: value["review"]["items"][0].update(id="item:" + "0" * 16),
            "discard": lambda value: value["review"]["items"][0].update(disposition="discard"),
            "not a target": lambda value: value["review"]["items"][0].update(anchors={other: "Timeout"}),
            "not a draft": lambda value: value["review"]["items"][0].update(targets=[target, other], anchors={other: "Timeout"}),
            "late failure": late_failure,
        }
        for name, mutate in cases.items():
            draft_path.write_bytes(plain_bytes)
            invalid = deepcopy(plan)
            mutate(invalid)
            blocked = command("compact-annotate", invalid)
            assert blocked["status"] == "blocked" and blocked["reason"].startswith("invalid_annotation"), (name, blocked)
            assert draft_path.read_bytes() == plain_bytes, name
        (root / "outside.json").write_text(json.dumps(plan), encoding="utf-8")
        assert cli.run(["--vault", str(root), "compact-annotate", str(root / "outside.json")])["status"] == "blocked"

        # Line endings are not normalized: a CRLF draft gets the markers and nothing else changes.
        draft_path.write_bytes(plain_bytes.replace(b"\n", b"\r\n"))
        assert command("compact-annotate")["status"] == "ok"
        assert draft_path.read_bytes() == plain_bytes.decode().replace(
            f"{retry} <!-- pvg-src: {old} -->", f"{retry} {marker(old, ids['e0'])}"
        ).replace(timeout, f"{timeout} {marker(ids['e1'])}").replace("\n", "\r\n").encode(), draft_path.read_bytes()

        # Applied tree: markers are indexed text and harmless to metadata and keyword search.
        draft_path.write_bytes(plain_bytes)
        assert command("compact-annotate")["status"] == "ok"
        applied = draft_path.read_text(encoding="utf-8")
        (root / target).write_text(applied, encoding="utf-8")
        (root / source).unlink()
        checked = command("compact-check")
        assert checked["status"] == "ok" and checked["provenance"]["unmarked_items"] == 0, checked
        assert checked["active_characters"]["after"] == len(core.markdown_body(applied)) + len(core.markdown_body((root / other).read_text()))
        with no_network():
            found = core.search_vault(replace(settings, embedding_provider="none"), {"scopes": ["vault-rag"]}, "Timeout is 30 seconds", bundle="current")
        assert target in {result["path"] for result in found["results"]}, found

        plain_other = (root / other).read_text(encoding="utf-8")

        def tampered(live: dict[str, str]) -> dict:
            for path, text in live.items():
                (root / path).write_text(text, encoding="utf-8")
            result = command("compact-check")
            (root / target).write_text(applied, encoding="utf-8")
            (root / other).write_text(plain_other, encoding="utf-8")
            return result

        for name, live, path in (
            ("fabricated id", {target: applied + f"- Extra. {marker('item:0123456789abcdef')}\n"}, target),
            ("discard id", {target: applied.replace(marker(ids["e1"]), marker(ids["e1"], ids["e2"]))}, target),
            ("already-covered id", {target: applied.replace(marker(ids["e1"]), marker(ids["e1"], ids["e3"]))}, target),
            ("wrong target", {other: plain_other + f"\n{marker(ids['e1'])}\n"}, other),
        ):
            blocked = tampered(live)
            assert blocked["status"] == "blocked", (name, blocked)
            assert f"provenance marker without a claim review: {path}" in blocked["errors"], (name, blocked)
        # Missing markers only warn for now; the old base marker alone never errors.
        warned = tampered({target: plain_bytes.decode()})
        assert warned["status"] == "ok" and warned["provenance"] == {
            "unmarked_items": 2, "examples": sorted([ids["e0"], ids["e1"]]),
        }, warned
    print("compaction provenance: annotate, fail-closed anchors, marker enforcement and search harmlessness passed")


def brief_cap_argument_checks() -> None:
    parse = cli.parser().parse_args
    assert parse(["compact-plan"]).brief_cap == 8000 and parse(["compact-plan", "--brief-cap", "5"]).brief_cap == 5
    for command in ("compact-check p", "compact-review p", "compact-finish p"):
        # Later commands default to the cap compact-plan recorded; the flag overrides it.
        assert parse([*command.split()]).brief_cap is None and parse([*command.split(), "--brief-cap", "5"]).brief_cap == 5, command
    recorded = {"selected": {"ledgers": [{"path": "20_Projects/P/DECISIONS.md", "rows": 0}, {"path": "20_Projects/P/BRIEF.md", "cap": 5000}]}}
    assert cli.plan_brief_cap(recorded, Namespace(brief_cap=None)) == 5000
    assert cli.plan_brief_cap(recorded, Namespace(brief_cap=7)) == 7
    assert cli.plan_brief_cap({"selected": {}}, Namespace(brief_cap=None)) == 8000
    for command in ("compact-annotate p", "health"):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                parse([*command.split(), "--brief-cap", "5"])
            except SystemExit:
                continue
        raise AssertionError(f"{command} accepted --brief-cap")


def annotate_checks() -> None:
    """compact-annotate on synthetic plans: claim items only, quotes see visible text only, user rows are tool-counted."""
    with TemporaryDirectory(prefix="pvg-annotate-") as temporary:
        root = Path(temporary)
        (root / ".tmp/curating").mkdir(parents=True)
        source, first, second, third = "30_Conversations/raw/2026/01/01/a.md", "item:" + "1" * 16, "item:" + "2" * 16, "item:" + "3" * 16
        sessions = ("sess:11111111", "sess:22222222")
        plan = {"selected": {"graph": {"nodes": [
            {"id": "source:a", "type": "source_file", "path": source, "session": sessions[0]},
            {"id": "source:b", "type": "source_file", "path": source + ".b", "session": sessions[1]},
            {"id": first, "type": "raw_item", "path": source}, {"id": third, "type": "raw_item", "path": source},
            {"id": second, "type": "raw_item", "path": source + ".b"},
        ]}}, "review": {"items": [], "drafts": {}}}
        topic, observations = "20_Projects/Demo/topic.md", "10_User/OBSERVATIONS.md"
        header = "| id | 날짜 | 유형 | 상태 | 내용 | 근거 | 독립 세션 |"
        fence = "```"
        texts = {
            topic: f"# Topic\n\nReal claim line.\n\n{fence}\nOnly in code.\nReal claim line.\n{fence}\n\n- Kept <!-- pvg-src: item:{'9' * 16} --> note\n",
            observations: f"{header}\n|---|---|---|---|---|---|---|\n| U-001 | 2026-01-01 | 선호(명시) | 확인 | Wants Korean | 근거 | 0 |\n| U-002 | 2026-01-01 | 맥락 | 확인 | Short row | 근거 |\n",
        }
        drafts = {}
        for target, body in texts.items():
            drafts[target] = root / ".tmp/curating" / PurePosixPath(target).name
            plan["review"]["drafts"][target] = drafts[target].relative_to(root).as_posix()

        def reset() -> None:
            for target, body in texts.items():
                kind = "user_ledger" if target == observations else "note"
                drafts[target].write_text(f"---\nid: {kind}\nmemory_type: canonical\nkind: {kind}\n---\n\n{body}", encoding="utf-8")

        def annotate(*items: dict, fresh: bool = True) -> dict[str, int]:
            plan["review"]["items"] = [{"reason": "x", **entry} for entry in items]
            if fresh:
                reset()
            return annotate_drafts(root, plan)

        def refused(*items: dict, text: str = "") -> None:
            reset()
            before = {path: path.read_bytes() for path in drafts.values()}
            try:
                annotate(*items, fresh=False)
            except ValueError as exc:
                assert text in str(exc), (items, exc)
            else:
                raise AssertionError(("annotate accepted", items))
            assert before == {path: path.read_bytes() for path in drafts.values()}, items

        claim = {"id": first, "disposition": "knowledge", "targets": [topic]}
        # Fenced code and existing markers are invisible to the quote match; a quote seen twice or not at all fails.
        assert annotate({**claim, "anchors": {topic: "Real claim"}}) == {topic: 1}
        marked = drafts[topic].read_text(encoding="utf-8")
        assert f"Real claim line. <!-- pvg-src: {first} -->\n\n{fence}\nOnly in code.\nReal claim line.\n{fence}" in marked, marked
        for quote in ("Only in code", "pvg-src", "9999", "Real claim line.\n", "Absent"):
            refused({**claim, "anchors": {topic: quote}}, text="anchor must match exactly one")
        assert annotate({**claim, "anchors": {topic: "Kept"}}) == {topic: 1}
        assert f"- Kept  note <!-- pvg-src: {first} item:{'9' * 16} -->" in drafts[topic].read_text(encoding="utf-8")
        # A list of quotes marks several lines with one item; markers merge, and a second run changes nothing.
        both = {**claim, "anchors": {topic: ["Real claim", "Kept"]}}
        assert annotate(both) == {topic: 2}
        once = drafts[topic].read_bytes()
        assert annotate(both, fresh=False) == {topic: 2} and drafts[topic].read_bytes() == once
        # Only claim dispositions may anchor; a malformed anchor is refused before anything is written.
        for disposition in ("discard", "already-covered", "hold"):
            refused({**claim, "disposition": disposition, "anchors": {topic: "Real claim"}}, text="anchors require a claim item")
        for anchors in ({topic: [""]}, {topic: []}, {topic: ["Real claim", " "]}, {topic: 5}, {topic: ["Real claim", 5]}, ["Real claim"], {observations: "x"}):
            refused({**claim, "anchors": anchors}, text="anchors require a claim item")
        for disposition in ("decision", "supersession", "user_context", "merge", "replace"):
            assert annotate({**claim, "disposition": disposition, "anchors": {topic: "Real claim"}}) == {topic: 1}

        # A user_ledger row is rewritten by the tool: one sess: token per source session and the 독립 세션 cell as their count.
        pref = lambda item_id, quote="Wants Korean": {"id": item_id, "disposition": "user_preference", "targets": [observations], "anchors": {observations: quote}}
        assert annotate(pref(first), pref(second)) == {observations: 1}
        row = next(line for line in drafts[observations].read_text(encoding="utf-8").splitlines() if "Wants Korean" in line)
        assert row == f"| U-001 | 2026-01-01 | 선호(명시) | 확인 | Wants Korean | 근거 | 2 <!-- pvg-src: {first} {second} {' '.join(sessions)} --> |", row
        annotated = drafts[observations].read_bytes()
        assert annotate(pref(first), pref(second), fresh=False) == {observations: 1} and drafts[observations].read_bytes() == annotated
        assert annotate(pref(first), pref(third)) == {observations: 1}  # two items of one session count once
        assert f"| 1 <!-- pvg-src: {first} {third} {sessions[0]} --> |" in drafts[observations].read_text(encoding="utf-8")
        assert annotate(pref(second), pref(third, "Wants Korean")) == {observations: 1}
        assert f"| 2 <!-- pvg-src: {second} {third} {' '.join(sessions)} --> |" in drafts[observations].read_text(encoding="utf-8")
        for quote in ("Short row", "| id |", "---", "독립 세션"):
            refused(pref(first, quote), text="user_ledger anchor must be a 7-cell U-### row")
        refused(pref(first, "Missing"), text="anchor must match exactly one")
        # All or nothing: a failing user row leaves the topic draft untouched, however the items are ordered.
        refused(
            {**claim, "targets": [topic, observations], "anchors": {topic: "Real claim", observations: "Short row"}},
            text="user_ledger anchor must be a 7-cell U-### row",
        )
    print("compaction annotate: claim-only anchors, visible-text matching, quote lists and tool-counted user rows passed")


def ledger_workflow_checks() -> None:
    """Typed review end to end on a Git vault: two curations (create, then supersede/confirm), CLI flags and mutations."""
    with TemporaryDirectory(prefix="pvg-ledger-") as temporary:
        root = Path(temporary)
        staging = root / ".tmp/curating"
        staging.mkdir(parents=True)
        decisions, brief, topic = "20_Projects/Demo/DECISIONS.md", "20_Projects/Demo/BRIEF.md", "20_Projects/Demo/topic.md"
        observations, profile = "10_User/OBSERVATIONS.md", "10_User/PROFILE.md"
        kinds = {decisions: "decision_ledger", brief: "brief", topic: "note", observations: "user_ledger", profile: "user_profile"}
        raw_paths = {name: f"30_Conversations/raw/2026/01/0{day}/{name}.md" for day, name in enumerate(("r1", "r2", "r3", "r4"), 1)}

        def git(*args: str) -> bytes:
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout

        def text_of(path: str, body: str, **extra: str) -> str:
            front = {"id": path.replace("/", "_").replace(".", "_"), "memory_type": "canonical", "review_state": "human_accepted",
                     "provenance_mode": "human_asserted", "temporal_state": "current", "retrieval_tier": "primary",
                     **({"kind": kinds[path]} if path in kinds else {}), **({"projects": '["Demo"]'} if path.startswith("20_") else {}), **extra}
            return "---\n" + "".join(f"{key}: {value}\n" for key, value in front.items()) + f"---\n\n{body}\n"

        def capture(name: str, session: str, *events: tuple[str, str, object]) -> None:
            parts = []
            for event_id, role, content in events:
                heading, text = role, content
                if role == "subagent":
                    heading, text = "subagent: reviewer", f"**Agent delegation (not a user statement)**\n\n{content[0]}\n\n**Result**\n\n{content[1]}"
                parts.append(f"### {heading}\n\n<!-- pvg-event {json.dumps({'event_id': event_id, 'role': role})} -->\n\n{text}")
            path = root / raw_paths[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text_of(raw_paths[name], "\n\n".join(parts), session_id=session, projects='["Demo"]', memory_type="transcript"), encoding="utf-8")

        filler = "x " * 2500
        capture("r1", "s1", ("e0", "user", "We will keep SQLite as the storage engine."), ("e1", "user", "Postpone the caching layer until traffic grows."),
                ("e2", "user", "I want answers in Korean."), ("f", "user", filler))
        capture("r2", "s2", ("e4", "user", "Please answer in Korean again."), ("e6", "user", "What about the retry budget?"),
                ("e8", "user", "Reusable tip: vacuum SQLite after bulk deletes."))
        capture("r3", "s3", ("e9", "user", "Replace SQLite with Postgres for storage."), ("e10", "user", "Korean answers please, always."), ("g", "user", filler))
        capture("r4", "s4", ("sa", "subagent", ("Check the schema.", "The schema is fine.")))
        (root / topic).parent.mkdir(parents=True, exist_ok=True)
        (root / topic).write_text(text_of(topic, "Old topic notes."), encoding="utf-8")
        git("init", "-q", "-b", "main")
        git("config", "user.name", "Ledger Test")
        git("config", "user.email", "test@localhost")
        git("add", "30_Conversations", "20_Projects")
        git("commit", "-qm", "Base")
        plan_path = staging / "plan.json"

        def run(*argv: str) -> dict:
            with patch.object(cli, "qdrant_compaction_neighbors", return_value={"status": "unavailable", "edges": []}):
                return cli.run(["--vault", str(root), *argv])

        def command(name: str, *extra: str) -> dict:
            return run(name, str(plan_path), *extra)

        def stage(plan: dict, drafts: dict[str, str]) -> dict:
            plan["review"]["drafts"] = {path: f".tmp/curating/{PurePosixPath(path).name}" for path in drafts}
            for path, body in drafts.items():
                (root / plan["review"]["drafts"][path]).write_text(text_of(path, body), encoding="utf-8")
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            return plan

        def review(plan: dict, *rows: dict, deleted: list[str]) -> None:
            ids = {node["locator"]: node["id"] for node in plan["selected"]["graph"]["nodes"] if node["type"] == "raw_item"}
            named = {row["id"] for row in rows}
            plan["review"].update(
                items=[{**row, "id": ids[row["id"]], "reason": "Reviewed."} for row in rows]
                + [{"id": node, "disposition": "discard", "targets": [], "reason": "No lasting value."} for loc, node in ids.items() if loc not in named],
                delete_paths=deleted, user_knowledge={"status": "updated", "reason": "The profile and observations changed."},
                probes=[{"query": "SQLite storage", "bundle": "current", "expect_paths": [brief], "answer_state": "supported"}],
            )

        # --- Curation 1: everything is created from nothing; plan preview, --brief-cap and --full.
        preview = run("compact-plan", "--before", "2026-01-03", "--output", str(plan_path))
        selected = preview["selected"]
        assert {entry["kind"]: entry["create"] for entry in selected["ledgers"]} == dict.fromkeys(("brief", "decision_ledger", "user_profile", "user_ledger"), True)
        assert selected["ledgers"][0]["cap"] == 8000 and selected["subagent_items"] == 0
        assert sorted(selected["create_targets"]) == sorted([brief, decisions, profile, observations])
        assert set(selected["create_targets"]) <= set(selected["target_candidates"])
        assert "graph" not in selected and "review" not in preview
        wide = run("compact-plan", "--before", "2026-01-03", "--brief-cap", "5000", "--output", str(staging / "wide.json"))
        assert wide["selected"]["ledgers"][0]["cap"] == 5000 and wide["selected"]["ledgers"][2]["cap"] == 5000
        assert json.loads((staging / "wide.json").read_text(encoding="utf-8"))["selected"]["ledgers"][0]["cap"] == 5000
        for argv in (("--brief-cap", "0"), ("--brief-cap", "-1")):
            refused = run("compact-plan", "--before", "2026-01-03", *argv)
            assert refused["status"] == "blocked" and refused["reason"].startswith("invalid_compaction_plan"), refused
        full = run("compact-plan", "--before", "2026-01-03", "--full")
        assert full["selected"]["graph"]["nodes"] and full["review"]["items"]
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan["selected"]["source_paths"] == [raw_paths["r1"], raw_paths["r2"]]
        next_id = {entry["kind"]: entry["next_id"] for entry in plan["selected"]["ledgers"] if "next_id" in entry}
        assert next_id == {"decision_ledger": "D-001", "user_ledger": "U-001"}

        rows = "| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |\n|---|---|---|---|---|---|---|\n"
        decision_body = "# 결정 장부\n\n" + rows + "\n".join([
            "| D-001 | 2026-01-01 | 결정 | 유효 | Keep SQLite as the storage engine | 단순함 | - |",
            "| D-002 | 2026-01-01 | 미룸 | 보류 | Postpone the caching layer | 트래픽 적음 | - |",
            "| D-003 | 2026-01-02 | 질문 | 유효 | What about the retry budget? | 미정 | - |",
        ])
        brief_body = "\n\n".join([
            "# Demo 브리프", "## 현재 목표\n\nSQLite 기반 저장소를 유지한다.",
            "## 유효한 결정\n\n| id | 날짜 | 결정 | 이유 |\n|---|---|---|---|\n| D-001 | 2026-01-01 | Keep SQLite storage | 단순함 |",
            "## 미뤄진 것\n\n| id | 무엇 | 왜 | 재개 조건 |\n|---|---|---|---|\n| D-002 | Caching layer postponed | 트래픽 적음 | 트래픽 증가 |",
            "## 대체된 것\n\n| 원래(id) | → 지금(id) | 언제·왜 |\n|---|---|---|", "## 열린 질문\n\n- D-003 Open retry budget question",
            "## 최근 변화\n\n- 2026-01-02 첫 정리",
        ])
        observation_body = "# 관찰 장부\n\n| id | 날짜 | 유형 | 상태 | 내용 | 근거 | 독립 세션 |\n|---|---|---|---|---|---|---|\n" + \
            "| U-001 | 2026-01-01 | 선호(추론) | 가설 | Wants answers in Korean | 두 세션 | 0 |"
        profile_body = "\n\n".join(["# 프로필", *(f"## {name}\n\n-" for name in ("역할·맥락", "확인된 선호")), "## 가설\n\n- Likely wants Korean answers (U-001)",
                                    *(f"## {name}\n\n-" for name in ("제약", "에이전트에 준 피드백", "반례·철회", "최근 변화"))])
        topic_body = "Old topic notes.\n\n- Vacuum SQLite after bulk deletes."
        drafts = {decisions: decision_body, brief: brief_body, observations: observation_body, profile: profile_body, topic: topic_body}
        both = lambda row: {"disposition": row, "targets": [decisions, brief]}
        review(
            plan,
            {"id": "e0", **both("decision"), "quote": "keep SQLite as the storage engine", "entry": {"id": "D-001", "type": "결정", "status": "유효"},
             "anchors": {decisions: "Keep SQLite as the storage engine", brief: "Keep SQLite storage"}},
            {"id": "e1", **both("deferral"), "quote": "Postpone the caching layer", "anchors": {decisions: "Postpone the caching layer", brief: "Caching layer postponed"}},
            {"id": "e6", **both("question"), "quote": "retry budget", "anchors": {decisions: "retry budget", brief: "retry budget"}},
            *({"id": item, "disposition": "user_preference", "targets": [observations, profile], "quote": quote,
               "anchors": {observations: "Wants answers in Korean", profile: "Likely wants Korean answers"}}
              for item, quote in (("e2", "answers in Korean"), ("e4", "answer in Korean again"))),
            {"id": "e8", "disposition": "knowledge", "targets": [topic], "quote": "vacuum SQLite after bulk deletes", "anchors": {topic: "Vacuum SQLite after bulk deletes"}},
            deleted=[raw_paths["r1"], raw_paths["r2"]],
        )
        stage(plan, drafts)
        plain = {path: (root / plan["review"]["drafts"][path]).read_bytes() for path in drafts}
        # Without annotation every typed item is missing its marker: an error in a curator document.
        unmarked = command("compact-review")
        assert unmarked["status"] == "blocked", unmarked
        for path in (decisions, brief, observations, profile):
            assert f"claim without provenance marker: {path}" in unmarked["errors"], unmarked["errors"]
        assert f"claim without provenance marker: {topic}" not in unmarked["errors"] and unmarked["provenance"]["unmarked_items"] == 1
        assert command("compact-annotate") == {"status": "ok", "annotated": {decisions: 3, brief: 3, observations: 1, profile: 1, topic: 1}}
        annotated = {path: (root / plan["review"]["drafts"][path]).read_bytes() for path in drafts}
        text = annotated[decisions].decode()
        assert re.search(r"\| D-001 \|.*\| - <!-- pvg-src: item:[0-9a-f]{16} --> \|\n", text), text
        row = next(line for line in annotated[observations].decode().splitlines() if "Wants answers" in line)
        assert row.count("sess:") == 2 and "| 2 <!-- pvg-src: item:" in row and row.endswith("--> |"), row
        assert command("compact-annotate")["status"] == "ok" and annotated == {path: (root / plan["review"]["drafts"][path]).read_bytes() for path in drafts}
        # CRLF drafts keep their line endings; a list of quotes marks several rows with one item.
        draft_files = {path: root / plan["review"]["drafts"][path] for path in drafts}
        draft_files[decisions].write_bytes(plain[decisions].replace(b"\n", b"\r\n"))
        assert command("compact-annotate")["status"] == "ok"
        assert draft_files[decisions].read_bytes() == annotated[decisions].replace(b"\n", b"\r\n")
        draft_files[decisions].write_bytes(plain[decisions])
        listed_plan = deepcopy(plan)
        listed_plan["review"]["items"][0]["anchors"][decisions] = ["Keep SQLite as the storage engine", "Postpone the caching layer"]
        plan_path.write_text(json.dumps(listed_plan), encoding="utf-8")
        assert command("compact-annotate")["annotated"][decisions] == 3
        assert draft_files[decisions].read_text(encoding="utf-8").count(listed_plan["review"]["items"][0]["id"]) == 2
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        draft_files[decisions].write_bytes(plain[decisions])
        assert command("compact-annotate")["status"] == "ok"

        reviewed = command("compact-review")
        assert reviewed["status"] == "ok" and reviewed["warnings"] == [] and reviewed["provenance"] == {"unmarked_items": 0, "examples": []}, reviewed
        curation = reviewed["curation"]
        assert curation["brief_cap"] == 8000 and set(curation["brief_chars"]) == {brief, profile}
        assert curation["ledger_rows"] == {decisions: 3, observations: 1} and curation["superseded_rows"] == {decisions: 0}
        over = command("compact-review", "--brief-cap", str(curation["brief_chars"][brief] - 1))
        assert over["status"] == "blocked" and f"brief over the cap: {brief}" in " ".join(over["errors"]), over
        assert command("compact-review", "--brief-cap", str(max(curation["brief_chars"].values())))["status"] == "ok"
        assert command("compact-review", "--brief-cap", "0")["reason"].startswith("invalid_compaction_check")

        def blocked(expected: str, mutate) -> None:
            """Mutate the plan (a callable) or drafts ({path: text transform}); compact-review must block with this error."""
            saved_plan = plan_path.read_bytes()
            saved = {path: file.read_bytes() for path, file in draft_files.items()}
            try:
                if callable(mutate):
                    changed = json.loads(saved_plan)
                    mutate(changed)
                    plan_path.write_text(json.dumps(changed), encoding="utf-8")
                else:
                    for path, edit in mutate.items():
                        draft_files[path].write_text(edit(draft_files[path].read_text(encoding="utf-8")), encoding="utf-8")
                result = command("compact-review")
            finally:
                plan_path.write_bytes(saved_plan)
                for path, content in saved.items():
                    draft_files[path].write_bytes(content)
            assert result["status"] == "blocked" and any(expected in error for error in result.get("errors", [result.get("reason", "")])), (expected, result)

        first_item = lambda value: value["review"]["items"][0]
        blocked("quote not found in item", lambda value: first_item(value).update(quote="keep Postgres as the storage engine"))
        blocked("claim item requires a quote", lambda value: first_item(value).pop("quote"))
        blocked("claim items cannot be grouped under ids: decision", lambda value: first_item(value).update(ids=[first_item(value).pop("id")]))
        blocked(f"project claim must target a brief or decision_ledger: {topic}", lambda value: first_item(value)["targets"].append(topic))
        blocked("ledger 확인 needs 3 independent sessions", {observations: lambda body: body.replace("| 가설 |", "| 확인 |")})
        blocked("ledger 독립 세션 differs from the tool count", {observations: lambda body: body.replace("| 2 <!--", "| 3 <!--")})
        blocked(f"brief headings missing or out of order: {brief}", {brief: lambda body: body.replace("## 미뤄진 것", "## Later")})
        blocked(f"brief 유효한 결정 lists a non-valid entry: {brief}: D-002", {brief: lambda body: body.replace("| D-001 | 2026-01-01 | Keep SQLite storage", "| D-002 | 2026-01-01 | Keep SQLite storage")})
        blocked(f"profile 확인된 선호 lists a non-confirmed entry: {profile}: U-001", {profile: lambda body: body.replace("## 확인된 선호\n\n-", "## 확인된 선호\n\n| U-001 | x |\n|---|---|")})
        blocked("ledger 날짜 invalid", {decisions: lambda body: body.replace("2026-01-02", "yesterday")})
        blocked(f"doc source invalid: {decisions}: doc:20_Projects/Demo/missing.md", {decisions: lambda body: re.sub(r"(retry budget\? \| 미정 \| - )<!-- pvg-src: item:[0-9a-f]{16} -->", r"\1<!-- pvg-src: doc:20_Projects/Demo/missing.md -->", body)})

        # A changed typed item (here: the proposed entry) invalidates exactly that item's recorded review.
        recorded = command("compact-review", "--record")
        assert recorded["semantic_review"] == "recorded" and command("compact-review")["semantic_review"] == "reusable"
        saved_plan = plan_path.read_bytes()
        changed = json.loads(saved_plan)
        first_item(changed)["entry"] = {"id": "D-001", "type": "결정", "status": "보류"}
        plan_path.write_text(json.dumps(changed), encoding="utf-8")
        assert command("compact-review")["review_binding"]["review_required_ids"] == [first_item(changed)["id"]]
        plan_path.write_bytes(saved_plan)
        for path, file in draft_files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(file.read_bytes())
        for name in ("r1", "r2"):
            (root / raw_paths[name]).unlink()
        applied = command("compact-check")
        assert applied["status"] == "ok" and applied["curation"] == curation and applied["provenance"]["unmarked_items"] == 0, applied
        assert command("compact-check", "--brief-cap", "50")["status"] == "blocked"
        status = {line[3:] for line in git("status", "--porcelain=v1", "-uall").decode().splitlines() if ".tmp/" not in line}
        assert status == set(kinds) | {raw_paths["r1"], raw_paths["r2"]}, status
        git("add", "30_Conversations", "20_Projects", "10_User")
        git("commit", "-qm", "Curation 1")

        # --- Curation 2 (on the applied result): a supersession flips a base row, a third session confirms the inference.
        plan_path = staging / "plan2.json"
        preview = run("compact-plan", "--before", "2026-02-01", "--output", str(plan_path))["selected"]
        assert preview["subagent_items"] == 2 and preview["create_targets"] == [] and preview["source_paths"] == [raw_paths["r3"], raw_paths["r4"]]
        assert [(entry["create"], entry["adopt"]) for entry in preview["ledgers"]] == [(False, False)] * 4 and not any("template" in entry for entry in preview["ledgers"])
        assert {entry["kind"]: entry["next_id"] for entry in preview["ledgers"] if "next_id" in entry} == {"decision_ledger": "D-004", "user_ledger": "U-002"}
        assert {entry["kind"]: entry["rows"] for entry in preview["ledgers"] if "rows" in entry} == {"decision_ledger": 3, "user_ledger": 1}
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        ids = {node["locator"]: node["id"] for node in plan["selected"]["graph"]["nodes"] if node["type"] == "raw_item"}
        live = lambda path: core.markdown_body((root / path).read_text(encoding="utf-8"))
        decision_body = live(decisions).replace("| 유효 | Keep SQLite as the storage engine | 단순함 | -", "| 대체됨 | Keep SQLite as the storage engine | 단순함 | D-004").rstrip("\n") + \
            "\n| D-004 | 2026-01-03 | 대체 | 유효 | Replace SQLite with Postgres for storage | 교체 | - |"
        brief_body = "\n\n".join([
            "# Demo 브리프", "## 현재 목표\n\nPostgres 기반 저장소로 전환한다. (D-004)",
            "## 유효한 결정\n\n| id | 날짜 | 결정 | 이유 |\n|---|---|---|---|\n| D-004 | 2026-01-03 | Use Postgres storage | 교체 |",
            "## 미뤄진 것\n\n| id | 무엇 | 왜 | 재개 조건 |\n|---|---|---|---|\n| D-002 | Caching layer postponed | 트래픽 적음 | 트래픽 증가 |",
            "## 대체된 것\n\n| 원래(id) | → 지금(id) | 언제·왜 |\n|---|---|---|\n| D-001 | → D-004 | Replaced SQLite storage on 2026-01-03 |",
            "## 열린 질문\n\n- D-003 Open retry budget question", "## 최근 변화\n\n- 2026-01-03 저장소 교체",
        ])
        profile_body = profile_body.replace("## 확인된 선호\n\n-", "## 확인된 선호\n\n| id | 날짜 | 선호 | 근거 |\n|---|---|---|---|\n| U-001 | 2026-01-03 | Wants answers in Korean | 세 세션 |") \
            .replace("- Likely wants Korean answers (U-001)", "-")
        drafts = {decisions: decision_body, brief: brief_body, observations: live(observations).replace("| 가설 |", "| 확인 |"), profile: profile_body}
        review(
            plan,
            {"id": "e9", "disposition": "supersession", "targets": [decisions, brief], "quote": "Replace SQLite with Postgres",
             "anchors": {decisions: ["Replace SQLite with Postgres for storage", "Keep SQLite as the storage engine"], brief: ["Use Postgres storage", "Replaced SQLite storage"]}},
            {"id": "e10", "disposition": "user_preference", "targets": [observations, profile], "quote": "Korean answers please",
             "anchors": {observations: "Wants answers in Korean", profile: "Wants answers in Korean"}},
            {"id": "sa#result", "disposition": "already-covered", "targets": [topic]},
            deleted=[raw_paths["r3"], raw_paths["r4"]],
        )
        stage(plan, drafts)
        draft_files = {path: root / plan["review"]["drafts"][path] for path in drafts}
        assert command("compact-annotate")["annotated"] == {decisions: 2, brief: 2, observations: 1, profile: 1}
        annotated = {path: file.read_bytes() for path, file in draft_files.items()}
        row = next(line for line in annotated[observations].decode().splitlines() if "Wants answers" in line)
        assert "| 확인 |" in row and "| 3 <!-- pvg-src:" in row and row.count("sess:") == 3 and row.count("item:") == 3, row
        reviewed = command("compact-review")
        assert reviewed["status"] == "ok" and reviewed["warnings"] == [], reviewed
        assert reviewed["curation"]["ledger_rows"] == {decisions: 4, observations: 1} and reviewed["curation"]["superseded_rows"] == {decisions: 1}

        item_of = lambda value, locator: next(entry for entry in value["review"]["items"] if entry["id"] == ids[locator])
        blocked("subagent item may only be knowledge, already-covered, discard or hold", lambda value: item_of(value, "sa#result").update(disposition="decision", targets=[decisions], quote="schema is fine"))
        blocked("quote not found in item", lambda value: item_of(value, "e9").update(quote="Replace Postgres with SQLite"))
        blocked(f"ledger row deleted: {decisions}: D-003", {decisions: lambda body: re.sub(r"\| D-003 [^\n]*\n", "", body)})
        blocked(f"ledger row changed outside its mutable columns: {decisions}: D-002", {decisions: lambda body: body.replace("Postpone the caching layer", "Postpone caching")})
        blocked(f"ledger 대체됨 requires 후속: {decisions}: D-001", {decisions: lambda body: re.sub(r"\| D-004( <!-- pvg-src: item:[0-9a-f]{16} item:[0-9a-f]{16} -->) \|", r"| -\1 |", body, count=1)})
        blocked(f"ledger row lost provenance: {decisions}: D-001", {decisions: lambda body: re.sub(r"(\| D-001 [^\n]*\| D-004 )<!-- pvg-src:[^>]*-->", r"\1<!-- pvg-src: " + ids["e9"] + " -->", body)})
        blocked(f"curator document kind changed: {decisions}", {decisions: lambda body: body.replace("kind: decision_ledger", "kind: note")})
        blocked(f"ledger 독립 세션 differs from the tool count: {observations}: U-001", {observations: lambda body: body.replace("| 3 <!--", "| 4 <!--")})
        blocked(f"ledger 확인 needs 3 independent sessions: {observations}: U-001", {observations: lambda body: re.sub(r" sess:[0-9a-f]{8}", "", body, count=1).replace("| 3 <!--", "| 2 <!--")})
        applied_texts = {path: file.read_bytes() for path, file in draft_files.items()}
        for path, content in applied_texts.items():
            (root / path).write_bytes(content)
        for name in ("r3", "r4"):
            (root / raw_paths[name]).unlink()
        applied = command("compact-check")
        assert applied["status"] == "ok" and applied["warnings"] == [] and applied["provenance"]["unmarked_items"] == 0, applied
        status = {line[3:] for line in git("status", "--porcelain=v1", "-uall").decode().splitlines() if ".tmp/" not in line}
        assert status == {decisions, brief, observations, profile, raw_paths["r3"], raw_paths["r4"]}, status
        assert core.markdown_body(applied_texts[decisions].decode()) == live(decisions)
    print("compaction ledgers: typed review, annotate, tool-counted sessions, supersession, --brief-cap and mutation matrix passed")


def index_lock_checks() -> None:
    if sys.platform == "win32":
        return
    import fcntl

    with TemporaryDirectory(prefix="pvg-index-lock-") as temporary:
        db = Path(temporary) / "gateway.db"
        command = [sys.executable, "-c", (
            "from pathlib import Path; from gateway import core; "
            "core._index_vault = lambda settings: {}; "
            f"core.index_vault(core.Settings(Path({temporary!r}), Path({str(db)!r}), 'test'))"
        )]
        with db.with_suffix(".db.index.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = subprocess.run(command, capture_output=True, text=True)
            assert blocked.returncode != 0 and "another process" in blocked.stderr
        assert subprocess.run(command, capture_output=True).returncode == 0
    print("compaction indexing: cross-process exclusion and lock release passed")


def main() -> None:
    efficiency_checks()
    workflow_checks()
    provenance_checks()
    brief_cap_argument_checks()
    annotate_checks()
    ledger_workflow_checks()
    index_lock_checks()
    disputed = document_metadata("20_Projects/Demo/disputed.md", """---
id: kn_disputed
memory_type: canonical
review_state: human_accepted
temporal_state: current
conflict_state: unresolved
---
Two claims remain unresolved after their raw sources were consolidated.
""")
    assert rag_answer_state([{**disputed, "match": "keyword"}], "current")["state"] == "review_required"
    with TemporaryDirectory(prefix="pvg-compaction-") as temporary:
        root = Path(temporary)

        def git(*args: str) -> bytes:
            return subprocess.run(
                ["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ).stdout

        def note(path: str, identity: str, text: str, memory_type: str = "canonical") -> None:
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                f"---\nid: {identity}\nmemory_type: {memory_type}\nreview_state: human_accepted\n"
                f"temporal_state: current\nprojects: [Demo]\nsubject_id: demo.subject\n---\n\n{text}\n",
                encoding="utf-8",
            )

        first = "30_Conversations/raw/2026/01/01/a.md"
        held = "30_Conversations/raw/2026/01/01/b.md"
        target = "20_Projects/Demo/current.md"
        profile = "10_User/PROFILE.md"
        note(first, "raw_a", "Repeated evidence. " * 100, "transcript")
        note(held, "raw_b", "Ambiguous attribution. " * 40, "transcript")
        note(target, "kn_demo", "Old project knowledge.")
        note(profile, "kn_user", "Contextual preferences, not instructions.")
        git("init", "-q", "-b", "main")
        git("config", "user.name", "Compaction Test")
        git("config", "user.email", "test@localhost")
        git("add", ".")
        git("commit", "-qm", "Test base")
        settings = Settings(root, root / "unused.db", "test", embedding_provider="hash")
        documents = vault_documents(settings)
        real_run = subprocess.run
        with patch.object(cli.subprocess, "run", wraps=real_run) as calls:
            base_documents = cli.documents_at_revision(root, git("rev-parse", "HEAD").decode().strip())
        assert {doc["path"]: doc["document_hash"] for doc in base_documents} == {
            doc["path"]: doc["document_hash"] for doc in documents
        }
        commands = [call.args[0] for call in calls.call_args_list]
        assert sum("cat-file" in command for command in commands) == 1
        assert not any("show" in command for command in commands)
        hashes = {doc["path"]: doc["document_hash"] for doc in documents}
        deferrals = [{
            "path": first, "sha256": hashes[first], "reason": "Attribution needs a source.",
            "revisit_when": "Source or related profile changes, or an explicit clarification arrives.",
            "dependencies": {profile: hashes[profile]},
        }]
        args = {"before": "2026-01-02", "tracked_paths": set(hashes), "deferrals": deferrals}
        deferred = build_compaction_plan(documents, **args)
        assert deferred["selected"]["source_paths"] == [held]
        assert deferred["deferred"] == deferrals
        all_deferred = build_compaction_plan(documents, **{
            **args, "deferrals": deferrals + [{**deferrals[0], "path": held, "sha256": hashes[held]}],
        })
        assert all_deferred["status"] == "deferred" and all_deferred["selected"] is None
        for changed_path in (first, profile):
            changed = [{**doc, "document_hash": "f" * 64} if doc["path"] == changed_path else doc for doc in documents]
            invalidated = build_compaction_plan(changed, **args)
            assert invalidated["invalidated_deferrals"] == [first]
            assert first in invalidated["selected"]["source_paths"]
        assert build_compaction_plan(documents, dirty_paths={target}, **args)["status"] == "blocked"
        try:
            build_compaction_plan(documents, before="2026-01-02", deferrals=[{"path": first}])
        except ValueError:
            pass
        else:
            raise AssertionError("unbound deferral was accepted")

        staging = root / ".tmp/curating"
        staging.mkdir(parents=True)
        plan_path = staging / "plan.json"
        deferred_path = staging / "deferred.json"
        deferred_path.write_text(json.dumps(deferrals), encoding="utf-8")
        with patch.object(cli, "qdrant_compaction_neighbors", return_value={"status": "unavailable", "edges": []}):
            queued = cli.run(["--vault", str(root), "compact-plan", "--before", "2026-01-02", "--deferrals", str(deferred_path)])
            assert queued["selected"]["source_paths"] == [held]
            assert "graph" not in queued["selected"] and "review" not in queued
            preview = cli.run(["--vault", str(root), "compact-plan", "--before", "2026-01-02", "--output", str(plan_path)])
            assert preview["plan_file"] == str(plan_path.resolve())
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            assert cli.run(["--vault", str(root), "compact-plan", "--output", str(plan_path)])["status"] == "blocked"
            assert json.loads(plan_path.read_text(encoding="utf-8")) == plan
            assert cli.run(["--vault", str(root), "compact-plan", "--output", str(root / "other.json")])["status"] == "blocked"
            assert cli.run(["--vault", str(root), "compact-plan", "--max-characters", "0"])["status"] == "blocked"
        # Keyword-only mode plans from the vault alone: the neighbor lookup reports itself disabled, no network.
        with patch.dict(os.environ, {"EMBEDDING_PROVIDER": "none"}), no_network():
            keyword_only_plan = cli.run(["--vault", str(root), "compact-plan", "--before", "2026-01-02"])
        assert keyword_only_plan["selected"]["semantic"] == {"status": "unavailable", "reason": "semantic_disabled"}, keyword_only_plan["selected"]
        items = {node["id"]: node["path"] for node in plan["selected"]["graph"]["nodes"] if node["type"] == "raw_item"}
        for entry in plan["review"]["items"]:
            entry.update(
                disposition="hold" if items[entry["id"]] == held else "merge",
                targets=[] if items[entry["id"]] == held else [target],
                reason="Attribution is unresolved." if items[entry["id"]] == held else "The target preserves the useful result.",
            )
        plan["review"]["delete_paths"] = [first]
        plan["review"]["user_knowledge"] = {"status": "unchanged", "reason": "No new durable preference in this batch."}
        (root / first).unlink()
        note(target, "kn_demo", "Consolidated project knowledge.")
        with patch.object(cli, "qdrant_compaction_neighbors", side_effect=AssertionError("dirty plan must stop before Qdrant")):
            assert cli.run(["--vault", str(root), "compact-plan"])["reason"] == "dirty_markdown"

        def check(value: dict) -> dict:
            plan_path.write_text(json.dumps(value), encoding="utf-8")
            status = git("status", "--porcelain=v1")
            with patch.object(cli, "qdrant_compaction_neighbors", side_effect=AssertionError("check must not contact Qdrant")):
                result = cli.run(["--vault", str(root), "compact-check", str(plan_path)])
            assert git("status", "--porcelain=v1") == status
            assert not settings.db_path.exists()
            return result

        result = check(plan)
        assert result["status"] == "ok", result
        assert result["active_characters"]["delta"] < 0
        assert result["retained_sources"] == [held]
        assert result["approval"] == "not_checked" and result["semantic_review"] == "required"
        assert result["index_verification"] == "not_checked"
        assert result["integrity"]["new_errors"] == []
        assert result["provenance"]["unmarked_items"] == 1  # unanchored legacy plans still pass; markers are a warning for now
        grouped = deepcopy(plan)
        for entry in grouped["review"]["items"]:
            entry["ids"] = [entry.pop("id")]
        assert check(grouped)["status"] == "ok"
        shared = deepcopy(plan)
        shared["review"]["items"] = [{
            "ids": [entry["id"] for entry in plan["review"]["items"]],
            "disposition": "already-covered", "targets": [target], "reason": "Shared disposition test.",
        }]
        assert check(shared)["status"] == "ok"
        grouped["review"]["items"][0]["ids"] *= 2
        assert check(grouped)["status"] == "blocked"
        del grouped["review"]["items"][0]["ids"]
        assert check(grouped)["status"] == "blocked"
        after = vault_documents(settings)
        for field, value, expected in (
            ("derived_from", ["raw_a"], "broken_reference"),
            ("derived_from", ["kn_demo"], "provenance_cycle"),
            ("metadata_valid", False, "invalid_metadata"),
        ):
            invalid = [{**doc, field: value} if doc["path"] == target else doc for doc in after]
            checked = check_compaction(plan, documents, invalid)
            assert checked["status"] == "blocked"
            assert any(error["code"] == expected for error in checked["integrity"]["new_errors"]), checked
        conflicting = [{**doc, "relations": {"contradicts": ["kn_user"]}} if doc["path"] == target else doc for doc in after]
        checked = check_compaction(plan, documents, conflicting)
        assert checked["integrity"]["unresolved_conflicts_after"] == 1
        assert len(checked["integrity"]["new_conflict_ids"]) == 1
        if sys.platform != "win32":
            (root / first).symlink_to(root / held)
            assert check(plan)["status"] == "blocked"
            (root / first).unlink()
        for mutation in ("missing_item", "duplicate_item", "held_deletion", "fake_hash", "missing_target", "pending_profile"):
            invalid = deepcopy(plan)
            reviewed = invalid["review"]["items"]
            merged = next(item for item in reviewed if item["disposition"] == "merge")
            if mutation == "missing_item":
                reviewed.pop()
            elif mutation == "duplicate_item":
                reviewed.append(deepcopy(reviewed[0]))
            elif mutation == "held_deletion":
                merged["disposition"] = "hold"
            elif mutation == "fake_hash":
                next(node for node in invalid["selected"]["graph"]["nodes"] if node["type"] == "source_file")["sha256"] = "0" * 64
            elif mutation == "missing_target":
                merged["targets"] = ["20_Projects/missing.md"]
            else:
                invalid["review"]["user_knowledge"]["status"] = "pending"
            assert check(invalid)["status"] == "blocked", mutation

        note(target, "kn_demo", "Uncompressed repetition. " * 300)
        assert "active Markdown characters did not decrease" in check(plan)["errors"]
        note(target, "kn_demo", "Consolidated project knowledge.")
        note("50_Knowledge/unplanned.md", "kn_extra", "Unexpected change")
        assert check(plan)["status"] == "blocked"
        (root / "50_Knowledge/unplanned.md").unlink()
        note(profile, "kn_user", "New evidence narrows the preference to design discussions.")
        assert check(plan)["status"] == "blocked"
        next(item for item in plan["review"]["items"] if item["disposition"] == "merge")["targets"].append(profile)
        plan["review"]["user_knowledge"] = {"status": "updated", "reason": "The profile now preserves the scope and counterexample."}
        assert check(plan)["status"] == "ok"
        git("add", "-u")
        git("commit", "-qm", "Applied test compaction")
        assert check(plan)["reason"] == "plan_base_changed"
        process = subprocess.run(
            [sys.executable, "-m", "gateway.cli", "--vault", str(root), "compact-check", str(plan_path)],
            capture_output=True, text=True,
        )
        assert process.returncode == 1 and json.loads(process.stdout)["reason"] == "plan_base_changed"
    print("compaction: deferral invalidation, coverage, retention, profile review, reduction and read-only Git checks passed")


if __name__ == "__main__":
    main()
