"""Read-only compaction planning/checking against disposable Git repositories."""

from __future__ import annotations

import contextlib
from copy import deepcopy
from argparse import Namespace
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import cli, core
from gateway.compaction import concurrent_raw_captures, finish_search, review_binding, staging_path
from gateway.core import Settings, document_metadata, rag_answer_state, vault_documents
from gateway.wiki import build_compaction_plan, check_compaction


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
