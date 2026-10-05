"""Hash-bound review records and final search checks; never apply Markdown or authorize it."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from . import core
from .wiki import MARKED_DISPOSITIONS, _SRC_RE, _cells, _review_items


def digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def staging_path(vault: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(vault.resolve() / ".tmp" / "curating"):
        raise ValueError("review artifacts must be inside Vault .tmp/curating/")
    return resolved


def knowledge_path(path: str) -> bool:
    return (
        isinstance(path, str) and PurePosixPath(path).as_posix() == path
        and ".." not in PurePosixPath(path).parts and path.endswith(".md")
        and not any(part in core.RAG_EXCLUDED_DIRS for part in PurePosixPath(path).parts)
        and path.startswith(("10_User/", "20_Projects/", "50_Knowledge/", "30_Conversations/summaries/"))
    )


def concurrent_raw_captures(vault: Path, plan: dict, before: list[dict], current: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Exclude only verified, post-cutoff Gateway appends from batch-scope checks."""
    cutoff = date.fromisoformat(plan["before"])
    original = {doc["path"]: doc for doc in before}
    live = {doc["path"]: doc for doc in current}
    scoped = set(plan["selected"]["source_paths"])
    scoped.update(node["path"] for node in plan["selected"]["graph"]["nodes"] if "sha256" in node)
    scoped.update(plan["review"].get("dependencies", []))
    scoped.update(target for item in _review_items(plan["review"]["items"]) for target in item["targets"])
    scoped.add("CURATOR.md")
    concurrent: dict[str, int] = {}

    def native(path: str, content: str) -> tuple[dict, str, list[dict], str] | None:
        try:
            metadata, prefix, messages, suffix = core.parse_conversation(content)
            times = [core.parse_time(message["timestamp"]) for message in messages]
            stamp = times[0]
            segment_date = stamp.date().isoformat()
            agent, session = metadata["agent_id"], metadata["session_id"]
            key = sha256(f"{agent}\0{session}".encode()).hexdigest()[:20]
            expected = f"30_Conversations/raw/{stamp:%Y/%m/%d}/{stamp:%Y%m%d-%H%M%S}-{agent}-{key}.md"
            if (
                path != expected or date.fromisoformat(segment_date) < cutoff
                or metadata.get("pv_schema") != 1 or metadata.get("memory_type") != "transcript"
                or metadata.get("type") != "conversation" or metadata.get("status") != "raw"
                or metadata.get("segment_date") != segment_date
                or metadata.get("conversation_id") != f"conv_{key}_{stamp:%Y%m%d}"
                or metadata.get("id") != metadata["conversation_id"]
                or metadata.get("capture_kind") not in {"conversation", "agent_note"}
                or times != sorted(times) or any(time.date().isoformat() != segment_date for time in times)
                or any(not metadata.get(field) for field in ("started_at", "observed_at", "ended_at"))
                or core.parse_time(metadata["started_at"]) != stamp
                or core.parse_time(metadata["observed_at"]) != stamp
                or core.parse_time(metadata["ended_at"]) != times[-1]
            ):
                return None
            return metadata, prefix, messages, suffix
        except (core.ConversationConflictError, ValueError, KeyError, TypeError, IndexError):
            return None

    for path in original.keys() | live.keys():
        previous, latest = original.get(path), live.get(path)
        if (path in scoped or not latest or not path.startswith("30_Conversations/raw/")
                or previous and previous["document_hash"] == latest["document_hash"]):
            continue
        try:
            text = (vault / path).read_text(encoding="utf-8")
        except UnicodeError:
            continue
        if sha256(text.encode("utf-8")).hexdigest() != latest["document_hash"]:
            raise ValueError(f"raw capture changed during validation: {path}")
        parsed = native(path, text)
        if not parsed:
            continue
        metadata, prefix, messages, suffix = parsed
        old_messages: list[dict] = []
        if previous:
            try:
                old_text = subprocess.run(
                    ["git", "-C", str(vault), "show", f"{plan['revision']}:{path}"],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ).stdout.decode("utf-8")
            except UnicodeError:
                continue
            old = native(path, old_text)
            if not old:
                continue
            old_metadata, old_prefix, old_messages, old_suffix = old
            dynamic = {"started_at", "observed_at", "ended_at", "event_count"}
            if (
                {k: v for k, v in metadata.items() if k not in dynamic}
                != {k: v for k, v in old_metadata.items() if k not in dynamic}
                or prefix.split("\n---\n\n", 1)[1] != old_prefix.split("\n---\n\n", 1)[1]
                or suffix != old_suffix or messages[:len(old_messages)] != old_messages
                or len(messages) <= len(old_messages)
            ):
                continue
            live[path] = previous
        else:
            del live[path]
        concurrent[path] = len(messages) - len(old_messages)
    return list(live.values()), concurrent


def draft_file(vault: Path, target: str, draft: Any) -> Path:
    if not knowledge_path(target) or not isinstance(draft, str) or Path(draft).is_absolute():
        raise ValueError("draft requires a knowledge target and a Vault-relative staging path")
    path = staging_path(vault, vault / draft)
    if path.suffix != ".md":
        raise ValueError("draft must be a Markdown file")
    return path


def annotate_drafts(vault: Path, plan: dict) -> dict[str, int]:
    """Write `pvg-src` markers for review anchors into staged drafts only; additive, idempotent, all-or-nothing."""
    review = plan["review"]
    drafts = review.get("drafts", {})
    if not isinstance(drafts, dict):
        raise ValueError("review.drafts must map reviewed target paths to staged Markdown files")
    nodes = plan["selected"]["graph"]["nodes"]
    sessions = {node["path"]: node.get("session", "") for node in nodes if node["type"] == "source_file"}
    item_session = {node["id"]: sessions.get(node["path"], "") for node in nodes if node["type"] == "raw_item"}

    def quotes_of(value: Any) -> list[str] | None:
        """One quote or a list of quotes per target, so one item can mark several rows; None if malformed."""
        quotes = [value] if isinstance(value, str) else value
        return quotes if isinstance(quotes, list) and quotes and all(isinstance(q, str) and q.strip() for q in quotes) else None

    marks: dict[str, dict[str, set[str]]] = {}
    for entry in _review_items(review["items"]):
        anchors = entry.get("anchors")
        if anchors is None:
            continue
        if (
            entry["disposition"] not in MARKED_DISPOSITIONS or entry["id"] not in item_session
            or not isinstance(anchors, dict) or not set(anchors) <= set(entry["targets"]) & set(drafts)
            or any(quotes_of(value) is None for value in anchors.values())
        ):
            raise ValueError(f"anchors require a claim item of this plan and draft targets: {entry['id']}")
        for target, value in anchors.items():
            for quote in quotes_of(value):
                marks.setdefault(target, {}).setdefault(quote, set()).add(entry["id"])
    written, lines_marked = {}, {}
    for target, quotes in marks.items():
        path = draft_file(vault, target, drafts[target])
        text = path.read_bytes().decode("utf-8")  # not read_text: universal newlines would rewrite every CRLF
        body = core.markdown_body(text)  # frontmatter stays untouched
        user = core.document_metadata(target, text).get("kind") == "user_ledger"
        lines, marked, fenced = body.split("\n"), set(), False
        # Quotes match visible text only: not inside fenced code, and not inside an existing marker.
        visible = []
        for line in lines:
            fenced ^= line.lstrip().startswith(("```", "~~~"))
            visible.append("" if fenced else _SRC_RE.sub("", line))
        for quote, ids in quotes.items():
            hits = [index for index, line in enumerate(visible) if quote in line]
            # A heading would leak the marker into markdown_title().
            if len(hits) != 1 or lines[hits[0]].lstrip().startswith("#"):
                raise ValueError(f"anchor must match exactly one non-heading body line in {target}: {quote!r}")
            found: set[str] = set()
            line = _SRC_RE.sub(lambda match: found.update(match.group(1).split()) or "", lines[hits[0]]).rstrip()
            # sess: tokens (hash of the source session id) make the row's 독립 세션 count tool-written and cumulative.
            tokens = found | ids | ({item_session[item_id] for item_id in ids} - {""} if user else set())
            marker = f"<!-- pvg-src: {' '.join(sorted(tokens))} -->"
            cells = _cells(line)
            if user and not (cells and len(cells) == 7 and re.fullmatch(r"U-\d{3,}", cells[0])):
                raise ValueError(f"user_ledger anchor must be a 7-cell U-### row in {target}: {quote!r}")
            if cells is None:
                line = f"{line} {marker}"
            else:
                # In a table row the marker sits inside the last cell, so the row still ends with its closing pipe.
                head = line[:-1].rstrip()
                if user:
                    head = f"{head.rpartition('|')[0]}| {sum(token.startswith('sess:') for token in tokens)}"
                line = f"{head} {marker} |"
            lines[hits[0]] = line + "\r" * lines[hits[0]].endswith("\r")
            marked.add(hits[0])
        written[path], lines_marked[target] = text[: len(text) - len(body)] + "\n".join(lines), len(marked)
    for path, text in written.items():
        path.write_text(text, encoding="utf-8", newline="")
    return lines_marked


def projected_documents(vault: Path, plan: dict, before: list[dict]) -> list[dict]:
    """Read staged full-file targets and project approved operations in memory only."""
    review = plan["review"]
    entries = _review_items(review["items"])
    targets = {path for item in entries for path in item["targets"]}
    drafts = review.get("drafts", {})
    if not isinstance(drafts, dict) or not set(drafts) <= targets:
        raise ValueError("review.drafts must map reviewed target paths to staged Markdown files")
    documents = {doc["path"]: doc for doc in before}
    for target, draft in drafts.items():
        text = draft_file(vault, target, draft).read_text(encoding="utf-8", errors="replace")
        documents[target] = {
            **core.document_metadata(target, text), "path": target, "text": core.markdown_body(text),
            "document_hash": sha256(text.encode()).hexdigest(),
        }
    for path in review["delete_paths"]:
        documents.pop(path, None)
    return list(documents.values())


def validated_probes(plan: dict) -> list[dict]:
    probes = plan["review"].get("probes")
    if not isinstance(probes, list) or not 1 <= len(probes) <= 20:
        raise ValueError("review.probes requires 1..20 explicit search checks")
    for probe in probes:
        if (
            not isinstance(probe, dict)
            or not isinstance(probe.get("query"), str) or not 1 <= len(probe["query"].strip()) <= 500
            or probe.get("bundle") not in core.RAG_BUNDLES
            or probe.get("answer_state") not in {"supported", "evidence", "review_required", "abstain"}
        ):
            raise ValueError("probe requires query, bundle and expected answer_state")
        for key in ("expect_paths", "forbid_paths"):
            paths = probe.get(key, [])
            if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
                raise ValueError("probe paths must be lists of nonempty paths")
        if set(probe.get("expect_paths", [])) & set(probe.get("forbid_paths", [])):
            raise ValueError("probe cannot both expect and forbid a path")
    return probes


def review_binding(plan: dict, before: list[dict], after: list[dict]) -> dict:
    original = {doc["path"]: doc["document_hash"] for doc in before}
    current = {doc["path"]: doc["document_hash"] for doc in after}
    review = plan["review"]
    probes = validated_probes(plan)
    # Bind shared source context conservatively; only independent target edits reuse other groups.
    context_paths = set(plan["selected"]["source_paths"])
    context_paths.update(node["path"] for node in plan["selected"]["graph"]["nodes"] if "sha256" in node)
    dependencies = review.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(path, str) or path not in original for path in dependencies):
        raise ValueError("review.dependencies must list existing evidence/policy paths")
    context_paths.update(dependencies)
    context_paths.add("CURATOR.md")
    context = {path: original.get(path) for path in sorted(context_paths)}
    fingerprints = {}
    for entry in _review_items(review["items"]):
        targets = entry["targets"]
        patch = {path: {"before": original.get(path), "after": current.get(path)} for path in targets}
        fingerprints[entry["id"]] = digest({
            "context": context, "target_patch": patch,
            "item": {key: value for key, value in entry.items() if key != "ids"},
            "user_knowledge": review["user_knowledge"], "probes": probes,
        })
    recorded = plan.get("review_checkpoint", {})
    if not isinstance(recorded, dict):
        raise ValueError("review_checkpoint must be an object")
    required = sorted(key for key, value in fingerprints.items() if recorded.get(key) != value)
    patch_hash = digest({
        path: [original.get(path), current.get(path)] for path in sorted(original.keys() | current.keys())
        if original.get(path) != current.get(path)
    })
    return {
        "fingerprints": fingerprints,
        "reused_items": len(fingerprints) - len(required),
        "review_required_ids": required,
        "patch_sha256": patch_hash,
        "scope_sha256": digest({
            "plan": {key: value for key, value in plan.items() if key not in {"review_checkpoint", "finish_checkpoint"}},
            "patch_sha256": patch_hash,
        }),
    }


def finish_search(settings: core.Settings, plan: dict, documents: list[dict]) -> dict:
    """Reuse the existing index checkpoint; batch completion has no separate database.

    With an explicit EMBEDDING_PROVIDER=none the same probes run keyword-only against the unchanged vault:
    no index, Qdrant or embedding is used and no index metadata is written. cloudflare/hash stay fail-closed.
    """
    probes = validated_probes(plan)
    keyword_only = settings.embedding_provider == "none"
    if not keyword_only and not settings.db_path.is_file():
        raise ValueError("compact-finish requires the existing Gateway database and Qdrant settings")
    fingerprint = core.vault_fingerprint(settings.vault_dir)
    binding = digest({
        "plan": {key: value for key, value in plan.items() if key != "finish_checkpoint"},
        "documents": {doc["path"]: doc["document_hash"] for doc in documents},
        # The selected mode is part of the binding, so a checkpoint never carries over between modes.
        "index": ["none"] if keyword_only else [
            settings.qdrant_url, settings.qdrant_collection, settings.embedding_provider,
            core.selected_embedding_model(settings), core.RAG_INDEX_SCHEMA],
        "fingerprint": fingerprint,
    })
    checkpoint = plan.get("finish_checkpoint", {})
    if not isinstance(checkpoint, dict):
        raise ValueError("finish_checkpoint must be an object")
    if keyword_only:
        indexed = True  # There is no index to refresh; checkpoint reuse depends on the binding alone.
        if {doc["path"] for doc in documents} & set(plan["review"]["delete_paths"]):
            raise RuntimeError("retired sources remain in the vault")
    else:
        indexed = core.qdrant_index_current(settings)
        if not indexed:
            if not core.qdrant_index_current(settings, allow_stale=True):
                raise RuntimeError("finalization requires a healthy, compatible existing index; repair/rebuild it separately")
            core.index_vault(settings)
        if not core.qdrant_index_current(settings):
            raise RuntimeError("final index is not current; retry after indexing is available")
        count = core.qdrant_json(
            settings, "POST", f"/collections/{settings.qdrant_collection}/points/count",
            {"exact": True, "filter": {"must": [{"key": "path", "match": {"any": plan["review"]["delete_paths"]}}]}},
        )["result"]["count"]
        if type(count) is not int or count != 0:
            raise RuntimeError("retired sources remain in Qdrant")
    expected_checks = [{"query": probe["query"], "passed": True, "answer_state": probe["answer_state"]} for probe in probes]
    if indexed and checkpoint.get("binding") == binding and checkpoint.get("probes") == expected_checks:
        return {**checkpoint, "reused": True}
    checks = []
    for probe in probes:
        response = core.search_vault(
            settings, {"scopes": ["vault-rag"]}, probe["query"], limit=20, refresh=False, bundle=probe["bundle"],
        )
        index = response["index"]
        paths = {item["path"] for item in response["results"]}
        if keyword_only:
            # Only the explicit disabled-mode marker is acceptable; any other degradation is still a failure.
            degraded = (
                index.get("stale") or index.get("search_mode") != "keyword"
                or index.get("fallback_reason") != "semantic_disabled"
            )
        else:
            degraded = index.get("stale") or index.get("fallback_reason")
        if degraded:
            raise RuntimeError("search used a stale/fallback index; retry after embedding/index recovery")
        passed = (
            set(probe.get("expect_paths", [])) <= paths
            and not (set(probe.get("forbid_paths", [])) | set(plan["review"]["delete_paths"])) & paths
            and response["answer_state"]["state"] == probe["answer_state"]
        )
        checks.append({"query": probe["query"], "passed": passed, "answer_state": response["answer_state"]["state"]})
    if not all(check["passed"] for check in checks):
        return {"status": "blocked", "reason": "search_probe_failed", "probes": checks}
    if keyword_only:
        hashes = {doc["path"]: doc["document_hash"] for doc in core.vault_documents(settings)}
        if core.vault_fingerprint(settings.vault_dir) != fingerprint or hashes != {
            doc["path"]: doc["document_hash"] for doc in documents
        }:
            raise RuntimeError("vault changed during final search checks")
    elif not core.qdrant_index_current(settings):
        raise RuntimeError("index changed during final search checks")
    return {"binding": binding, "probes": checks, "reused": False, **({"semantic": "disabled"} if keyword_only else {})}
