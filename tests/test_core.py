from __future__ import annotations

import asyncio
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from starlette.requests import Request

import gateway.app as app_module
import gateway.core as core
from gateway.core import (
    Settings,
    comma_list,
    disable_agent,
    document_metadata,
    generate_token,
    init_db,
    list_agents,
    load_working_agreement,
    lookup_agent,
    rag_readiness,
    save_agent_note,
    save_conversation,
    search_vault,
    target_path,
    upsert_agent,
)
from gateway.app import (
    agent_table_html,
    allowed_roots_for_agent,
    app,
    make_admin_cookie,
    scopes_for_permission,
    token_created_html,
    token_form_html,
    valid_admin_cookie,
)


WORKING_AGREEMENT_TEXT = """---
pv_schema: 1
id: user_working_agreement
subject_id: user-agent-working-agreement
memory_type: canonical
kind: procedure
review_state: human_accepted
temporal_state: current
outcome: not_applicable
topics: ["user-preferences"]
provenance_mode: human_asserted
relations: {"supports":[],"contradicts":[],"supersedes":[]}
retrieval_tier: primary
privacy: normal
---
# Working Agreement

Prefer concise status updates.
"""


def qdrant_filter_matches(payload: dict, query_filter: dict | None) -> bool:
    if not query_filter:
        return True
    for condition in query_filter.get("must", []):
        value = payload.get(condition["key"])
        match = condition.get("match", {})
        if "value" in match and value != match["value"]:
            return False
        if "any" in match:
            values = value if isinstance(value, list) else [value]
            if not set(values) & set(match["any"]):
                return False
    return True


def start_fake_qdrant(collection: str) -> tuple[ThreadingHTTPServer, dict] | None:
    state = {"exists": False, "points": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def send_json(self, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_json(self) -> dict:
            size = int(self.headers.get("content-length", "0"))
            return json.loads(self.rfile.read(size).decode() or "{}") if size else {}

        def do_GET(self) -> None:
            if self.path == f"/collections/{collection}/exists":
                self.send_json({"result": {"exists": state["exists"]}})
            else:
                self.send_error(404)

        def do_DELETE(self) -> None:
            if self.path == f"/collections/{collection}":
                state["exists"] = False
                state["points"] = []
                self.send_json({"status": "ok"})
            else:
                self.send_error(404)

        def do_PUT(self) -> None:
            body = self.read_json()
            if self.path == f"/collections/{collection}":
                state["exists"] = True
                state["points"] = []
                self.send_json({"status": "ok"})
            elif self.path == f"/collections/{collection}/points?wait=true":
                incoming = {point["id"]: point for point in body["points"]}
                state["points"] = [point for point in state["points"] if point["id"] not in incoming]
                state["points"].extend(incoming.values())
                self.send_json({"status": "ok"})
            elif self.path == f"/collections/{collection}/points/payload?wait=true":
                selected = set(body["points"])
                for point in state["points"]:
                    if point["id"] in selected:
                        point["payload"] = body["payload"]
                self.send_json({"status": "ok"})
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            body = self.read_json()
            if self.path == f"/collections/{collection}/points/count":
                self.send_json({"result": {"count": len(state["points"])}})
                return
            if self.path == f"/collections/{collection}/points/scroll":
                ordered = sorted(state["points"], key=lambda point: point["id"])
                offset = body.get("offset")
                start = next((index + 1 for index, point in enumerate(ordered) if point["id"] == offset), 0)
                page = ordered[start : start + body.get("limit", 10)]
                selected = body.get("with_payload")
                result = [
                    {
                        "id": point["id"],
                        "payload": {key: point["payload"].get(key) for key in selected}
                        if isinstance(selected, list)
                        else point["payload"],
                    }
                    for point in page
                ]
                next_offset = page[-1]["id"] if start + len(page) < len(ordered) else None
                self.send_json({"result": {"points": result, "next_page_offset": next_offset}})
                return
            if self.path == f"/collections/{collection}/points/delete?wait=true":
                selected = set(body["points"])
                state["points"] = [point for point in state["points"] if point["id"] not in selected]
                self.send_json({"status": "ok"})
                return
            if self.path == f"/collections/{collection}/points/query":
                query = body["query"]
                ranked = []
                for point in state["points"]:
                    if not qdrant_filter_matches(point["payload"], body.get("filter")):
                        continue
                    score = sum(a * b for a, b in zip(query, point["vector"]))
                    ranked.append({"id": point["id"], "score": score, "payload": point["payload"]})
                ranked.sort(key=lambda item: item["score"], reverse=True)
                self.send_json({"result": {"points": ranked[: body.get("limit", 10)]}})
                return
            self.send_error(404)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except (PermissionError, OSError) as exc:
        print(f"skip fake qdrant smoke: {exc}")
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


def run_fake_qdrant_smoke() -> None:
    collection = "persona_test"
    fake_qdrant = start_fake_qdrant(collection)
    if not fake_qdrant:
        return
    server, state = fake_qdrant
    try:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                root / "vault",
                root / "gateway.db",
                "smoke",
                embedding_provider="hash",
                qdrant_url=f"http://127.0.0.1:{server.server_port}",
                qdrant_collection=collection,
            )
            init_db(settings.db_path)
            agent = {"agent_id": "smoke-agent", "scopes": ["vault-rag"], "allowed_roots": []}

            (settings.vault_dir / "50_Knowledge").mkdir(parents=True)
            (settings.vault_dir / "50_Knowledge/rag.md").write_text(
                "# RAG\n\nsemantic smoke marker",
                encoding="utf-8",
            )
            def readonly(reason: str | None, query: str = "semantic smoke marker") -> dict:
                before_meta = core.rag_index_meta(settings)
                before_points = json.dumps(state, sort_keys=True)
                backend = core.qdrant_json

                def read_request(settings: Settings, method: str, path: str, body: dict | None = None) -> dict:
                    assert method == "GET" or path.endswith(("/points/count", "/points/query")), (method, path)
                    return backend(settings, method, path, body)

                with patch.object(core, "index_vault", side_effect=AssertionError("search indexed")), \
                     patch.object(core, "embed_documents", side_effect=AssertionError("search embedded documents")), \
                     patch.object(core, "qdrant_json", side_effect=read_request):
                    result = search_vault(settings, agent, query)
                assert core.rag_index_meta(settings) == before_meta
                assert json.dumps(state, sort_keys=True) == before_points
                assert result["index"].get("fallback_reason") == reason, result["index"]
                if reason == "stale_index":
                    # Stale only filters semantic hits per document; it must not force keyword-only mode.
                    assert result["index"]["search_mode"] == "hybrid" and result["index"]["stale"] is True
                elif reason:
                    assert result["index"]["search_mode"] == "keyword"
                    assert all(item["match"] == "keyword" for item in result["results"])
                return result

            assert readonly("missing_index")["results"][0]["path"] == "50_Knowledge/rag.md"
            (settings.vault_dir / "50_Knowledge/z.md").write_text("# Later\n\nquota checkpoint marker", encoding="utf-8")
            building = threading.Event()
            release = threading.Event()
            embed_documents = core.embed_documents
            calls = 0

            def quota_after_checkpoint(settings: Settings, texts: list[str]) -> list[list[float]]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return embed_documents(settings, texts)
                building.set()
                assert release.wait(10)
                raise core.EmbeddingLimitError("quota blocked after first file")

            with patch.object(core, "embed_documents", side_effect=quota_after_checkpoint), ThreadPoolExecutor(1) as pool:
                future = pool.submit(core.index_vault, settings)
                try:
                    assert building.wait(10)
                    assert state["points"], "build must have a real partial checkpoint"
                    assert readonly("index_building")["results"]
                    try:
                        search_vault(settings, agent, "semantic smoke marker", refresh=True)
                    except RuntimeError as exc:
                        assert "already in progress" in str(exc)
                    else:
                        raise AssertionError("explicit indexing ignored build lock")
                finally:
                    release.set()
                try:
                    future.result(timeout=10)
                except core.EmbeddingLimitError:
                    pass
                else:
                    raise AssertionError("build was not quota blocked")
            core.set_rag_index_meta(settings, {"embedding_blocked_until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
            assert readonly("index_building")["results"]
            core.set_rag_index_meta(settings, {"rebuild_state": "ready", "chunks": str(len(state["points"]) + 1)})
            assert readonly("incomplete_index")["results"]
            result = search_vault(settings, agent, "semantic smoke marker", refresh=True)
            assert result["results"][0]["path"] == "50_Knowledge/rag.md"
            assert result["index"]["stale"] is False
            assert readonly(None)["results"][0]["match"] == "hybrid"

            qdrant_json = core.qdrant_json
            for failed_path in ("/exists", "/points/count", "/points/query"):
                def unavailable(settings: Settings, method: str, path: str, body: dict | None = None) -> dict:
                    if path.endswith(failed_path):
                        raise RuntimeError("Qdrant unavailable")
                    return qdrant_json(settings, method, path, body)

                with patch.object(core, "qdrant_json", side_effect=unavailable):
                    assert readonly("qdrant_unavailable")["results"]

            for error, reason in (
                (core.EmbeddingLimitError("quota"), "embedding_limit"),
                (RuntimeError("provider authentication"), "embedding_unavailable"),
                (ValueError("malformed embedding response"), "embedding_unavailable"),
                (TimeoutError("provider timeout"), "embedding_unavailable"),
            ):
                with patch.object(core, "embed_query", side_effect=error):
                    assert readonly(reason)["results"]

            good_point = {"score": 1.0, "payload": state["points"][0]["payload"]}
            for bad_point in (None, {"payload": []}, {**good_point, "score": "bad"}, {**good_point, "score": float("nan")}):
                def malformed(settings: Settings, method: str, path: str, body: dict | None = None) -> dict:
                    if path.endswith("/points/query"):
                        return {"result": {"points": [good_point, bad_point]}}
                    return qdrant_json(settings, method, path, body)

                with patch.object(core, "qdrant_json", side_effect=malformed):
                    assert readonly("qdrant_unavailable")["results"]

            before = result["index"]["chunks"]
            (settings.vault_dir / "40_Agents").mkdir()
            (settings.vault_dir / "40_Agents/new.md").write_text(
                "# New\n\nautomatic refresh marker",
                encoding="utf-8",
            )
            result = search_vault(settings, agent, "automatic refresh marker")
            assert result["index"]["chunks"] == before
            assert result["index"]["updated"] == 0
            assert result["index"]["stale"] is True
            assert result["results"][0]["path"] == "40_Agents/new.md"
            refreshed = search_vault(settings, agent, "automatic refresh marker", refresh=True)
            assert refreshed["index"]["chunks"] > before
            assert refreshed["index"]["stale"] is False

            (settings.vault_dir / "50_Knowledge/rag.md").write_text("# Replaced\n\nunrelated content", encoding="utf-8")
            (settings.vault_dir / "50_Knowledge/z.md").unlink()
            # Changed and deleted documents lose their semantic hits; the unchanged indexed note keeps its own.
            changed = readonly("stale_index")
            assert "50_Knowledge/rag.md" not in [item["path"] for item in changed["results"]]
            assert changed["index"]["semantic_filtered"] >= 1
            deleted = readonly("stale_index", "quota checkpoint marker")
            assert "50_Knowledge/z.md" not in [item["path"] for item in deleted["results"]]
            unchanged = readonly("stale_index", "automatic refresh marker")
            assert [item["path"] for item in unchanged["results"]] == ["40_Agents/new.md"]
            assert unchanged["results"][0]["match"] == "hybrid"
    finally:
        server.shutdown()


def free_port() -> int | None:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    except (PermissionError, OSError) as exc:
        print(f"skip gateway http smoke: {exc}")
        return None


def run_gateway_http_smoke() -> None:
    fake_qdrant = start_fake_qdrant("persona_http_smoke")
    port = free_port()
    if not fake_qdrant or not port:
        return
    qdrant_server, _state = fake_qdrant
    gateway_server: uvicorn.Server | None = None
    gateway_thread: threading.Thread | None = None
    env = {
        "VAULT_DIR": "",
        "DB_PATH": "",
        "HOST_ID": "http-smoke",
        "ADMIN_PASSWORD": "admin",
        "EMBEDDING_PROVIDER": "hash",
        "EMBEDDING_MODEL": core.CLOUDFLARE_EMBEDDING_MODEL,
        "EMBEDDING_BATCH_SIZE": "32",
        "CLOUDFLARE_ACCOUNT_ID": "",
        "CLOUDFLARE_API_TOKEN": "",
        "QDRANT_API_KEY": "",
        "QDRANT_URL": f"http://127.0.0.1:{qdrant_server.server_port}",
        "QDRANT_COLLECTION": "persona_http_smoke",
    }
    old_env = {key: os.environ.get(key) for key in env}
    try:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            env["VAULT_DIR"] = str(root / "vault")
            env["DB_PATH"] = str(root / "gateway.db")
            os.environ.update(env)

            token = generate_token()
            read_token = generate_token()
            settings = Settings.from_env()
            init_db(settings.db_path)
            upsert_agent(
                settings.db_path,
                "http-smoke-agent",
                token,
                ["conversation-log", "agent-memo", "vault-rag"],
                ["30_Conversations/raw"],
            )
            upsert_agent(
                settings.db_path,
                "http-read-agent",
                read_token,
                ["vault-rag"],
                [],
            )

            gateway_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
            gateway_thread = threading.Thread(target=gateway_server.run, daemon=True)
            gateway_thread.start()

            base_url = f"http://127.0.0.1:{port}"
            for _ in range(50):
                try:
                    urllib.request.urlopen(f"{base_url}/healthz", timeout=0.2).read()
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError("gateway did not start")

            marker = "http endpoint rag smoke marker"
            capabilities_request = urllib.request.Request(
                f"{base_url}/gateway/v3/capabilities",
                headers={"Authorization": f"Bearer {token}"},
            )
            capabilities = json.loads(urllib.request.urlopen(capabilities_request, timeout=5).read().decode())
            assert capabilities["api_version"] == "v3"
            assert capabilities["plugin"]["marketplace"] == "mykim0409/persona-vault-gateway"
            assert capabilities["plugin"]["min_version"] == "0.7.0"
            assert "conversation-upsert-v3" in capabilities["features"]
            assert "conversation-merge-v1" in capabilities["features"]
            assert "raw-evidence-v1" in capabilities["features"]
            assert "working-agreement-v1" in capabilities["features"]

            unauthenticated_old_request = urllib.request.Request(
                f"{base_url}/gateway/v2/agent-memo",
                data=json.dumps({"title": "Invalid Token", "body": marker}).encode(),
                headers={
                    "Authorization": "Bearer invalid",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                urllib.request.urlopen(unauthenticated_old_request, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
                assert json.loads(exc.read().decode())["detail"] == "invalid token"
            else:
                raise AssertionError("API version check ran before token authentication")

            retired_memo_request = urllib.request.Request(
                f"{base_url}/gateway/v2/agent-memo",
                data=json.dumps({"legacy_body": marker}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(retired_memo_request, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 410
                assert 'rel="successor-version"' in exc.headers["Link"]
                upgrade = json.loads(exc.read().decode())["detail"]
                assert upgrade["code"] == "client_upgrade_required"
                assert upgrade["reason"] == "api_version_retired"
                assert upgrade["requested_api_version"] == "v2"
                assert upgrade["current_api_version"] == "v3"
                assert upgrade["preserve_request_body"] is True
                assert upgrade["plugin"]["min_version"] == "0.7.0"
            else:
                raise AssertionError("retired API version was accepted")
            assert not list(settings.vault_dir.rglob("*.md"))

            old_v1_request = urllib.request.Request(
                f"{base_url}/gateway/v1/conversation-log",
                data=json.dumps({
                    "session_id": "old-v2-session",
                    "started_at": "2026-07-30T10:00:00Z",
                    "messages": [{"role": "user", "content": "missing current fields"}],
                }).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(old_v1_request, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 410
            else:
                raise AssertionError("old v1 conversation payload was accepted")

            conversation_payload = {
                "kind": "conversation",
                "session_id": "http-session",
                "project": "PersonaVault",
                "title": "HTTP session",
                "started_at": "2026-07-30T10:00:00Z",
                "ended_at": "2026-07-30T10:00:00Z",
                "messages": [
                    {
                        "role": "user",
                        "content": "capture this request",
                        "event_id": "codex:session:turn:user",
                        "timestamp": "2026-07-30T10:00:00Z",
                    }
                ],
                "context": {"client": "test"},
                "tags": ["agent-session", "test"],
                "privacy": "normal",
            }
            conversation_request = urllib.request.Request(
                f"{base_url}/gateway/v3/capture",
                data=json.dumps(conversation_payload).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            first_conversation = json.loads(urllib.request.urlopen(conversation_request, timeout=5).read().decode())
            forbidden_capture = urllib.request.Request(
                f"{base_url}/gateway/v3/capture",
                data=json.dumps(conversation_payload).encode(),
                headers={"Authorization": f"Bearer {read_token}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(forbidden_capture, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            else:
                raise AssertionError("read-only token was allowed to capture")
            conversation_payload["messages"].append(
                {
                    "role": "assistant",
                    "content": "captured result",
                    "event_id": "codex:session:turn:assistant",
                    "timestamp": "2026-07-30T10:01:00Z",
                }
            )
            conversation_request = urllib.request.Request(
                f"{base_url}/gateway/v3/capture",
                data=json.dumps(conversation_payload).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            updated_conversation = json.loads(urllib.request.urlopen(conversation_request, timeout=5).read().decode())
            assert first_conversation["path"] == updated_conversation["path"]
            assert updated_conversation["operation"] == "updated"

            conversation_payload["mode"] = "merge"
            conversation_payload["messages"] = [{
                "role": "assistant", "content": "additive batch",
                "event_id": "codex:session:batch:assistant", "timestamp": "2026-07-30T10:02:00Z",
            }]
            for attempt, expected_status in enumerate((200, 200, 409)):
                request = urllib.request.Request(
                    f"{base_url}/gateway/v3/capture",
                    data=json.dumps(conversation_payload).encode(),
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    response = urllib.request.urlopen(request, timeout=5)
                    assert response.status == expected_status
                    assert json.loads(response.read())["path"] == first_conversation["path"]
                except urllib.error.HTTPError as exc:
                    assert exc.code == expected_status == 409
                if expected_status == 200:
                    text = (settings.vault_dir / first_conversation["path"]).read_text(encoding="utf-8")
                    assert core.parse_frontmatter(text)["event_count"] == 3
                    assert "capture this request" in text and "captured result" in text
                    if attempt == 1:
                        conversation_payload["messages"][0]["content"] = "conflicting content"


            note_request = urllib.request.Request(
                f"{base_url}/gateway/v3/capture",
                data=json.dumps({
                    "kind": "note",
                    "title": "HTTP RAG Smoke",
                    "body": f"RAG smoke marker: {marker}",
                    "note_type": "observation",
                }).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            note = json.loads(urllib.request.urlopen(note_request, timeout=5).read().decode())
            assert note["status"] == "ok"
            assert note["path"].startswith("30_Conversations/raw/")
            assert not (settings.vault_dir / "40_Agents").exists()

            search_request = urllib.request.Request(
                f"{base_url}/gateway/v3/search",
                data=json.dumps({"query": marker, "refresh": True, "view": "evidence"}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            search = json.loads(urllib.request.urlopen(search_request, timeout=10).read().decode())
            assert marker in search["context"], search
            assert search["view"] == "evidence"
            assert search["results"][0]["path"].startswith("30_Conversations/raw/")
            assert search["results"][0]["role"] == "evidence"
            assert "groups" not in search
            assert "analysis" not in search
            assert set(search["results"][0]) <= set(app_module.SEARCH_RESULT_FIELDS) | {"role"}

            health_request = urllib.request.Request(
                f"{base_url}/gateway/v3/health",
                headers={"Authorization": f"Bearer {token}"},
            )
            health = json.loads(urllib.request.urlopen(health_request, timeout=5).read().decode())
            assert health["document_count"] == 2
            assert health["index"]["chunks"] >= 1

            agreement_path = settings.vault_dir / "10_User/WORKING_AGREEMENT.md"
            agreement_path.parent.mkdir(parents=True)
            agreement_path.write_text(WORKING_AGREEMENT_TEXT, encoding="utf-8")
            agreement_request = urllib.request.Request(
                f"{base_url}/gateway/v3/working-agreement",
                headers={"Authorization": f"Bearer {token}"},
            )
            agreement = json.loads(urllib.request.urlopen(agreement_request, timeout=5).read().decode())
            assert agreement["status"] == "ok"
            assert agreement["path"] == "10_User/WORKING_AGREEMENT.md"
            assert "Prefer concise status updates." in agreement["content"]
    finally:
        if gateway_server:
            gateway_server.should_exit = True
        if gateway_thread:
            gateway_thread.join(timeout=5)
        qdrant_server.shutdown()
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_conversation_merge_checks() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = Settings(root / "vault", root / "gateway.db", "merge-test", embedding_provider="hash")
        init_db(settings.db_path)
        agent = {"agent_id": "merge-agent", "scopes": ["conversation-log"], "allowed_roots": ["30_Conversations/raw"]}

        def message(event_id: str, content: str = "content", timestamp: str = "2026-07-30T10:00:00+09:00") -> dict:
            return {"role": "assistant", "event_id": event_id, "content": content, "timestamp": timestamp}

        payload = {
            "session_id": "merge-session", "project": "PersonaVault", "title": "Original title",
            "started_at": "2026-07-30T10:00:00+09:00", "ended_at": "2026-07-30T10:00:00+09:00",
            "context": {"client": "original"}, "tags": [], "privacy": "normal",
        }

        def reject(values: dict, exception: type[Exception] = core.ConversationConflictError) -> None:
            before = {path: path.read_bytes() for path in settings.vault_dir.rglob("*.md")}
            try:
                save_conversation(settings, agent, values)
            except exception:
                pass
            else:
                raise AssertionError("unsafe conversation accepted")
            assert {path: path.read_bytes() for path in settings.vault_dir.rglob("*.md")} == before

        marker = (
            "UTF-8: \uac00\ub098\ub2e4\r\n\n### assistant\n\n"
            '<!-- pvg-event {"event_id": "example", "timestamp": "2026-07-30T10:00:00Z"} -->\n\n'
            "## Messages\n\n## Context Snapshot\n\n```json\n{}\n```\n\n"
            "**Agent delegation (not a user statement)**\n\nexample\n\n**Result**\n\nquoted\n\n"
        )
        original = {**message("first", marker), "request": marker, "agent_type": "explorer", "agent_id": "worker"}
        first = save_conversation(settings, agent, {**payload, "messages": [original]})
        path = settings.vault_dir / first["path"]
        assert core.parse_conversation(path.read_bytes().decode("utf-8"))[2] == [original]
        merge = {**payload, "mode": "merge", "messages": [original, message("second", "second batch")]}
        second = save_conversation(settings, agent, merge)
        assert second["path"] == first["path"] and second["operation"] == "updated"
        before_retry = path.read_bytes(), path.stat().st_mtime_ns
        save_conversation(settings, agent, merge)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before_retry
        assert core.parse_conversation(path.read_bytes().decode("utf-8"))[2][0] == original

        for conflict in (
            {**original, "content": "changed"}, {**original, "request": "changed"},
            {**original, "role": "user"}, {**original, "timestamp": "2026-07-31T10:00:00+09:00"},
        ):
            # Include the source day when testing a cross-day ID collision.
            reject({**merge, "messages": [message("must-not-write", timestamp="2026-07-29T10:00:00+09:00"), original, conflict]})
        reject({**merge, "messages": [message("new-id"), message("new-id", "conflict in batch")]})

        earlier = message("earlier", "earliest", "2026-07-30T08:00:00+09:00")
        next_day = message("next-day", "local date is not UTC date", "2026-07-31T00:05:00+09:00")
        save_conversation(settings, agent, {**merge, "title": "Ignored", "context": {}, "messages": [next_day, earlier]})
        assert path.exists()
        metadata, prefix, messages, suffix = core.parse_conversation(path.read_bytes().decode("utf-8"))
        assert metadata["started_at"] == earlier["timestamp"]
        assert metadata["observed_at"] == earlier["timestamp"]
        assert metadata["ended_at"] == original["timestamp"]
        assert messages[0] == earlier
        assert "Original title" in prefix and '"original"' in suffix
        paths = sorted(settings.vault_dir.rglob("*.md"))
        assert len(paths) == 2 and "/2026/07/31/" in paths[1].as_posix()

        # Snapshots still replace the day's events rather than implicitly merging.
        snapshot = save_conversation(settings, agent, {**payload, "messages": [message("snapshot-only")]})
        assert core.parse_conversation((settings.vault_dir / snapshot["path"]).read_text(encoding="utf-8"))[2] == [message("snapshot-only")]

        legacy_payload = {**payload, "session_id": "legacy-session", "messages": [message("legacy", "old content\n\n### Ordinary heading\n\ntrailing\n")]}
        legacy = save_conversation(settings, agent, legacy_payload)
        legacy_path = settings.vault_dir / legacy["path"]
        _metadata, prefix, _messages, suffix = core.parse_conversation(legacy_path.read_text(encoding="utf-8"))
        legacy_text = prefix + core.render_messages(legacy_payload["messages"]) + suffix
        legacy_path.write_text(legacy_text, encoding="utf-8")
        save_conversation(settings, agent, {**legacy_payload, "mode": "merge", "messages": [message("legacy-new")]})
        assert core.parse_conversation(legacy_path.read_text(encoding="utf-8"))[2][0] == legacy_payload["messages"][0]

        bad_legacy = [
            legacy_text.replace("event_count: 1", "event_count: 2"),
            legacy_text.replace("event_count: 1", "event_count: 1\nevent_count: 1"),
            legacy_text.replace('"event_id": "legacy"', '"event_id": "legacy", "event_id": "other"'),
            legacy_text + "unexpected trailing content\n",
            prefix + core.render_messages([message("legacy", marker)]) + suffix,
            prefix + core.render_messages([{**message("legacy"), "request": "old unframed delegation"}]) + suffix,
            legacy_text.replace("old content", "<!-- pvg-event malformed example -->"),
        ]
        for text in bad_legacy:
            legacy_path.write_text(text, encoding="utf-8")
            reject({**legacy_payload, "mode": "merge", "messages": [message("legacy-new")]})
        legacy_path.write_bytes(b"\xff invalid UTF-8")
        reject({**legacy_payload, "mode": "merge", "messages": [message("legacy-new")]})

        isolated = {**payload, "session_id": "legacy-day-isolation"}
        old_event = {
            **message("old-delegation", "legacy result", "2026-09-06T10:00:00+09:00"),
            "role": "subagent", "request": "legacy request",
        }
        old_result = save_conversation(settings, agent, {**isolated, "messages": [old_event]})
        old_path = settings.vault_dir / old_result["path"]
        _metadata, prefix, _messages, suffix = core.parse_conversation(old_path.read_text(encoding="utf-8"))
        old_path.write_text(prefix + core.render_messages([old_event]) + suffix, encoding="utf-8")
        old_bytes = old_path.read_bytes()
        new_event = message("new-day", "new collection", "2026-09-07T10:00:00+09:00")
        new_batch = {**isolated, "mode": "merge", "messages": [new_event]}
        with patch.object(core, "parse_conversation", wraps=core.parse_conversation) as parser:
            new_result = save_conversation(settings, agent, new_batch)
            parser.assert_not_called()
        new_path = settings.vault_dir / new_result["path"]
        assert new_result["operation"] == "created" and "/2026/09/07/" in new_path.as_posix()
        assert core.parse_conversation(new_path.read_text(encoding="utf-8"))[2] == [new_event]
        before_retry = new_path.read_bytes(), new_path.stat().st_mtime_ns
        save_conversation(settings, agent, new_batch)
        assert (new_path.read_bytes(), new_path.stat().st_mtime_ns) == before_retry
        earlier_event = message("new-day-earlier", "earlier", "2026-09-07T09:00:00+09:00")
        earlier_result = save_conversation(settings, agent, {**new_batch, "messages": [earlier_event]})
        assert earlier_result["path"] == new_result["path"]
        assert core.parse_conversation(new_path.read_text(encoding="utf-8"))[2] == [earlier_event, new_event]
        reject({**new_batch, "messages": [{**new_event, "content": "conflict"}]})
        reject({**new_batch, "messages": [old_event]})
        assert old_path.read_bytes() == old_bytes

        large = {**merge, "session_id": "large-session"}
        all_messages = [message(f"batch-{index}") for index in range(501)]
        save_conversation(settings, agent, {**large, "messages": all_messages[:300]})
        large_result = save_conversation(settings, agent, {**large, "messages": all_messages[300:]})
        assert len(core.parse_conversation((settings.vault_dir / large_result["path"]).read_text(encoding="utf-8"))[2]) == 501
        reject({**large, "messages": all_messages}, ValueError)
        reject({**large, "messages": [message(f"oversize-{index}", "x" * 64_000) for index in range(66)]}, ValueError)

        concurrent = {**merge, "session_id": "concurrent-session"}
        process_context = multiprocessing.get_context("spawn")
        processes = [process_context.Process(
            target=save_conversation,
            args=(settings, agent, {**concurrent, "messages": [message(f"process-{index}")]}),
        ) for index in range(6)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(
                lambda index: save_conversation(settings, agent, {**concurrent, "messages": [message(f"thread-{index}")]}),
                range(6),
            ))
        concurrent_path = settings.vault_dir / results[0]["path"]
        assert len({result["path"] for result in results}) == 1
        saved = core.parse_conversation(concurrent_path.read_text(encoding="utf-8"))[2]
        assert {item["event_id"] for item in saved} == {f"{kind}-{index}" for kind in ("process", "thread") for index in range(6)}


CONCEPT_WORDS = (
    ("rollback", "revert", "undone"), ("milk", "fridge"), ("invoice", "billing"), ("alpha",), ("beta",), ("gamma",),
)


def concept_embedding(text: str) -> list[float]:
    lowered = text.lower()
    vector = [0.0] * core.HASH_EMBEDDING_DIMENSIONS
    for axis, words in enumerate(CONCEPT_WORDS):
        vector[axis] = 1.0 if any(word in lowered for word in words) else 0.0
    return core.normalize_vector(vector)


def start_broken_qdrant(mode: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(self) -> None:
            if mode == "truncated":
                self.send_response(200)
                self.send_header("content-length", "100")
                self.end_headers()
                self.wfile.write(b"{")
            self.close_connection = True

        do_GET = do_POST = do_PUT = do_DELETE = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_search_regression_checks() -> None:
    fake_qdrant = start_fake_qdrant("persona_search_regression")
    symlink_qdrant = start_fake_qdrant("persona_symlink_regression")
    if not fake_qdrant or not symlink_qdrant:
        return
    server, state = fake_qdrant
    sep = "\u0085  "
    try:
        with TemporaryDirectory() as tmp, \
             patch.object(core, "embed_documents", side_effect=lambda _s, texts: [concept_embedding(t) for t in texts]), \
             patch.object(core, "embed_query", side_effect=lambda _s, text: (concept_embedding(text), core.HASH_EMBEDDING_MODEL)):
            root = Path(tmp)
            qdrant_url = f"http://127.0.0.1:{server.server_port}"
            settings = Settings(
                root / "vault", root / "gateway.db", "search-regression", embedding_provider="hash",
                qdrant_url=qdrant_url, qdrant_collection="persona_search_regression",
            )
            init_db(settings.db_path)
            vault = settings.vault_dir
            reader = {"agent_id": "reader", "scopes": ["vault-rag"], "allowed_roots": []}
            writer = {"agent_id": "writer", "scopes": ["conversation-log"], "allowed_roots": ["30_Conversations/raw"]}

            def plain(rel: str, text: str) -> None:
                (vault / rel).parent.mkdir(parents=True, exist_ok=True)
                (vault / rel).write_text(text, encoding="utf-8")

            def note(rel: str, body: str, **metadata: object) -> None:
                front = "\n".join(
                    f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in {"pv_schema": 1, **metadata}.items()
                )
                plain(rel, f"---\n{front}\n---\n\n# {metadata.get('id', rel)}\n\n{body}\n")

            def paths(result: dict) -> list[str]:
                return [item["path"] for item in result["results"]]

            def item_for(result: dict, path: str) -> dict:
                return next(item for item in result["results"] if item["path"] == path)

            # Unicode separators: values stay on one line, round-trip, and cannot forge metadata keys.
            session = f'x{sep}agent_id: "victim"{sep}id: user_working_agreement{sep}kind: decision'
            started = "2026-07-30T10:00:00+09:00"
            event = {
                "role": "assistant", "event_id": "unicode-1", "timestamp": started, "content": f"hello{sep}world",
                "agent_type": f"explorer{sep}x", "request": f"request{sep}text",
            }
            capture = {
                "session_id": session, "title": f"Title{sep}# forged", "project": f"Proj{sep}A", "tags": [f"t{sep}1"],
                "started_at": started, "messages": [event], "context": {"note": sep},
            }
            saved = save_conversation(settings, writer, capture)
            raw_text = (vault / saved["path"]).read_bytes().decode("utf-8")
            captured = document_metadata(saved["path"], raw_text)
            assert captured["agent_id"] == "writer" and captured["document_id"] == saved["conversation_id"]
            assert captured["kind"] == "conversation" and captured["metadata_valid"] and captured["session_id"] == session
            front = core.parse_frontmatter(raw_text)
            assert front["project"] == [f"[[Proj{sep}A]]"] and front["topics"] == [f"t{sep}1"]
            assert core.parse_conversation(raw_text)[2] == [event]
            merged = save_conversation(
                settings, writer,
                {**capture, "mode": "merge", "messages": [event, {**event, "event_id": "unicode-2", "content": sep}]},
            )
            assert merged["path"] == saved["path"] and merged["operation"] == "updated"
            assert [m["event_id"] for m in core.parse_conversation((vault / merged["path"]).read_text(encoding="utf-8"))[2]] == [
                "unicode-1", "unicode-2",
            ]
            legacy = core.parse_frontmatter(f'---\nid: "legacy"\nsession_id: "a{sep}agent_id: \\"victim\\""\nagent_id: "real"\n---\n')
            assert legacy["agent_id"] == "real" and legacy["session_id"] == f'a{sep}agent_id: "victim"'

            # Deep or corrupt frontmatter only invalidates its own document.
            deep_mapping = "---\n" + "".join(" " * i + f"k{i}:\n" for i in range(1200)) + " " * 1200 + "leaf: 1\n---\n\nbody"
            deep_inline = "---\napplicability: " + '{"a":' * 900 + "1" + "}" * 900 + "\n---\n\nbody"
            for deep in (deep_mapping, deep_inline):
                assert document_metadata("20_Projects/deep.md", deep)["metadata_valid"] is False
            plain("20_Projects/deep-mapping.md", deep_mapping)
            plain("20_Projects/deep-inline.md", deep_inline)

            # Natural queries: stopwords do not match, semantic-only hits survive beside lexical ones.
            plain("20_Projects/rollback.md", "# Rollback\n\nRevert the release with the rollback runbook.\n")
            plain("20_Projects/groceries.md", "# Groceries\n\nThe milk is in the fridge.\n")
            plain("20_Projects/deployment-log.md", "# Deployment log\n\nThe deployment finished without incident.\n")
            plain("20_Projects/partial.md", "# Partial\n\nRollback notes that also mention milk, invoice, alpha, beta and gamma.\n")
            core.index_vault(settings)
            assert core.qdrant_index_current(settings)
            assert core.keyword_terms("how is the deployment undone") == ["deployment", "undone"]
            assert core.keyword_terms("what is the") == ["what", "is", "the"]
            assert core.rag_score("x.md", "maintain chain", core.keyword_terms("ai tool")) == 0
            assert core.rag_score("x.md", "AI는 유용한 tool", core.keyword_terms("ai tool")) > 0
            assert core.rag_score("x.md", "배포를 롤백하는 절차", core.keyword_terms("배포 롤백")) > 0
            assert core.rag_score(
                "x.md", "ModuleNotFoundError: no module named '_exampleenc_native'",
                core.keyword_terms("ModuleNotFoundError _exampleenc_native"),
            ) > 0
            natural = search_vault(settings, reader, "how is the deployment undone")
            assert "20_Projects/groceries.md" not in paths(natural), paths(natural)
            assert item_for(natural, "20_Projects/rollback.md")["match"] == "semantic"
            assert item_for(natural, "20_Projects/deployment-log.md")["match"] in {"keyword", "hybrid"}
            assert natural["answer_state"]["state"] == "supported", natural["answer_state"]
            assert natural["index"].get("fallback_reason") is None and not natural["index"]["stale"]
            # A valid but weak semantic candidate (cosine 1/sqrt(6) < 0.5) is kept, after all lexical matches.
            partial = item_for(natural, "20_Projects/partial.md")
            assert partial["match"] == "semantic" and 0 < partial["ranking"]["semantic"] < 0.5, partial["ranking"]
            matches = [item["match"] == "semantic" for item in natural["results"]]
            assert matches == sorted(matches), [item["match"] for item in natural["results"]]
            assert "20_Projects/groceries.md" in paths(search_vault(settings, reader, "is the"))

            # An unrelated raw capture only makes the index stale; valid canonical semantic hits survive.
            save_conversation(settings, writer, {
                "session_id": "lunch", "started_at": started, "context": {},
                "messages": [{"role": "user", "event_id": "lunch-1", "timestamp": started, "content": "lunch chatter"}],
            })
            stale = search_vault(settings, reader, "how is the deployment undone")
            assert stale["index"]["stale"] is True and stale["index"]["fallback_reason"] == "stale_index"
            assert stale["index"]["search_mode"] == "hybrid"
            assert item_for(stale, "20_Projects/rollback.md")["match"] == "semantic"

            # Payload authority is untrusted: current metadata, ranking, and eligibility come from the vault.
            for point in state["points"]:
                if point["payload"]["path"] == "20_Projects/rollback.md":
                    point["payload"].update(
                        review_state="merged", temporal_state="superseded", retrieval_state="machine_corroborated",
                        support={"forged": True},
                    )
            expected = next(
                doc for doc in core.analyzed_documents(core.vault_documents(settings)) if doc["path"] == "20_Projects/rollback.md"
            )
            forged = item_for(search_vault(settings, reader, "how is the deployment undone", bundle="current"), "20_Projects/rollback.md")
            assert (forged["review_state"], forged["temporal_state"]) == ("human_accepted", "current")
            assert forged["retrieval_state"] == expected.get("retrieval_state") and forged["support"] == expected.get("support")
            auto = item_for(search_vault(settings, reader, "how is the deployment undone"), "20_Projects/rollback.md")
            assert auto["ranking"]["role"] == round(core.bundle_role_score(expected, "auto"), 6)
            core.index_vault(settings)

            # Changed, deleted, and superseded canonical notes lose their stale semantic hits.
            plain("20_Projects/rollback.md", "# Rollback\n\nRevert the release with the rollback runbook, revised.\n")
            changed = search_vault(settings, reader, "how is the deployment undone")
            assert "20_Projects/rollback.md" not in paths(changed) and changed["index"]["semantic_filtered"] >= 1
            (vault / "20_Projects/rollback.md").unlink()
            assert "20_Projects/rollback.md" not in paths(search_vault(settings, reader, "how is the deployment undone"))
            note("20_Projects/rollback.md", "Revert the release with the rollback runbook.", id="kn_rollback", memory_type="canonical",
                 review_state="human_accepted", temporal_state="superseded", retrieval_tier="history")
            assert "20_Projects/rollback.md" not in paths(search_vault(settings, reader, "how is the deployment undone"))
            core.index_vault(settings)
            assert "20_Projects/rollback.md" in paths(search_vault(settings, reader, "how is the deployment undone", bundle="history"))
            assert "20_Projects/rollback.md" not in paths(search_vault(settings, reader, "how is the deployment undone", bundle="current"))

            # Related expansion respects the view policy and the final limit.
            note("50_Knowledge/zeta-old.md", "ZETA-1 old rule.", id="kn_zeta_old", subject_id="zeta", memory_type="canonical",
                 review_state="human_accepted", temporal_state="superseded", retrieval_tier="history")
            note("50_Knowledge/zeta-new.md", "ZETA-1 new rule.", id="kn_zeta_new", subject_id="zeta", memory_type="canonical",
                 review_state="human_accepted", temporal_state="current", retrieval_tier="primary",
                 relations={"supersedes": ["kn_zeta_old"]})
            current = search_vault(settings, reader, "ZETA-1", limit=1, bundle="current")
            assert [item["document_id"] for item in current["results"]] == ["kn_zeta_new"]
            for index in range(8):
                note(f"40_Agents/a/episodes/omega-{index}.md", "OMEGA-9 observation.", id=f"ep_omega_{index}", subject_id="omega",
                     memory_type="episode", session_id=f"omega-{index}", evidence_refs=[f"run-{index}"])
            for limit in (1, 3):
                for bundle in ("history", "current"):
                    assert len(search_vault(settings, reader, "OMEGA-9", limit=limit, bundle=bundle)["results"]) <= limit
            assert len(search_vault(settings, reader, "OMEGA-9", limit=3, bundle="history")["results"]) == 3
            # Context that does not fit is reported rather than silently dropped.
            truncated = search_vault(settings, reader, "OMEGA-9", limit=1, bundle="history")
            assert truncated["index"]["results_truncated"] > 0 and "results_truncated" not in truncated
            public = app_module.search_response(truncated, "history")
            assert public["index"]["results_truncated"] == truncated["index"]["results_truncated"]

            # A semantic-only candidate that contradicts the lexical canonical claim must gate the answer;
            # an unrelated weak semantic filler with its own conflict must not.
            note("20_Projects/release-policy.md", "Rollback is recommended.", id="kn_release", subject_id="release-policy",
                 memory_type="canonical", review_state="human_accepted", temporal_state="current",
                 retrieval_tier="primary", provenance_mode="human_asserted")
            note("40_Agents/a/candidates/revert-unsafe.md", "Revert is unsafe.", id="cand_revert_unsafe", memory_type="candidate",
                 subject_id="release-policy", relations={"contradicts": ["kn_release"]})
            plain("20_Projects/billing-policy.md", "# Billing\n\nBilling policy: invoices are monthly.\n")
            note("40_Agents/b/candidates/dispute.md", "Invoice dispute notes alpha beta gamma.", id="cand_dispute",
                 memory_type="candidate", subject_id="disputes", relations={"contradicts": ["kn_unrelated"]})
            core.index_vault(settings)
            contested = search_vault(settings, reader, "rollback recommended")
            unsafe = next(item for item in contested["results"] if item["document_id"] == "cand_revert_unsafe")
            assert unsafe["match"] == "semantic" and unsafe["conflict_state"] == "unresolved"
            assert contested["answer_state"] == {"state": "review_required", "reason": "unresolved_conflict"}
            filler_query = search_vault(settings, reader, "billing invoices")
            filler = next(item for item in filler_query["results"] if item["document_id"] == "cand_dispute")
            assert filler["match"] == "semantic" and filler["conflict_state"] == "unresolved"
            assert filler_query["answer_state"]["state"] == "supported", filler_query["answer_state"]

            # An interrupted payload migration keeps vectors but never claims a current index.
            core.index_vault(settings)
            assert core.qdrant_index_current(settings)
            for point in state["points"]:
                point["payload"]["payload_hash"] = "old"
            points_before = len(state["points"])
            overwritten: list[str] = []
            overwrite = core.qdrant_overwrite_payload

            def interrupted_overwrite(settings: Settings, point_id: str, payload: dict) -> None:
                if overwritten:
                    raise RuntimeError("payload migration interrupted")
                overwritten.append(point_id)
                overwrite(settings, point_id, payload)

            with patch.object(core, "qdrant_overwrite_payload", side_effect=interrupted_overwrite):
                try:
                    core.index_vault(settings)
                except RuntimeError as exc:
                    assert "interrupted" in str(exc)
                else:
                    raise AssertionError("interrupted payload migration was ignored")
            meta = core.rag_index_meta(settings)
            assert meta["fingerprint"] == "" and meta["rebuild_state"] == "building"
            assert len(state["points"]) == points_before
            assert not core.qdrant_index_current(settings) and not core.qdrant_index_current(settings, allow_stale=True)
            assert rag_readiness(settings)["status"] == "error"
            assert search_vault(settings, reader, "how is the deployment undone")["index"]["fallback_reason"] == "index_building"
            with patch.object(core, "embed_documents", side_effect=AssertionError("resume must not re-embed")):
                resumed = core.index_vault(settings)
            assert resumed["updated"] == 0 and resumed["payload_updated"] > 0
            assert core.qdrant_index_current(settings) and rag_readiness(settings)["status"] == "ok"

            # An unreadable file aborts indexing before any deletion or meta change; search just skips it.
            points_snapshot = json.dumps(state["points"], sort_keys=True)
            meta_snapshot = core.rag_index_meta(settings)
            read_text = Path.read_text

            def unreadable(self: Path, *args: object, **kwargs: object) -> str:
                if self.name == "deployment-log.md":
                    raise PermissionError("denied")
                return read_text(self, *args, **kwargs)

            with patch.object(Path, "read_text", unreadable):
                try:
                    core.index_vault(settings)
                except RuntimeError as exc:
                    assert "unchanged" in str(exc)
                else:
                    raise AssertionError("index ignored an unreadable file")
                assert json.dumps(state["points"], sort_keys=True) == points_snapshot
                assert core.rag_index_meta(settings) == meta_snapshot
                skipped = search_vault(settings, reader, "how is the deployment undone")
                assert "20_Projects/deployment-log.md" not in paths(skipped) and skipped["results"]
            assert core.qdrant_index_current(settings)

            # Transport failures while reading from Qdrant always surface as RuntimeError.
            for mode in ("disconnect", "truncated"):
                broken_server = start_broken_qdrant(mode)
                try:
                    broken = Settings(
                        vault, root / "gateway.db", "broken", embedding_provider="hash",
                        qdrant_url=f"http://127.0.0.1:{broken_server.server_port}", qdrant_collection="persona_search_regression",
                    )
                    try:
                        core.qdrant_json(broken, "GET", "/collections/persona_search_regression/exists")
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError(f"{mode} Qdrant response was accepted")
                    assert core.qdrant_collection_exists(broken) is False
                    assert rag_readiness(broken)["status"] == "error"
                    unavailable = search_vault(broken, reader, "deployment finished")
                    assert unavailable["index"]["fallback_reason"] == "qdrant_unavailable"
                    assert "20_Projects/deployment-log.md" in paths(unavailable)
                    core.set_rag_index_meta(broken, {"embedding_blocked_until": "2000-01-01T00:00:00+00:00"})
                    try:
                        core.retry_due_embeddings(broken)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("retry did not report the Qdrant failure")
                finally:
                    broken_server.shutdown()
                    core.set_rag_index_meta(settings, {"embedding_blocked_until": ""})

            # Long transcripts cannot starve other documents; fewer eligible documents are not padded.
            lean = Settings(
                root / "lean-vault", root / "gateway.db", "lean", embedding_provider="hash",
                qdrant_url=qdrant_url, qdrant_collection="persona_missing_collection",
            )
            (lean.vault_dir / "30_Conversations/raw/2026/07/30").mkdir(parents=True)
            (lean.vault_dir / "30_Conversations/raw/2026/07/30/a-long.md").write_text(
                "\n\n".join(f"deploy rollback paragraph {index} " + "x" * 80 for index in range(1200)), encoding="utf-8",
            )
            (lean.vault_dir / "20_Projects").mkdir()
            for index in range(6):
                (lean.vault_dir / f"20_Projects/b{index}.md").write_text(f"# Runbook {index}\n\ndeploy rollback runbook", encoding="utf-8")
            crowded = search_vault(lean, reader, "deploy rollback", limit=5)
            assert crowded["index"]["search_mode"] == "keyword"
            assert len(crowded["results"]) == 5 and len({item["document_id"] for item in crowded["results"]}) == 5
            assert all(path.startswith("20_Projects/") for path in paths(crowded)), paths(crowded)
            for index in range(2, 6):
                (lean.vault_dir / f"20_Projects/b{index}.md").unlink()
            assert len(search_vault(lean, reader, "deploy rollback", limit=5)["results"]) == 3

            # Symlinks: no write redirect to another root, no duplicate index entries, nothing outside the vault.
            link_server, _link_state = symlink_qdrant
            linked = Settings(
                root / "linked-vault", root / "gateway.db", "linked", embedding_provider="hash",
                qdrant_url=f"http://127.0.0.1:{link_server.server_port}", qdrant_collection="persona_symlink_regression",
            )
            raw, knowledge = linked.vault_dir / "30_Conversations/raw", linked.vault_dir / "50_Knowledge"
            raw.mkdir(parents=True)
            knowledge.mkdir()
            (knowledge / "real.md").write_text("# Real\n\nreal note", encoding="utf-8")
            (root / "outside.md").write_text("# Outside\n\noutside note", encoding="utf-8")
            os.symlink(knowledge / "real.md", knowledge / "alias.md")
            os.symlink(root / "outside.md", knowledge / "outside.md")
            files = [rel for rel, _path in core.iter_markdown_files(linked.vault_dir)]
            assert files == ["50_Knowledge/real.md"], files
            assert core.index_vault(linked)["files"] == 1
            os.symlink(knowledge, raw / "2026", target_is_directory=True)
            try:
                save_conversation(linked, writer, {**capture, "session_id": "redirect"})
            except PermissionError:
                pass
            else:
                raise AssertionError("symlink redirected a capture outside its allowed root")
            assert sorted(path.name for path in knowledge.iterdir()) == ["alias.md", "outside.md", "real.md"]
            (raw / "archive-2027").mkdir()
            os.symlink(raw / "archive-2027", raw / "2027", target_is_directory=True)
            inside = target_path(linked, "30_Conversations/raw/2027/01/01/x.md", ["30_Conversations/raw"])
            assert inside.resolve().is_relative_to((raw / "archive-2027").resolve())
    finally:
        server.shutdown()
        symlink_qdrant[0].shutdown()


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = Settings(root / "vault", root / "gateway.db", "test-host", embedding_provider="hash")
        token = generate_token()
        init_db(settings.db_path)
        with sqlite3.connect(settings.db_path) as con:
            assert con.execute("PRAGMA user_version").fetchone()[0] == core.DB_SCHEMA_VERSION

        legacy_db = root / "legacy.db"
        with sqlite3.connect(legacy_db) as con:
            con.executescript(core.SCHEMA)
            con.execute("INSERT INTO rag_index_meta (key, value) VALUES ('legacy', 'kept')")
            assert con.execute("PRAGMA user_version").fetchone()[0] == 0
        init_db(legacy_db)
        init_db(legacy_db)
        with sqlite3.connect(legacy_db) as con:
            assert con.execute("PRAGMA user_version").fetchone()[0] == core.DB_SCHEMA_VERSION
            assert con.execute("SELECT value FROM rag_index_meta WHERE key = 'legacy'").fetchone()[0] == "kept"

        future_db = root / "future.db"
        with sqlite3.connect(future_db) as con:
            con.execute(f"PRAGMA user_version = {core.DB_SCHEMA_VERSION + 1}")
        try:
            init_db(future_db)
        except RuntimeError as exc:
            assert "newer than supported" in str(exc)
        else:
            raise AssertionError("future database schema was accepted")

        upsert_agent(
            settings.db_path,
            "linux-container-a",
            token,
            ["conversation-log", "agent-memo", "vault-rag"],
            allowed_roots_for_agent("linux-container-a"),
        )
        agent = lookup_agent(settings.db_path, token)
        assert agent and agent["agent_id"] == "linux-container-a"

        agreement_settings = Settings(
            root / "agreement-vault",
            settings.db_path,
            "test-host",
            embedding_provider="hash",
        )
        assert load_working_agreement(agreement_settings, agent)["status"] == "absent"
        agreement_path = agreement_settings.vault_dir / "10_User/WORKING_AGREEMENT.md"
        agreement_path.parent.mkdir(parents=True)
        agreement_path.write_text(WORKING_AGREEMENT_TEXT, encoding="utf-8")
        agreement = load_working_agreement(agreement_settings, agent)
        assert agreement["status"] == "ok"
        assert "Prefer concise status updates." in agreement["content"]
        agreement_path.write_text(
            WORKING_AGREEMENT_TEXT.replace("review_state: human_accepted", "review_state: unreviewed"),
            encoding="utf-8",
        )
        assert load_working_agreement(agreement_settings, agent)["status"] == "unavailable"
        try:
            load_working_agreement(agreement_settings, {"scopes": ["agent-memo"]})
        except PermissionError:
            pass
        else:
            raise AssertionError("write-only token read the working agreement")

        agents = list_agents(settings.db_path)
        assert len(agents) == 1 and agents[0]["enabled"]
        assert agents[0]["token_prefix"] == token[:12]
        assert 'action="/admin/tokens/rotate"' in agent_table_html(agents)
        assert any(getattr(route, "path", "") == "/gateway/v3/capture" for route in app.routes)
        assert any(getattr(route, "path", "") == "/gateway/v3/search" for route in app.routes)
        assert any(getattr(route, "path", "") == "/gateway/v3/health" for route in app.routes)
        assert any(getattr(route, "path", "") == "/gateway/v3/capabilities" for route in app.routes)
        assert any(getattr(route, "path", "") == "/gateway/v3/working-agreement" for route in app.routes)
        assert any(getattr(route, "path", "") == "/admin/rag/rebuild" for route in app.routes)
        assert any(getattr(route, "path", "") == "/readyz" for route in app.routes)
        token_form = token_form_html(agents)
        assert 'action="/admin/rag/rebuild"' in token_form
        assert '<select name="permission"' in token_form
        assert 'name="scopes"' not in token_form
        assert scopes_for_permission("read") == ["vault-rag"]
        assert scopes_for_permission("write") == ["conversation-log", "agent-memo"]
        assert scopes_for_permission("read-write") == ["conversation-log", "agent-memo", "vault-rag"]
        assert allowed_roots_for_agent("mac-codex", scopes_for_permission("read")) == []
        assert allowed_roots_for_agent("mac-codex", scopes_for_permission("write")) == [
            "30_Conversations/raw",
        ]
        try:
            scopes_for_permission("admin")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid token permission was accepted")

        conversation = save_conversation(
            settings,
            agent,
            {
                "session_id": "path-safety-session",
                "title": "../PersonaVault Handoff",
                "project": "PersonaVault",
                "started_at": "2026-07-05T22:10:30+09:00",
                "ended_at": "2026-07-05T22:10:30+09:00",
                "messages": [{
                    "role": "user",
                    "content": "hello",
                    "event_id": "codex:path-safety:user",
                    "timestamp": "2026-07-05T22:10:30+09:00",
                }],
                "context": {"cwd": "/workspace"},
                "tags": ["obsidian"],
                "privacy": "normal",
            },
        )
        assert conversation["path"].startswith("30_Conversations/raw/2026/07/05/")
        assert (settings.vault_dir / conversation["path"]).exists()
        assert ".." not in conversation["path"]
        conversation_text = (settings.vault_dir / conversation["path"]).read_text(encoding="utf-8")
        conversation_metadata = document_metadata(conversation["path"], conversation_text)
        assert conversation_metadata["memory_type"] == "transcript"
        assert conversation_metadata["retrieval_tier"] == "evidence"

        session_payload = {
            "session_id": "codex-session-1",
            "project": "PersonaVault",
            "title": "PersonaVault implementation",
            "started_at": "2026-07-30T10:00:00Z",
            "ended_at": "2026-07-30T10:00:00+09:00",
            "messages": [
                {
                    "role": "user",
                    "content": "implement capture",
                    "event_id": "codex:session1:turn1:user",
                    "turn_id": "turn1",
                    "timestamp": "2026-07-30T10:00:00+09:00",
                }
            ],
            "context": {"client": "codex"},
            "tags": ["agent-session", "codex"],
            "privacy": "normal",
        }
        created_session = save_conversation(settings, agent, session_payload)
        session_payload["messages"].extend(
            [
                {
                    "role": "user",
                    "content": "duplicate must be ignored",
                    "event_id": "codex:session1:turn1:user",
                    "timestamp": "2026-07-30T10:00:00+09:00",
                },
                {
                    "role": "subagent",
                    "request": "inspect hooks",
                    "content": "hook manifest was missing",
                    "event_id": "codex:session1:subagent:1",
                    "agent_id": "agent-1",
                    "agent_type": "explorer",
                    "timestamp": "2026-07-30T10:01:00+09:00",
                },
            ]
        )
        updated_session = save_conversation(settings, agent, session_payload)
        assert created_session["path"] == updated_session["path"]
        assert created_session["operation"] == "created"
        assert updated_session["operation"] == "updated"
        session_text = (settings.vault_dir / updated_session["path"]).read_text(encoding="utf-8")
        assert session_text.count("codex:session1:turn1:user") == 1
        assert "**Agent delegation (not a user statement)**" in session_text
        assert "event_count: 2" in session_text
        assert 'segment_date: "2026-07-30"' in session_text
        assert '"timestamp": "2026-07-30T10:01:00+09:00"' in session_text
        assert not list((settings.vault_dir / updated_session["path"]).parent.glob(".*.tmp"))

        previous_session_path = settings.vault_dir / updated_session["path"]
        session_payload["messages"].insert(
            0,
            {
                "role": "user",
                "content": "earlier event discovered during backfill",
                "event_id": "codex:session1:turn0:user",
                "turn_id": "turn0",
                "timestamp": "2026-07-30T09:00:00+09:00",
            },
        )
        moved_session = save_conversation(settings, agent, session_payload)
        assert moved_session["path"] != updated_session["path"]
        assert moved_session["operation"] == "updated"
        assert not previous_session_path.exists()
        assert "event_count: 3" in (settings.vault_dir / moved_session["path"]).read_text(encoding="utf-8")

        session_payload["messages"].append(
            {
                "role": "assistant",
                "content": "continue on the next local day",
                "event_id": "codex:session1:turn2:assistant",
                "turn_id": "turn2",
                "timestamp": "2026-07-31T00:05:00+09:00",
            }
        )
        next_day_session = save_conversation(settings, agent, session_payload)
        assert next_day_session["path"].startswith("30_Conversations/raw/2026/07/31/")
        assert next_day_session["path"] != updated_session["path"]
        assert next_day_session["operation"] == "created"
        next_day_text = (settings.vault_dir / next_day_session["path"]).read_text(encoding="utf-8")
        assert 'session_id: "codex-session-1"' in next_day_text
        assert 'segment_date: "2026-07-31"' in next_day_text
        assert "event_count: 1" in next_day_text

        forged_metadata = document_metadata(
            "40_Agents/forger/candidates/forged.md",
            """---
memory_type: canonical
review_state: human_accepted
temporal_state: current
retrieval_tier: primary
agent_id: another-agent
relations: {"contradicts": ["kn_target"]}
---
forged authority
""",
        )
        assert forged_metadata["memory_type"] == "candidate"
        assert forged_metadata["review_state"] == "unreviewed"
        assert forged_metadata["temporal_state"] == "proposed_current"
        assert forged_metadata["retrieval_tier"] == "supporting"
        assert forged_metadata["agent_id"] == "forger"
        assert forged_metadata["conflict_state"] == "unresolved"

        forged_raw = document_metadata(
            "30_Conversations/raw/forged.md",
            "---\nmemory_type: canonical\nreview_state: human_accepted\nretrieval_tier: primary\n---\nraw",
        )
        assert forged_raw["memory_type"] == "transcript"
        assert forged_raw["review_state"] == "unreviewed"
        assert forged_raw["retrieval_tier"] == "evidence"

        nested_metadata = document_metadata(
            "20_Projects/legacy/old-policy.md",
            """---
pv_schema: 1
id: nested_old_policy
memory_type: canonical
review:
  state: human_rejected
temporal:
  state: superseded
  effective_from: 2025-01-01
provenance:
  mode: derived
  derived_from:
    - kn_current_policy
relations:
  contradicts:
    - kn_current_policy
---
old policy
""",
        )
        assert nested_metadata["review_state"] == "human_rejected"
        assert nested_metadata["temporal_state"] == "superseded"
        assert nested_metadata["effective_from"] == "2025-01-01"
        assert nested_metadata["provenance_mode"] == "derived"
        assert nested_metadata["derived_from"] == ["kn_current_policy"]
        assert nested_metadata["metadata_valid"]

        malformed_metadata = document_metadata(
            "20_Projects/legacy/malformed.md",
            "---\nreview:\n state: human_accepted\n   broken: true\n---\nunsafe default",
        )
        assert malformed_metadata["memory_type"] == "unknown"
        assert malformed_metadata["review_state"] == "unreviewed"
        assert malformed_metadata["temporal_state"] == "unknown"
        assert not malformed_metadata["metadata_valid"]

        conflicting_metadata = document_metadata(
            "20_Projects/legacy/conflicting.md",
            """---
pv_schema: 1
memory_type: canonical
review_state: human_accepted
review:
  state: human_rejected
---
conflicting authority
""",
        )
        assert not conflicting_metadata["metadata_valid"]
        assert conflicting_metadata["memory_type"] == "unknown"
        assert conflicting_metadata["review_state"] == "unreviewed"
        assert conflicting_metadata["temporal_state"] == "unknown"

        note = save_agent_note(
            settings,
            agent,
            {
                "title": "PersonaVault: token rotation lesson",
                "body": "Current truth: the build failure was fixed by rotating the token.",
                "project": "PersonaVault",
                "tags": ["knowledge-candidate", "lesson"],
                "note_type": "proposal",
                "note_kind": "lesson",
                "outcome": "success",
                "subject_id": "persona-vault-token-rotation",
                "applicability": {"repository": "persona-vault-gateway"},
                "provenance": {"mode": "direct_observation", "evidence_refs": ["run_token_rotation"]},
                "relations": {"supports": ["kn_token_policy"]},
            },
        )
        assert note["path"].startswith("30_Conversations/raw/")
        assert note["note_type"] == "proposal"
        note_text = (settings.vault_dir / note["path"]).read_text(encoding="utf-8")
        assert not list((settings.vault_dir / note["path"]).parent.glob(".*.tmp"))
        assert "memory_type: transcript" in note_text
        assert 'capture_kind: "agent_note"' in note_text
        assert 'note_type: "proposal"' in note_text
        assert '"outcome": "success"' in note_text
        assert '"evidence_refs": [' in note_text
        assert '"run_token_rotation"' in note_text
        note_metadata = document_metadata(note["path"], note_text)
        assert note_metadata["memory_type"] == "transcript"
        assert note_metadata["projects"] == ["PersonaVault"]
        assert note_metadata["kind"] == "lesson"
        assert note_metadata["outcome"] == "success"
        assert note_metadata["subject_id"] == "persona-vault-token-rotation"
        assert note_metadata["provenance_mode"] == "direct_observation"
        assert note_metadata["applicability"] == {"repository": "persona-vault-gateway"}

        reported_note = save_agent_note(
            settings,
            agent,
            {
                "title": "Reported context",
                "body": "A human reported this context.",
                "session_id": "shared-source-session",
            },
        )
        reported_text = (settings.vault_dir / reported_note["path"]).read_text(encoding="utf-8")
        assert '"mode": "reported"' in reported_text
        reported_metadata = document_metadata(reported_note["path"], reported_text)
        assert reported_metadata["provenance_mode"] == "reported"
        second_session_note = save_agent_note(
            settings,
            agent,
            {
                "title": "Second reported context",
                "body": "A second note from the same source session.",
                "session_id": "shared-source-session",
            },
        )
        assert second_session_note["path"] != reported_note["path"]
        assert '"source_session_id": "shared-source-session"' in reported_text

        try:
            save_agent_note(settings, agent, {"title": "Unsafe", "body": "No", "note_type": "canonical"})
        except ValueError:
            pass
        else:
            raise AssertionError("agent note accepted an invalid type")

        for invalid_note in (
            {"title": "--help", "body": "not a note"},
            {"title": "Empty", "body": "   "},
            {"title": "Legacy empty", "body": "(empty memo)"},
            {"title": "Smoke", "body": "temporary marker", "note_kind": "smoke-test"},
            {"title": "Help", "body": "usage output", "note_kind": "help-output"},
            {
                "title": "Invalid repository",
                "body": "invalid source",
                "repository_sources": [{"repo_id": "demo", "commit": "latest", "path": "../secret"}],
            },
        ):
            try:
                save_agent_note(settings, agent, invalid_note)
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid note was accepted: {invalid_note['title']}")

        try:
            target_path(settings, "../bad.md", ["30_Conversations/raw"])
        except ValueError:
            pass
        else:
            raise AssertionError("path traversal was not rejected")

        for blocked in (
            ".obsidian",
            "90_Private",
            "30_Conversations",
            "30_Conversations/summaries",
            "30_Conversations/important",
            "50_Knowledge",
        ):
            try:
                upsert_agent(settings.db_path, f"blocked-{len(blocked)}", generate_token(), ["agent-memo"], [blocked])
            except ValueError:
                pass
            else:
                raise AssertionError(f"blocked root was accepted: {blocked}")

        assert comma_list("conversation-log, agent-memo") == ["conversation-log", "agent-memo"]
        assert allowed_roots_for_agent("mac-codex") == ["30_Conversations/raw"]
        assert core.bundle_accepts({"memory_type": "transcript"}, "evidence")
        assert core.bundle_accepts({"memory_type": "episode"}, "evidence")
        assert not core.bundle_accepts({"memory_type": "canonical"}, "evidence")
        try:
            upsert_agent(
                settings.db_path,
                "root-owner",
                generate_token(),
                ["agent-memo"],
                ["40_Agents/someone-else"],
            )
        except ValueError:
            pass
        else:
            raise AssertionError("agent was allowed to own another agent's root")

        try:
            target_path(settings, "40_Agents/linux-container-a/not-markdown.txt", ["40_Agents/linux-container-a"])
        except ValueError:
            pass
        else:
            raise AssertionError("non-Markdown vault write was accepted")

        try:
            target_path(settings, "40_Agents/linux-container-a/.git/config.md", ["40_Agents/linux-container-a"])
        except ValueError:
            pass
        else:
            raise AssertionError("technical .git path was accepted")

        outside = root / "outside"
        outside.mkdir()
        linked_parent = settings.vault_dir / "40_Agents/linux-container-a/linked"
        linked_parent.parent.mkdir(parents=True, exist_ok=True)
        linked_parent.symlink_to(outside, target_is_directory=True)
        try:
            target_path(settings, "40_Agents/linux-container-a/linked/escape.md", ["40_Agents/linux-container-a"])
        except ValueError:
            pass
        else:
            raise AssertionError("static symlink escaped the vault")
        assert disable_agent(settings.db_path, "linux-container-a")
        assert lookup_agent(settings.db_path, token) is None
        new_token = generate_token()
        upsert_agent(
            settings.db_path,
            "linux-container-a",
            new_token,
            ["agent-memo"],
            allowed_roots_for_agent("linux-container-a"),
        )
        assert lookup_agent(settings.db_path, new_token)
        cookie = make_admin_cookie("password", now=100)
        assert valid_admin_cookie(cookie, "password", now=100)
        assert not valid_admin_cookie(cookie, "wrong", now=100)
        assert not valid_admin_cookie(cookie, "password", now=100 + 13 * 60 * 60)
        assert 'href="/admin/tokens"' in token_created_html(new_token)

        (settings.vault_dir / "50_Knowledge").mkdir(parents=True)
        (settings.vault_dir / "50_Knowledge/rag.md").write_text(
            "# RAG Note\n\nSemantic retrieval remembers persona vault decisions.",
            encoding="utf-8",
        )
        (settings.vault_dir / "90_Private").mkdir()
        (settings.vault_dir / "90_Private/personal.md").write_text(
            """---
id: private_archive
rag_index: false
---
# Personal context

pvgbroadreadarchive987
""",
            encoding="utf-8",
        )
        (settings.vault_dir / ".obsidian").mkdir()
        (settings.vault_dir / ".obsidian/config.md").write_text(
            "semantic retrieval should not index this",
            encoding="utf-8",
        )
        (settings.vault_dir / ".tmp/curating/demo").mkdir(parents=True)
        (settings.vault_dir / ".tmp/curating/demo/draft.md").write_text(
            "curation drafts should not be indexed",
            encoding="utf-8",
        )

        other_agent = {
            "agent_id": "claude-agent",
            "scopes": ["agent-memo", "vault-rag"],
            "allowed_roots": ["30_Conversations/raw"],
        }
        evidence_note = save_agent_note(
            settings,
            other_agent,
            {
                "title": "Shared retry trap",
                "body": (
                    "Situation: shared retry trap.\n"
                    "Action: repeated the stale command.\n"
                    "Outcome: it failed.\n"
                    "Reuse guidance: clear the stale state first."
                ),
                "project": "PersonaVault",
                "note_type": "observation",
                "note_kind": "debugging",
                "outcome": "failure",
                "applicability": {"operating_system": "linux"},
                "provenance": {
                    "mode": "direct_observation",
                    "evidence_refs": ["run_claude_01"],
                    "method_refs": [note["note_id"]],
                },
            },
        )
        assert evidence_note["path"].startswith("30_Conversations/raw/")

        (settings.vault_dir / "50_Knowledge/history.md").write_text(
            """---
id: kn_history
memory_type: canonical
temporal_state: superseded
retrieval_tier: history
---
# Historical policy

historical-policy-marker
""",
            encoding="utf-8",
        )
        conflict_dir = settings.vault_dir / "40_Agents/claude-agent/candidates"
        conflict_dir.mkdir(parents=True, exist_ok=True)
        (conflict_dir / "conflict.md").write_text(
            """---
id: cand_conflict
memory_type: candidate
conflict_state: unresolved
relations: {"contradicts": ["kn_rag"]}
---
# Conflicting policy

conflict-policy-marker
""",
            encoding="utf-8",
        )

        points: list[dict] = []
        collection_exists = False
        semantic_query_disabled = False
        original_qdrant_json = core.qdrant_json

        def fake_qdrant_json(settings: Settings, method: str, path: str, body: dict | None = None) -> dict:
            nonlocal collection_exists, points, semantic_query_disabled
            if path.endswith("/exists"):
                return {"result": {"exists": collection_exists}}
            if method == "DELETE":
                collection_exists = False
                points = []
                return {"status": "ok"}
            if method == "PUT" and path == f"/collections/{settings.qdrant_collection}":
                collection_exists = True
                points = []
                return {"status": "ok"}
            if method == "PUT" and path.endswith("/points?wait=true"):
                incoming = {point["id"]: point for point in (body or {})["points"]}
                points = [point for point in points if point["id"] not in incoming]
                points.extend(incoming.values())
                return {"status": "ok"}
            if method == "PUT" and path.endswith("/points/payload?wait=true"):
                selected = set((body or {})["points"])
                for point in points:
                    if point["id"] in selected:
                        point["payload"] = (body or {})["payload"]
                return {"status": "ok"}
            if method == "POST" and path.endswith("/points/count"):
                return {"result": {"count": len(points)}}
            if method == "POST" and path.endswith("/points/scroll"):
                ordered = sorted(points, key=lambda point: point["id"])
                offset = (body or {}).get("offset")
                start = next((index + 1 for index, point in enumerate(ordered) if point["id"] == offset), 0)
                page = ordered[start : start + (body or {}).get("limit", 10)]
                selected = (body or {}).get("with_payload")
                result = [
                    {
                        "id": point["id"],
                        "payload": {key: point["payload"].get(key) for key in selected}
                        if isinstance(selected, list)
                        else point["payload"],
                    }
                    for point in page
                ]
                next_offset = page[-1]["id"] if start + len(page) < len(ordered) else None
                return {"result": {"points": result, "next_page_offset": next_offset}}
            if method == "POST" and path.endswith("/points/delete?wait=true"):
                selected = set((body or {})["points"])
                points = [point for point in points if point["id"] not in selected]
                return {"status": "ok"}
            if method == "POST" and path.endswith("/points/query"):
                if semantic_query_disabled:
                    return {"result": {"points": []}}
                query = (body or {})["query"]
                ranked = []
                for point in points:
                    if not qdrant_filter_matches(point["payload"], (body or {}).get("filter")):
                        continue
                    score = sum(a * b for a, b in zip(query, point["vector"]))
                    ranked.append({"id": point["id"], "score": score, "payload": point["payload"]})
                ranked.sort(key=lambda item: item["score"], reverse=True)
                return {"result": {"points": ranked[: (body or {}).get("limit", 10)]}}
            raise AssertionError(f"unexpected qdrant call: {method} {path}")

        core.qdrant_json = fake_qdrant_json
        try:
            result = search_vault(settings, agent, "semantic retrieval", refresh=True)
            assert result["index"]["store"] == "qdrant"
            assert result["results"][0]["path"] == "50_Knowledge/rag.md"
            assert result["results"][0]["label"] == "CANONICAL - CURRENT"
            assert result["groups"]
            assert ".obsidian" not in result["context"]
            assert rag_readiness(settings)["qdrant"] == "ok"
            assert points and all("memory_type" in point["payload"] for point in points)
            assert all(not point["payload"]["path"].startswith(".tmp/") for point in points)
            private_points = [point for point in points if point["payload"]["path"] == "90_Private/personal.md"]
            assert private_points and private_points[0]["payload"]["retrieval_tier"] == "archive"

            assert core.RAG_INDEX_LOCK.acquire(blocking=False)
            try:
                try:
                    core.index_vault(settings)
                except RuntimeError as exc:
                    assert "already in progress" in str(exc)
                else:
                    raise AssertionError("concurrent RAG indexing was accepted")
            finally:
                core.RAG_INDEX_LOCK.release()

            core.set_rag_index_meta(settings, {"rebuild_state": "building"})
            partial_readiness = rag_readiness(settings)
            assert partial_readiness["status"] == "error"
            assert "not current" in partial_readiness["error"]
            core.set_rag_index_meta(settings, {"rebuild_state": "ready"})

            private_result = search_vault(settings, agent, "pvgbroadreadarchive987", limit=20)
            assert any(item["document_id"] == "private_archive" for item in private_result["results"])
            current_private = search_vault(settings, agent, "pvgbroadreadarchive987", limit=20, bundle="current")
            assert all(item["document_id"] != "private_archive" for item in current_private["results"])

            experiences = search_vault(
                settings,
                agent,
                "shared retry trap",
                bundle="experiences",
                context={"projects": ["PersonaVault"], "operating_system": "linux"},
            )
            assert experiences["bundle_type"] == "experiences"
            assert experiences["results"][0]["path"] == evidence_note["path"]
            assert experiences["results"][0]["agent_id"] == "claude-agent"
            assert experiences["results"][0]["label"] == "EVIDENCE - RAW TRANSCRIPT"
            assert experiences["results"][0]["provenance_mode"] == "direct_observation"

            history = search_vault(settings, agent, "historical-policy-marker", bundle="history")
            assert history["results"][0]["document_id"] == "kn_history"
            assert history["results"][0]["temporal_state"] == "superseded"
            assert "timeline" in history["groups"][0]

            conflicts = search_vault(settings, agent, "conflict-policy-marker", bundle="conflicts")
            assert conflicts["results"][0]["document_id"] == "cand_conflict"
            assert conflicts["groups"][0]["conflict_state"] == "unresolved"
            assert "claims" in conflicts["groups"][0]
            assert conflicts["groups"][0]["winner"] is None

            before = result["index"]["chunks"]
            (settings.vault_dir / "40_Agents/linux-container-a/rag-refresh.md").write_text(
                "# Fresh RAG\n\nfresh automatic refresh marker",
                encoding="utf-8",
            )
            result = search_vault(settings, agent, "fresh automatic refresh", refresh=False)
            assert result["index"]["chunks"] == before
            assert result["index"]["updated"] == 0
            assert result["index"]["stale"] is True
            assert result["index"]["fallback_reason"] == "stale_index"
            assert result["results"][0]["path"] == "40_Agents/linux-container-a/rag-refresh.md"

            refreshed = search_vault(settings, agent, "fresh automatic refresh", refresh=True)
            assert refreshed["index"]["chunks"] > before
            assert refreshed["index"]["stale"] is False

            semantic_query_disabled = True
            (settings.vault_dir / "40_Agents/linux-container-a/keyword.md").write_text(
                "# Keyword\n\nliteral-only-marker",
                encoding="utf-8",
            )
            result = search_vault(settings, agent, "literal-only-marker", refresh=False)
            assert result["results"][0]["path"] == "40_Agents/linux-container-a/keyword.md"
            assert result["results"][0]["match"] == "keyword"
            assert result["index"]["fallback_reason"] == "stale_index"

            semantic_query_disabled = False
            search_vault(settings, agent, "literal-only-marker", refresh=True)
            semantic_query_disabled = True

            original_embed_query = core.embed_query
            core.embed_query = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                core.EmbeddingLimitError("simulated embedding limit")
            )
            try:
                fallback = search_vault(settings, agent, "literal-only-marker", refresh=False)
            finally:
                core.embed_query = original_embed_query
            assert fallback["results"][0]["path"] == "40_Agents/linux-container-a/keyword.md"
            assert fallback["results"][0]["match"] == "keyword"
            assert fallback["index"]["search_mode"] == "keyword"
            assert fallback["index"]["fallback_reason"] == "embedding_limit"

            duplicate_id_documents = core.analyzed_documents(
                [
                    {"document_id": "shared", "path": "first.md", "text": "first"},
                    {"document_id": "shared", "path": "second.md", "text": "second"},
                ]
            )
            assert [(item["path"], item["text"]) for item in duplicate_id_documents] == [
                ("first.md", "first"),
                ("second.md", "second"),
            ]

            incremental_path = settings.vault_dir / "50_Knowledge/incremental.md"
            incremental_text = "# Incremental\n\n" + "\n\n".join(
                f"section-{index} " + ("MIDDLE_OLD_0000" if index == 3 else "body") + " x" * 500
                for index in range(8)
            )
            incremental_path.write_text(incremental_text, encoding="utf-8")
            metadata_path = settings.vault_dir / "50_Knowledge/metadata-only.md"
            metadata_path.write_text("---\noutcome: success\n---\n# Metadata only\n\nstable body", encoding="utf-8")

            cloudflare_batches: list[dict] = []
            cloudflare_fail_after: int | None = None
            original_cloudflare_request = core.cloudflare_embedding_request

            def fake_cloudflare_request(
                _settings: Settings,
                body: dict,
                expected_count: int,
            ) -> list[list[float]]:
                if cloudflare_fail_after is not None and len(cloudflare_batches) >= cloudflare_fail_after:
                    raise RuntimeError("simulated Cloudflare checkpoint failure")
                cloudflare_batches.append(body)
                return [[1.0] * core.CLOUDFLARE_EMBEDDING_DIMENSIONS for _ in range(expected_count)]

            core.cloudflare_embedding_request = fake_cloudflare_request
            try:
                cloudflare_settings = Settings(
                    settings.vault_dir,
                    settings.db_path,
                    settings.host_id,
                    embedding_batch_size=2,
                    cloudflare_account_id="account-id",
                    cloudflare_api_token="api-token",
                    qdrant_url=settings.qdrant_url,
                    qdrant_collection=settings.qdrant_collection,
                )
                cloudflare_index = core.index_vault(cloudflare_settings)
                assert sum(len(batch["documents"]) for batch in cloudflare_batches) == cloudflare_index["chunks"]
                assert all(len(batch["documents"]) <= 2 for batch in cloudflare_batches)
                assert cloudflare_index["provider"] == "cloudflare"
                assert cloudflare_index["dimension"] == core.CLOUDFLARE_EMBEDDING_DIMENSIONS

                legacy_count = len(points)
                legacy_ids = {point["id"] for point in points}
                for point in points:
                    point["payload"].pop("embedding_input_hash", None)
                    point["payload"].pop("payload_hash", None)
                core.set_rag_index_meta(cloudflare_settings, {"schema": "4", "rebuild_state": "ready"})
                (settings.vault_dir / "50_Knowledge/checkpoint-a.md").write_text(
                    "# Checkpoint A\n\nfirst new file",
                    encoding="utf-8",
                )
                (settings.vault_dir / "50_Knowledge/checkpoint-b.md").write_text(
                    "# Checkpoint B\n\nsecond new file",
                    encoding="utf-8",
                )
                checkpoint_settings = Settings(
                    settings.vault_dir,
                    settings.db_path,
                    settings.host_id,
                    embedding_batch_size=1_000,
                    cloudflare_account_id="account-id",
                    cloudflare_api_token="api-token",
                    qdrant_url=settings.qdrant_url,
                    qdrant_collection=settings.qdrant_collection,
                )
                cloudflare_batches.clear()
                cloudflare_fail_after = 1
                try:
                    core.index_vault(checkpoint_settings)
                except RuntimeError as exc:
                    assert "checkpoint failure" in str(exc)
                else:
                    raise AssertionError("simulated checkpoint failure was ignored")
                assert legacy_ids <= {point["id"] for point in points}
                assert len(points) == legacy_count + 1
                completed = sum("embedding_input_hash" in point["payload"] for point in points)
                assert 0 < completed < legacy_count
                checkpoint_meta = core.rag_index_meta(cloudflare_settings)
                # An interrupted schema migration must not look current until it finishes.
                assert checkpoint_meta["schema"] == "4"
                assert checkpoint_meta["rebuild_state"] == "building"
                assert checkpoint_meta["fingerprint"] == ""
                assert not core.qdrant_index_current(cloudflare_settings, allow_stale=True)
                assert rag_readiness(cloudflare_settings)["status"] == "error"
                interrupted = search_vault(cloudflare_settings, agent, "literal-only-marker")
                assert interrupted["index"]["fallback_reason"] == "index_building"

                cloudflare_fail_after = None
                cloudflare_batches.clear()
                resumed_index = core.index_vault(cloudflare_settings)
                assert 0 < resumed_index["updated"] < resumed_index["chunks"]
                assert sum(len(batch["documents"]) for batch in cloudflare_batches) == resumed_index["updated"]
                assert core.rag_index_meta(cloudflare_settings)["schema"] == core.RAG_INDEX_SCHEMA
                assert core.qdrant_index_current(cloudflare_settings)

                points.extend(
                    {
                        "id": core.qdrant_point_id(f"stale/{index}.md", 0),
                        "vector": [0.0] * core.CLOUDFLARE_EMBEDDING_DIMENSIONS,
                        "payload": {"embedding_input_hash": "stale", "payload_hash": "stale"},
                    }
                    for index in range(300)
                )
                cloudflare_batches.clear()
                unchanged_index = core.index_vault(cloudflare_settings)
                assert unchanged_index["updated"] == 0
                assert unchanged_index["payload_updated"] == 0
                assert unchanged_index["deleted"] == 300
                assert not cloudflare_batches

                incremental_path.write_text(
                    incremental_text.replace("MIDDLE_OLD_0000", "MIDDLE_NEW_0000"),
                    encoding="utf-8",
                )
                cloudflare_batches.clear()
                incremental_index = core.index_vault(cloudflare_settings)
                assert 0 < incremental_index["updated"] < incremental_index["chunks"]
                assert sum(len(batch["documents"]) for batch in cloudflare_batches) == incremental_index["updated"]

                metadata_path.write_text("---\noutcome: failure\n---\n# Metadata only\n\nstable body", encoding="utf-8")
                cloudflare_batches.clear()
                metadata_index = core.index_vault(cloudflare_settings)
                assert metadata_index["updated"] == 0
                assert metadata_index["payload_updated"] == 1
                assert not cloudflare_batches

                before_delete = metadata_index["chunks"]
                incremental_path.unlink()
                metadata_path.unlink()
                deleted_index = core.index_vault(cloudflare_settings)
                assert deleted_index["updated"] == 0
                assert deleted_index["deleted"] > 1
                assert deleted_index["chunks"] < before_delete
            finally:
                core.cloudflare_embedding_request = original_cloudflare_request
        finally:
            core.qdrant_json = original_qdrant_json

        def fake_qdrant_error(*args: object, **kwargs: object) -> dict:
            raise RuntimeError("qdrant offline")

        core.qdrant_json = fake_qdrant_error
        try:
            readiness = rag_readiness(settings)
            assert readiness["status"] == "error"
            assert readiness["qdrant"] == "error"
        finally:
            core.qdrant_json = original_qdrant_json

        def fake_qdrant_count_error(settings: Settings, method: str, path: str, body: dict | None = None) -> dict:
            if path.endswith("/exists"):
                return {"result": {"exists": True}}
            raise RuntimeError("qdrant count offline")

        core.qdrant_json = fake_qdrant_count_error
        try:
            readiness = rag_readiness(settings)
            assert readiness["status"] == "error"
            assert "count offline" in readiness["error"]
        finally:
            core.qdrant_json = original_qdrant_json

        try:
            search_vault(settings, {"agent_id": "limited", "scopes": [], "allowed_roots": []}, "semantic")
        except PermissionError:
            pass
        else:
            raise AssertionError("vault-rag scope was not required")

        original_urlopen = core.urllib.request.urlopen
        original_sleep = core.time.sleep
        embedding_settings = Settings(
            settings.vault_dir,
            settings.db_path,
            settings.host_id,
            embedding_batch_size=2,
            cloudflare_account_id="account-id",
            cloudflare_api_token="api-token",
        )

        class FakeEmbeddingResponse:
            def __init__(self, payload: dict):
                self.payload = json.dumps(payload).encode()

            def __enter__(self) -> "FakeEmbeddingResponse":
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def read(self) -> bytes:
                return self.payload

        captured_embedding_bodies: list[dict] = []

        def successful_embedding_response(request: urllib.request.Request, **_kwargs: object) -> FakeEmbeddingResponse:
            body = json.loads(request.data or b"{}")
            captured_embedding_bodies.append(body)
            texts = body.get("documents") or body.get("queries") or []
            return FakeEmbeddingResponse(
                {
                    "success": True,
                    "result": {
                        "data": [
                            [1.0] * core.CLOUDFLARE_EMBEDDING_DIMENSIONS
                            for _ in texts
                        ]
                    },
                }
            )

        core.urllib.request.urlopen = successful_embedding_response
        core.time.sleep = lambda _seconds: None
        try:
            document_vectors = core.embed_documents(embedding_settings, ["one", "two", "three"])
            assert len(document_vectors) == 3
            assert [body["documents"] for body in captured_embedding_bodies] == [["one", "two"], ["three"]]

            query_vector, query_model = core.embed_query(embedding_settings, "현재 정책은?")
            assert len(query_vector) == core.CLOUDFLARE_EMBEDDING_DIMENSIONS
            assert query_model == core.CLOUDFLARE_EMBEDDING_MODEL
            assert captured_embedding_bodies[-1] == {
                "queries": ["현재 정책은?"],
                "instruction": core.CLOUDFLARE_EMBEDDING_INSTRUCTION,
            }

            transient_attempts = 0

            def transient_then_success(
                request: urllib.request.Request,
                **kwargs: object,
            ) -> FakeEmbeddingResponse:
                nonlocal transient_attempts
                transient_attempts += 1
                if transient_attempts == 1:
                    raise core.urllib.error.HTTPError(
                        request.full_url,
                        503,
                        "unavailable",
                        {"Retry-After": "0"},
                        io.BytesIO(b'{"errors":[{"code":3040,"message":"capacity"}]}'),
                    )
                return successful_embedding_response(request, **kwargs)

            core.urllib.request.urlopen = transient_then_success
            core.embed_query(embedding_settings, "retry")
            assert transient_attempts == 2

            core.urllib.request.urlopen = lambda *_args, **_kwargs: FakeEmbeddingResponse(
                {"success": True, "result": {"data": [[1.0, 2.0]]}}
            )
            try:
                core.embed_query(embedding_settings, "bad dimension")
            except RuntimeError as exc:
                assert "embedding dimension must be 1024" in str(exc)
            else:
                raise AssertionError("invalid Cloudflare embedding dimension was accepted")

            def quota_exceeded(request: urllib.request.Request, **_kwargs: object) -> object:
                raise core.urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "limited",
                    {},
                    io.BytesIO(b'{"errors":[{"code":3036,"message":"daily allocation used"}]}'),
                )

            core.urllib.request.urlopen = quota_exceeded
            try:
                core.embed_query(embedding_settings, "quota")
            except core.EmbeddingLimitError as exc:
                assert "blocked until" in str(exc)
                assert "daily allocation used" in str(exc)
            else:
                raise AssertionError("Cloudflare daily limit was retried")

            core.urllib.request.urlopen = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("blocked embedding called Cloudflare")
            )
            try:
                core.embed_query(embedding_settings, "still blocked")
            except core.EmbeddingLimitError:
                pass
            else:
                raise AssertionError("Cloudflare embedding cooldown was ignored")
            assert core.rag_index_meta(settings).get("embedding_blocked_until")
            core.set_rag_index_meta(settings, {"embedding_blocked_until": "2000-01-01T00:00:00+00:00"})

            retry_calls = []
            original_index_vault = core.index_vault
            original_qdrant_index_current = core.qdrant_index_current
            core.index_vault = lambda *_args, **_kwargs: retry_calls.append("indexed")
            core.qdrant_index_current = lambda *_args, **_kwargs: False
            try:
                blocked_until = datetime(2026, 8, 12, tzinfo=timezone.utc)
                core.set_rag_index_meta(settings, {"embedding_blocked_until": blocked_until.isoformat()})
                assert not core.retry_due_embeddings(settings, blocked_until)
                assert core.retry_due_embeddings(
                    settings,
                    blocked_until + timedelta(seconds=core.EMBEDDING_RETRY_GRACE_SECONDS),
                )
                assert retry_calls == ["indexed"]
                assert core.rag_index_meta(settings)["embedding_blocked_until"] == ""
            finally:
                core.index_vault = original_index_vault
                core.qdrant_index_current = original_qdrant_index_current
        finally:
            core.urllib.request.urlopen = original_urlopen
            core.time.sleep = original_sleep

        original_index_vault = app_module.index_vault
        old_admin_password = os.environ.get("ADMIN_PASSWORD")
        old_vault_dir = os.environ.get("VAULT_DIR")
        old_db_path = os.environ.get("DB_PATH")
        os.environ["ADMIN_PASSWORD"] = "password"
        os.environ["VAULT_DIR"] = str(settings.vault_dir)
        os.environ["DB_PATH"] = str(settings.db_path)

        # A real same-origin form POST: signed session cookie plus the CSRF token bound to it.
        session_cookie = app_module.make_admin_cookie("password")
        form_body = urllib.parse.urlencode(
            {app_module.CSRF_FIELD: app_module.csrf_token_for("password", session_cookie)}
        ).encode()

        async def receive() -> dict:
            return {"type": "http.request", "body": form_body, "more_body": False}

        def rebuild_request() -> Request:
            return Request(
                {
                    "type": "http",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/admin/rag/rebuild",
                    "query_string": b"",
                    "server": ("testserver", 80),
                    "client": ("127.0.0.1", 50000),
                    "headers": [
                        (b"host", b"testserver"),
                        (b"cookie", f"{app_module.ADMIN_COOKIE}={session_cookie}".encode()),
                        (b"content-type", b"application/x-www-form-urlencoded"),
                        (b"content-length", str(len(form_body)).encode()),
                    ],
                },
                receive,
            )

        def fake_index_vault(settings: Settings) -> dict[str, int | str]:
            raise ValueError("bad embedding provider")

        app_module.index_vault = fake_index_vault
        core.qdrant_json = fake_qdrant_error
        old_embedding_provider = os.environ.get("EMBEDDING_PROVIDER")

        def fake_qdrant_empty(*args: object, **kwargs: object) -> dict:
            return {"result": {"exists": False}}

        try:
            response = app_module.readyz()
            assert response.status_code == 503
            os.environ["EMBEDDING_PROVIDER"] = "typo"
            core.qdrant_json = fake_qdrant_empty
            response = app_module.readyz()
            assert response.status_code == 503
            response = asyncio.run(app_module.admin_rag_rebuild(rebuild_request()))
            assert response.status_code == 503
            assert b"bad embedding provider" in response.body
        finally:
            app_module.index_vault = original_index_vault
            core.qdrant_json = original_qdrant_json
            if old_embedding_provider is None:
                os.environ.pop("EMBEDDING_PROVIDER", None)
            else:
                os.environ["EMBEDDING_PROVIDER"] = old_embedding_provider
            if old_admin_password is None:
                os.environ.pop("ADMIN_PASSWORD", None)
            else:
                os.environ["ADMIN_PASSWORD"] = old_admin_password
            if old_vault_dir is None:
                os.environ.pop("VAULT_DIR", None)
            else:
                os.environ["VAULT_DIR"] = old_vault_dir
            if old_db_path is None:
                os.environ.pop("DB_PATH", None)
            else:
                os.environ["DB_PATH"] = old_db_path

    run_fake_qdrant_smoke()
    run_search_regression_checks()
    run_conversation_merge_checks()
    run_gateway_http_smoke()


if __name__ == "__main__":
    main()
