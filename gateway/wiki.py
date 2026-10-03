from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import PurePosixPath
from typing import Any


__all__ = ["analyze_documents", "build_compaction_plan", "check_compaction", "bundle_groups", "health_report"]

_BUNDLES = {"auto", "current", "evidence", "experiences", "history", "conflicts"}
_CORROBORATABLE_KINDS = {"debugging", "procedure"}
_DOCUMENT_REF_KINDS = {
    "document",
    "episode",
    "candidate",
    "canonical",
    "derived-view",
    "transcript",
    "memo",
    "note",
}
_REPOSITORY_KINDS = {"repository", "repo"}
_REPOSITORY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_RAW_PATH_RE = re.compile(r"^30_Conversations/raw/(\d{4})/(\d{2})/(\d{2})/")
_DATED_PATH_RE = re.compile(r"(?:^|/)(\d{4})/(\d{2})/(\d{2})(?:/|$)")
_RAW_ITEM_RE = re.compile(r"(?m)^### ([^\n]+)\n\n<!-- pvg-event (\{[^\n]*\}) -->\n?")


def _normal(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return str(value)


def _document_id(document: dict[str, Any]) -> str:
    return str(document.get("document_id") or document.get("id") or "").strip()


def _metadata_values(document: dict[str, Any], key: str) -> list[Any]:
    value = document.get(key)
    if value is None and isinstance(document.get("provenance"), dict):
        value = document["provenance"].get(key)
    return _values(value)


def _ref(item: Any) -> tuple[str, str, bool]:
    if isinstance(item, dict):
        kind = _normal(item.get("kind")).replace("_", "-")
        value = item.get("document_id") or item.get("id") or item.get("locator")
        return str(value or "").strip(), kind, "document_id" in item or kind in _DOCUMENT_REF_KINDS
    return str(item or "").strip(), "", False


def _named_values(values: list[Any]) -> list[tuple[str, str]]:
    named: set[tuple[str, str]] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("text") or value.get("name") or value.get("id") or value.get("signature")
        label = str(value or "").strip()
        key = _normal(label)
        if key:
            named.add((key, label))
    return sorted(named, key=lambda item: (item[0], item[1]))


def _error_signatures(document: dict[str, Any]) -> list[Any]:
    values = _values(document.get("error_signature")) + _values(document.get("error_signatures"))
    error = document.get("error")
    if isinstance(error, dict):
        values += _values(error.get("signature"))
    applicability = document.get("applicability")
    if isinstance(applicability, dict):
        values += _values(applicability.get("error_signature"))
    return values


def _claim(document: dict[str, Any]) -> dict[str, Any]:
    claim = document.get("claim")
    if isinstance(claim, dict):
        claim = claim.get("statement") or claim.get("text")
    statement = document.get("statement") or claim or document.get("text") or document.get("title") or ""
    return {
        "document_id": _document_id(document),
        "path": str(document.get("path") or ""),
        "title": str(document.get("title") or ""),
        "statement": str(statement),
        "memory_type": str(document.get("memory_type") or ""),
        "kind": str(document.get("kind") or ""),
        "review_state": str(document.get("review_state") or ""),
        "outcome": str(document.get("outcome") or "unknown"),
        "applicability": _safe(document.get("applicability") or {}),
    }


def _repository_errors(source: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    repo_id = source.get("repo_id") or source.get("repository")
    path = source.get("path")
    commit = source.get("commit") or source.get("revision")
    if not isinstance(repo_id, str) or not _REPOSITORY_ID_RE.fullmatch(repo_id):
        errors.append("invalid_repo_id")
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
        errors.append("invalid_path")
    else:
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or path.endswith("/") or not parsed.parts or ".." in parsed.parts:
            errors.append("invalid_path")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        errors.append("invalid_commit")
    if "anchor" in source and (not isinstance(source["anchor"], str) or not source["anchor"].strip()):
        errors.append("invalid_anchor")
    return errors


def _family_id(root: str) -> str:
    return "pf_" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]


def _applicability_compatible(expected: Any, actual: Any) -> bool:
    if not isinstance(expected, dict) or not expected:
        return True
    if not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        if key == "conditions" or value in (None, "", [], {}):
            continue
        other = actual.get(key)
        if isinstance(value, dict):
            if other is None or not _applicability_compatible(value, other):
                return False
        elif other is None or _normal(other) != _normal(value):
            return False
    return True


def _finding_key(finding: dict[str, Any]) -> str:
    return json.dumps(finding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_date(
    document: dict[str, Any], fallback_dates: dict[str, str]
) -> tuple[str | None, str | None]:
    path = str(document.get("path") or "")
    match = _RAW_PATH_RE.match(path)
    candidates = [
        ("raw_path", "-".join(match.groups()) if match else ""),
        ("observed_at", str(document.get("observed_at") or "")[:10]),
        ("created_at", str(document.get("created_at") or "")[:10]),
    ]
    path_match = _DATED_PATH_RE.search(path)
    candidates.extend(
        [
            ("source_path", "-".join(path_match.groups()) if path_match else ""),
            ("git_first_add", fallback_dates.get(path, "")[:10]),
        ]
    )
    for basis, candidate in candidates:
        try:
            return date.fromisoformat(candidate).isoformat(), basis
        except ValueError:
            continue
    return None, None


def _routing_key(document: dict[str, Any]) -> tuple[str, str, str]:
    projects = _named_values(_values(document.get("projects")))
    project = projects[0][0] if projects else "_unscoped"
    for kind, values in (
        ("subject", [document.get("subject_id")]),
        ("error_signature", _error_signatures(document)),
        ("topic", _values(document.get("topics"))),
    ):
        named = _named_values(values)
        if named:
            return project, kind, named[0][0]
    if projects:
        return project, "project", project
    return project, "document", _document_id(document)


def _raw_items(document: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(document.get("text") or "")
    path = str(document.get("path") or "")
    matches = list(_RAW_ITEM_RE.finditer(text))
    if not matches:
        return [{"locator": f"{path}#document", "role": "document", "characters": len(text.strip())}]

    items: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end() : end].strip()
        metadata: dict[str, Any] = {}
        try:
            metadata = json.loads(match.group(2))
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        event_id = str(metadata.get("event_id") or f"{path}#item-{index + 1}")
        role = match.group(1).strip()
        delegation_marker = "**Agent delegation (not a user statement)**"
        result_marker = "**Result**"
        if delegation_marker in content and result_marker in content:
            request, result = content.split(result_marker, 1)
            request = request.split(delegation_marker, 1)[1].strip()
            for suffix, component_role, value in (
                ("delegation", "agent_delegation", request),
                ("result", "agent_result", result.strip()),
            ):
                items.append(
                    {
                        "locator": f"{event_id}#{suffix}",
                        "role": component_role,
                        "characters": len(value),
                    }
                )
            continue
        items.append(
            {
                "locator": event_id,
                "role": role,
                "agent_type": str(metadata.get("agent_type") or ""),
                "characters": len(content),
            }
        )
    return items


def build_compaction_plan(
    documents: list[dict[str, Any]],
    *,
    before: str,
    tracked_paths: set[str] | None = None,
    dirty_paths: set[str] | None = None,
    fallback_dates: dict[str, str] | None = None,
    semantic: dict[str, Any] | None = None,
    revision: str = "",
    max_sources: int = 8,
    max_characters: int = 60_000,
    deferrals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, read-only graph for the next raw compaction batch."""
    cutoff = date.fromisoformat(before).isoformat()
    if max_sources < 1 or max_characters < 1:
        raise ValueError("source and character limits must be positive")
    dirty_paths = dirty_paths or set()
    fallback_dates = fallback_dates or {}
    semantic = semantic or {"status": "not_requested", "edges": []}
    if dirty_paths:
        return {"status": "blocked", "reason": "dirty_markdown", "paths": sorted(dirty_paths), "selected": None}
    hashes = {str(doc.get("path")): doc.get("document_hash") for doc in documents}
    deferred: dict[str, dict[str, Any]] = {}
    invalidated: list[str] = []
    if deferrals is not None and not isinstance(deferrals, list):
        raise ValueError("deferrals must be a list")
    seen: set[str] = set()
    for entry in deferrals or []:
        if not isinstance(entry, dict):
            raise ValueError("each deferral must be an object")
        path = entry.get("path")
        dependencies = entry.get("dependencies", {})
        if (
            not isinstance(path, str) or not path.startswith(("30_Conversations/raw/", "40_Agents/"))
            or path in seen or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))
            or any(not isinstance(entry.get(key), str) or not entry[key].strip() for key in ("reason", "revisit_when"))
            or not isinstance(dependencies, dict)
            or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", str(value)) for key, value in dependencies.items())
        ):
            raise ValueError("deferral requires a unique source path, SHA-256, reason and revisit_when")
        seen.add(path)
        if hashes.get(path) != entry["sha256"] or any(hashes.get(key) != value for key, value in dependencies.items()):
            invalidated.append(path)
        else:
            deferred[path] = entry
    excluded = defaultdict(int)
    sources: list[dict[str, Any]] = []
    for document in documents:
        path = str(document.get("path") or "")
        raw = path.startswith("30_Conversations/raw/")
        legacy = path.startswith("40_Agents/")
        if not (raw or legacy):
            continue
        if tracked_paths is not None and path not in tracked_paths:
            excluded["untracked"] += 1
            continue
        source_date, date_basis = _source_date(document, fallback_dates)
        if source_date is None:
            excluded["undated_raw" if raw else "undated_legacy"] += 1
            continue
        if raw and source_date >= cutoff:
            excluded["current_or_future_raw"] += 1
            continue
        if path in deferred:
            excluded["deferred"] += 1
            continue
        sources.append(
            {
                **document,
                "source_date": source_date,
                "date_basis": date_basis,
                "items": _raw_items(document),
            }
        )

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        grouped[_routing_key(source)].append(source)

    queue: list[dict[str, Any]] = []
    source_groups: dict[str, list[dict[str, Any]]] = {}
    for routing, members in grouped.items():
        members.sort(key=lambda item: (item["source_date"] or "9999-12-31", item["path"]))
        component_id = "cmp_" + hashlib.sha256("\0".join(routing).encode("utf-8")).hexdigest()[:16]
        source_groups[component_id] = members
        oldest_source_date = members[0]["source_date"]
        queue.append(
            {
                "component_id": component_id,
                "routing": {"project": routing[0], "kind": routing[1], "key": routing[2]},
                "oldest_source_date": oldest_source_date,
                "oldest_date_characters": sum(
                    len(str(item.get("text") or ""))
                    for item in members
                    if item["source_date"] == oldest_source_date
                ),
                "source_files": len(members),
                "source_items": sum(len(item["items"]) for item in members),
                "source_characters": sum(len(str(item.get("text") or "")) for item in members),
                "source_paths": [item["path"] for item in members],
            }
        )
    queue.sort(
        key=lambda item: (
            item["oldest_source_date"] or "9999-12-31",
            -item["oldest_date_characters"],
            item["component_id"],
        )
    )
    if not queue:
        return {
            "status": "deferred" if deferred else "empty",
            "revision": revision,
            "before": cutoff,
            "eligible_sources": 0,
            "excluded": dict(sorted(excluded.items())),
            "queue": [],
            "selected": None,
            "deferred": [deferred[path] for path in sorted(deferred)],
            "invalidated_deferrals": sorted(invalidated),
        }

    selected_summary = queue[0]
    selected_sources = []
    selected_characters = 0
    for source in source_groups[selected_summary["component_id"]]:
        characters = len(str(source.get("text") or ""))
        if selected_sources and (len(selected_sources) >= max_sources or selected_characters + characters > max_characters):
            break
        # Keep the oldest oversized file whole rather than truncate it or starve it forever.
        selected_sources.append(source)
        selected_characters += characters
    selected_paths = {source["path"] for source in selected_sources}
    documents_by_id = {_document_id(document): document for document in documents}
    documents_by_path = {str(document.get("path") or ""): document for document in documents}
    selected_routing = _routing_key(selected_sources[0])
    target_documents = {
        _document_id(document): document
        for document in documents
        if document.get("memory_type") == "canonical"
        and (
            _routing_key(document) == selected_routing
            or (
                selected_routing[0] != "_unscoped"
                and str(document.get("path") or "").startswith("20_Projects/")
                and _routing_key(document)[0] == selected_routing[0]
            )
        )
    }
    relevant_semantic_edges = [
        edge
        for edge in semantic.get("edges", [])
        if edge.get("source_path") in selected_paths and edge.get("target_path") not in selected_paths
    ]
    for edge in relevant_semantic_edges:
        target = documents_by_id.get(str(edge.get("target_document_id") or "")) or documents_by_path.get(
            str(edge.get("target_path") or "")
        )
        if target:
            target_documents[_document_id(target)] = target

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    source_node_ids: dict[str, str] = {}
    for source in selected_sources:
        source_node = f"source:{_document_id(source)}"
        source_node_ids[source["path"]] = source_node
        nodes.append(
            {
                "id": source_node,
                "type": "source_file",
                "document_id": _document_id(source),
                "path": source["path"],
                "date": source["source_date"],
                "date_basis": source["date_basis"],
                "sha256": source.get("document_hash"),
            }
        )
        for item in source["items"]:
            item_id = "item:" + hashlib.sha256(
                f"{source['path']}\0{item['locator']}".encode("utf-8")
            ).hexdigest()[:16]
            nodes.append({"id": item_id, "type": "raw_item", "path": source["path"], **item})
            edges.append({"source": source_node, "target": item_id, "type": "contains"})

    anchor_node = source_node_ids[selected_sources[0]["path"]]
    for source in selected_sources[1:]:
        edges.append(
            {"source": anchor_node, "target": source_node_ids[source["path"]], "type": "same_component"}
        )

    for document_id, target in sorted(target_documents.items()):
        node_id = f"document:{document_id}"
        nodes.append(
            {
                "id": node_id,
                "type": "canonical_candidate" if target.get("memory_type") == "canonical" else "related_evidence",
                "document_id": document_id,
                "path": str(target.get("path") or ""),
                "memory_type": str(target.get("memory_type") or "unknown"),
                "sha256": target.get("document_hash"),
            }
        )
        if target.get("memory_type") == "canonical" and _routing_key(target) == selected_routing:
            edges.append({"source": anchor_node, "target": node_id, "type": "exact_route"})
        elif target.get("memory_type") == "canonical" and _routing_key(target)[0] == selected_routing[0]:
            edges.append({"source": anchor_node, "target": node_id, "type": "same_project_candidate"})

    for edge in relevant_semantic_edges:
        source_node = source_node_ids.get(str(edge.get("source_path") or ""))
        target_id = str(edge.get("target_document_id") or "")
        if not source_node or target_id not in target_documents:
            continue
        edges.append(
            {
                "source": source_node,
                "target": f"document:{target_id}",
                "type": "semantic_candidate",
                "score": edge.get("score"),
            }
        )

    nodes = sorted({node["id"]: node for node in nodes}.values(), key=lambda node: node["id"])
    edges = sorted(
        {json.dumps(edge, sort_keys=True): edge for edge in edges}.values(),
        key=lambda edge: (edge["source"], edge["target"], edge["type"]),
    )
    return {
        "status": "ok",
        "revision": revision,
        "before": cutoff,
        "eligible_sources": len(sources),
        "excluded": dict(sorted(excluded.items())),
        "queue": queue,
        "deferred": [deferred[path] for path in sorted(deferred)],
        "invalidated_deferrals": sorted(invalidated),
        "review": {
            "items": [
                {"id": node["id"], "disposition": None, "targets": [], "reason": ""}
                for node in nodes if node["type"] == "raw_item"
            ],
            "delete_paths": [],
            "user_knowledge": {"status": "pending", "reason": ""},
        },
        "selected": {
            **selected_summary,
            "source_files": len(selected_sources),
            "source_items": sum(len(source["items"]) for source in selected_sources),
            "source_characters": selected_characters,
            "character_budget": max_characters,
            "oversized_source": selected_characters > max_characters,
            "source_paths": [source["path"] for source in selected_sources],
            "remaining_source_files": max(0, selected_summary["source_files"] - len(selected_sources)),
            "graph": {"nodes": nodes, "edges": edges},
            "semantic": {key: value for key, value in semantic.items() if key != "edges"},
            "compression_gate": {
                "source_characters": selected_characters,
                "item_count": sum(len(source["items"]) for source in selected_sources),
                "classified_items": 0,
                "ready_for_retirement": False,
                "requires": "every item must be classified and no item may remain on hold",
            },
        },
    }


def _review_items(entries: Any) -> list[dict[str, Any]]:
    """Expand explicitly grouped IDs; never infer that unlisted items were reviewed."""
    if not isinstance(entries, list):
        raise ValueError("review.items must be a list")
    expanded = []
    for entry in entries:
        if not isinstance(entry, dict) or ("id" in entry) == ("ids" in entry):
            raise ValueError("each review requires either id or ids")
        ids = entry.get("ids") if "ids" in entry else [entry["id"]]
        if not isinstance(ids, list) or not ids or any(not isinstance(item_id, str) for item_id in ids):
            raise ValueError("review ids must be a nonempty list of item IDs")
        expanded.extend({**entry, "id": item_id} for item_id in ids)
    return expanded


def check_compaction(
    plan: dict[str, Any], before: list[dict[str, Any]], after: list[dict[str, Any]],
    *, integrity_after: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check batch changes against Git, while preserving live-tree integrity checks."""
    errors: list[str] = []
    original = {doc["path"]: doc for doc in before}
    current = {doc["path"]: doc for doc in after}
    selected = plan.get("selected") or {}
    paths = selected.get("source_paths") or []
    review = plan.get("review") or {}
    if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths) or not isinstance(review, dict):
        raise ValueError("plan requires selected source_paths and an annotated review")
    nodes = selected.get("graph", {}).get("nodes", [])
    frozen = {node["path"]: node.get("sha256") for node in nodes if node.get("type") == "source_file"}
    expected: dict[str, str] = {}
    for path in paths:
        source = original.get(path)
        if not source or not path.startswith(("30_Conversations/raw/", "40_Agents/")) or frozen.get(path) != source.get("document_hash"):
            errors.append(f"source does not match Git base: {path}")
            continue
        for item in _raw_items(source):
            item_id = "item:" + hashlib.sha256(f"{path}\0{item['locator']}".encode("utf-8")).hexdigest()[:16]
            if item_id in expected:
                errors.append(f"ambiguous item identity: {item_id}")
            expected[item_id] = path
    entries = _review_items(review.get("items"))
    deleted = review.get("delete_paths")
    if not isinstance(deleted, list) or any(not isinstance(path, str) for path in deleted):
        raise ValueError("review.delete_paths must be a list of paths")
    if not deleted or len(set(deleted)) != len(deleted) or not set(deleted) <= set(paths):
        errors.append("delete_paths must select distinct planned source files")
    classified: set[str] = set()
    targets: set[str] = set()
    retained: set[str] = set()
    for entry in entries:
        item_id = entry.get("id")
        disposition = entry.get("disposition")
        destinations = entry.get("targets")
        if not isinstance(item_id, str) or item_id not in expected or item_id in classified:
            errors.append(f"unknown or duplicate reviewed item: {item_id}")
            continue
        classified.add(item_id)
        if disposition not in {"merge", "replace", "already-covered", "discard", "hold"}:
            errors.append(f"unclassified item: {item_id}")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            errors.append(f"item requires a reason: {item_id}")
        if not isinstance(destinations, list) or any(not isinstance(path, str) for path in destinations):
            raise ValueError("item.targets must be a list of paths")
        if disposition in {"merge", "replace", "already-covered"} and not destinations:
            errors.append(f"durable item requires a target: {item_id}")
        if disposition == "hold":
            retained.add(expected[item_id])
            if expected[item_id] in deleted:
                errors.append(f"held source must be retained: {expected[item_id]}")
        for target in destinations:
            if (
                target not in current
                or not target.startswith(("10_User/", "20_Projects/", "50_Knowledge/", "30_Conversations/summaries/"))
                or current[target].get("metadata_valid") is False
            ):
                errors.append(f"missing or invalid knowledge target: {target}")
            targets.add(target)
    if classified != set(expected):
        errors.append("every source item must be reviewed exactly once")
    for path in paths:
        if path in deleted:
            if path in current:
                errors.append(f"planned deletion has not been applied: {path}")
        elif path not in current or current[path].get("document_hash") != original.get(path, {}).get("document_hash"):
            errors.append(f"retained source changed: {path}")
    changed = {
        path for path in original.keys() | current.keys()
        if original.get(path, {}).get("document_hash") != current.get(path, {}).get("document_hash")
    }
    unexpected = changed - set(deleted) - targets
    if unexpected:
        errors.append(f"unplanned Markdown changes: {', '.join(sorted(unexpected))}")
    user = review.get("user_knowledge") or {}
    user_changed = any(path.startswith("10_User/") for path in changed)
    if (
        not isinstance(user, dict) or user.get("status") != ("updated" if user_changed else "unchanged")
        or not isinstance(user.get("reason"), str) or not user["reason"].strip()
    ):
        errors.append("user_knowledge requires an accurate updated/unchanged status and reason")
    before_chars = sum(len(str(doc.get("text") or "")) for doc in before)
    after_chars = sum(len(str(doc.get("text") or "")) for doc in after)
    if after_chars >= before_chars:
        errors.append("active Markdown characters did not decrease")
    baseline = analyze_documents(before)
    analysis = analyze_documents(after if integrity_after is None else integrity_after)
    previous_findings = {_finding_key(finding) for finding in baseline["findings"]}
    new_errors = [finding for finding in analysis["findings"] if (
        finding["code"] in {"broken_reference", "provenance_cycle", "invalid_metadata", "duplicate_document_id", "invalid_repository_source"}
        and _finding_key(finding) not in previous_findings
    )]
    if new_errors:
        errors.append("new Wiki integrity errors")
    previous_conflicts = {conflict["conflict_id"] for conflict in baseline["conflict_queue"]}
    return {
        "status": "blocked" if errors else "ok",
        "mechanical_checks": "failed" if errors else "passed",
        "errors": sorted(set(errors)),
        "classified_items": len(classified),
        "item_count": len(expected),
        "active_characters": {"before": before_chars, "after": after_chars, "delta": after_chars - before_chars},
        "retained_sources": sorted(retained),
        "integrity": {
            "new_errors": new_errors,
            "unresolved_conflicts_before": len(baseline["conflict_queue"]),
            "unresolved_conflicts_after": len(analysis["conflict_queue"]),
            "new_conflict_ids": [
                conflict["conflict_id"] for conflict in analysis["conflict_queue"]
                if conflict["conflict_id"] not in previous_conflicts
            ],
        },
        "semantic_review": "required",
        "approval": "not_checked",
        "index_verification": "not_checked",
    }


def analyze_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Build deterministic, query-time wiki analysis without mutating documents."""
    if not isinstance(documents, list) or any(not isinstance(document, dict) for document in documents):
        raise TypeError("documents must be a list of dictionaries")

    findings: list[dict[str, Any]] = []
    indexed: dict[str, dict[str, Any]] = {}
    for position, document in enumerate(documents):
        document_id = _document_id(document)
        if not document_id:
            document_id = f"missing:{document.get('path') or position}"
            findings.append({"code": "missing_document_id", "document_id": document_id})
        if document_id in indexed:
            findings.append({"code": "duplicate_document_id", "document_id": document_id})
            continue
        indexed[document_id] = document
        if document.get("metadata_valid") is False:
            findings.append({"code": "invalid_metadata", "document_id": document_id})

    document_ids = sorted(indexed)
    edges: dict[str, set[str]] = {document_id: set() for document_id in document_ids}
    external_roots: dict[str, set[str]] = {document_id: set() for document_id in document_ids}
    provenance_broken: set[str] = set()
    broken_references: list[dict[str, Any]] = []

    def broken(document_id: str, field: str, reference: str) -> None:
        finding = {
            "code": "broken_reference",
            "document_id": document_id,
            "field": field,
            "reference": reference,
        }
        broken_references.append(finding)
        findings.append(finding)

    for document_id in document_ids:
        document = indexed[document_id]
        for item in sorted(_metadata_values(document, "derived_from"), key=lambda value: _finding_key({"v": _safe(value)})):
            reference, _kind, explicit_document = _ref(item)
            if reference in indexed:
                edges[document_id].add(reference)
            elif reference:
                external_roots[document_id].add("source:" + reference)
                broken(document_id, "derived_from", reference)
                if explicit_document:
                    provenance_broken.add(document_id)

        for item in sorted(_metadata_values(document, "evidence_refs"), key=lambda value: _finding_key({"v": _safe(value)})):
            reference, kind, explicit_document = _ref(item)
            if reference in indexed:
                edges[document_id].add(reference)
            elif explicit_document:
                broken(document_id, "evidence_refs", reference)
                provenance_broken.add(document_id)
            elif reference and kind not in _REPOSITORY_KINDS:
                external_roots[document_id].add("evidence:" + reference)

        for item in sorted(_metadata_values(document, "method_refs"), key=lambda value: _finding_key({"v": _safe(value)})):
            reference, _kind, explicit_document = _ref(item)
            if reference and reference not in indexed and (explicit_document or reference.startswith(("doc_", "ep_", "cand_", "kn_", "conv_"))):
                broken(document_id, "method_refs", reference)

        relations = document.get("relations") if isinstance(document.get("relations"), dict) else {}
        for relation, values in sorted(relations.items()):
            for item in _values(values):
                reference, _kind, _explicit_document = _ref(item)
                if reference and reference not in indexed:
                    broken(document_id, f"relations.{relation}", reference)

    roots_cache: dict[str, set[str]] = {}
    active: list[str] = []
    cycles: set[tuple[str, ...]] = set()

    def roots(document_id: str) -> set[str]:
        if document_id in roots_cache:
            return roots_cache[document_id]
        if document_id in active:
            cycle = tuple(sorted(set(active[active.index(document_id) :])))
            cycles.add(cycle)
            return {"cycle:" + ",".join(cycle)}
        active.append(document_id)
        found = set(external_roots[document_id])
        for parent_id in sorted(edges[document_id]):
            found.update(roots(parent_id))
        active.pop()
        if not found:
            found.add("document:" + document_id)
        roots_cache[document_id] = found
        return found

    for document_id in document_ids:
        roots(document_id)

    provenance_cycles = [
        {"code": "provenance_cycle", "document_ids": list(cycle)} for cycle in sorted(cycles)
    ]
    findings.extend(provenance_cycles)
    cycle_documents = {document_id for cycle in cycles for document_id in cycle}

    family_documents: dict[str, set[str]] = defaultdict(set)
    family_roots: dict[str, str] = {}
    document_families: dict[str, list[str]] = {}
    for document_id in document_ids:
        family_ids = []
        for root in sorted(roots_cache[document_id]):
            family_id = _family_id(root)
            family_ids.append(family_id)
            family_roots[family_id] = root
            family_documents[family_id].add(document_id)
        document_families[document_id] = sorted(family_ids)

    def lineage(seed_ids: list[str] | set[str]) -> set[str]:
        found = {document_id for document_id in seed_ids if document_id in indexed}
        pending = list(found)
        while pending:
            for parent_id in edges[pending.pop()]:
                if parent_id not in found:
                    found.add(parent_id)
                    pending.append(parent_id)
        return found

    def support(seed_ids: list[str] | set[str], applicability: dict[str, Any] | None = None) -> dict[str, Any]:
        supporting = lineage(seed_ids)
        direct = sorted(
            document_id
            for document_id in supporting
            if str(indexed[document_id].get("provenance_mode") or (indexed[document_id].get("provenance") or {}).get("mode") or "")
            == "direct_observation"
        )
        valid_direct = [
            document_id
            for document_id in direct
            if document_id not in provenance_broken and document_id not in cycle_documents
            and bool(external_roots[document_id] or edges[document_id])
            and _applicability_compatible(applicability, indexed[document_id].get("applicability"))
        ]
        successful = [document_id for document_id in valid_direct if indexed[document_id].get("outcome") == "success"]
        families = sorted({family_id for document_id in valid_direct for family_id in document_families[document_id]})
        family_sessions: dict[str, set[str]] = defaultdict(set)
        for document_id in successful:
            session_id = str(indexed[document_id].get("session_id") or "").strip()
            if not session_id:
                continue
            for family_id in document_families[document_id]:
                family_sessions[family_id].add(session_id)
        successful_families = sorted(family_sessions)
        derived = sorted(
            document_id
            for document_id in supporting
            if str(indexed[document_id].get("provenance_mode") or (indexed[document_id].get("provenance") or {}).get("mode") or "")
            == "derived"
            or bool(_metadata_values(indexed[document_id], "derived_from"))
        )
        outcomes = {
            "successes": sum(indexed[document_id].get("outcome") == "success" for document_id in valid_direct),
            "failures": sum(indexed[document_id].get("outcome") == "failure" for document_id in valid_direct),
            "mixed": sum(indexed[document_id].get("outcome") == "mixed" for document_id in valid_direct),
            "unknown": sum(indexed[document_id].get("outcome") in {None, "", "unknown"} for document_id in valid_direct),
        }
        return {
            "supporting_documents": len(supporting),
            "direct_observations": len(valid_direct),
            "successful_direct_observations": len(successful),
            "independent_provenance_families": len(families),
            "independent_reproductions": min(
                len(successful_families),
                len({session for sessions in family_sessions.values() for session in sessions}),
            ),
            "derived_echoes": len(derived),
            "outcomes": outcomes,
            "document_ids": sorted(supporting),
            "direct_document_ids": valid_direct,
            "family_ids": families,
            "successful_family_ids": successful_families,
            "reproduction_sessions": sorted({session for sessions in family_sessions.values() for session in sessions}),
        }

    cluster_members: dict[str, dict[str, set[str]]] = {
        "subjects": defaultdict(set),
        "topics": defaultdict(set),
        "error_signatures": defaultdict(set),
    }
    cluster_labels: dict[str, dict[str, set[str]]] = {
        name: defaultdict(set) for name in cluster_members
    }
    document_keys: dict[str, dict[str, list[str]]] = {}
    for document_id in document_ids:
        document = indexed[document_id]
        subjects = _named_values([document.get("subject_id") or document.get("target_subject_id")])
        topics = _named_values(_values(document.get("topics")))
        signatures = _named_values(_error_signatures(document))
        values_by_cluster = {"subjects": subjects, "topics": topics, "error_signatures": signatures}
        document_keys[document_id] = {}
        for cluster_name, values in values_by_cluster.items():
            document_keys[document_id][cluster_name] = [key for key, _label in values]
            for key, label in values:
                cluster_members[cluster_name][key].add(document_id)
                cluster_labels[cluster_name][key].add(label)

    clusters: dict[str, list[dict[str, Any]]] = {}
    for cluster_name in ("subjects", "topics", "error_signatures"):
        clusters[cluster_name] = [
            {
                "key": key,
                "labels": sorted(cluster_labels[cluster_name][key], key=lambda label: (_normal(label), label)),
                "document_ids": sorted(cluster_members[cluster_name][key]),
                "support": support(cluster_members[cluster_name][key]),
            }
            for key in sorted(cluster_members[cluster_name])
        ]

    conflict_pairs: set[tuple[str, str]] = set()
    for document_id in document_ids:
        document = indexed[document_id]
        relations = document.get("relations") if isinstance(document.get("relations"), dict) else {}
        contradicts = _values(relations.get("contradicts")) + _values(document.get("contradicts"))
        for item in contradicts:
            other_id, _kind, _explicit_document = _ref(item)
            if not other_id or other_id not in indexed or other_id == document_id:
                continue
            if document.get("conflict_state") == "resolved":
                continue
            conflict_pairs.add(tuple(sorted((document_id, other_id))))

    curation_events: dict[str, dict[str, Any]] = {}
    for document_id in document_ids:
        document = indexed[document_id]
        if document.get("kind") != "conflict-resolution":
            continue
        conflict_id = str(document.get("conflict_id") or "")
        state = str(document.get("resolution_state") or "")
        if not conflict_id or state not in {"resolved", "unresolved"}:
            findings.append({"code": "invalid_conflict_curation", "document_id": document_id})
            continue
        current = curation_events.get(conflict_id)
        order = (str(document.get("created_at") or ""), str(document.get("path") or ""))
        if current is None or order > current["order"]:
            curation_events[conflict_id] = {
                "order": order,
                "document_id": document_id,
                "state": state,
                "curator": str(document.get("curator") or ""),
                "note": str(document.get("curation_note") or ""),
            }

    conflict_queue: list[dict[str, Any]] = []
    resolved_conflicts: list[dict[str, Any]] = []
    for left_id, right_id in sorted(conflict_pairs):
        conflict_id = "conf_" + hashlib.sha256(f"{left_id}\0{right_id}".encode()).hexdigest()[:16]
        curation = curation_events.get(conflict_id)
        claims = []
        for document_id in (left_id, right_id):
            claim = _claim(indexed[document_id])
            claim["support"] = support([document_id])
            claims.append(claim)
        conflict = {
            "conflict_id": conflict_id,
            "conflict_state": curation["state"] if curation else "unresolved",
            "document_ids": [left_id, right_id],
            "canonical_baseline": next(
                (claim["document_id"] for claim in claims if claim["memory_type"] == "canonical"), None
            ),
            "claims": claims,
            "winner": None,
            "curation": _safe(curation) if curation else None,
        }
        if conflict["conflict_state"] == "resolved":
            resolved_conflicts.append(conflict)
        else:
            conflict_queue.append(conflict)
            findings.append({"code": "unresolved_conflict", "document_ids": [left_id, right_id]})

    invalid_repository_ids: set[str] = set()
    repository_validation: dict[str, str] = {}
    for document_id in document_ids:
        sources = [item for item in _values(indexed[document_id].get("repository_sources")) if isinstance(item, dict)]
        errors = [error for source in sources for error in _repository_errors(source)]
        if errors:
            invalid_repository_ids.add(document_id)
            repository_validation[document_id] = "invalid"
        elif sources:
            repository_validation[document_id] = "valid"
        else:
            repository_validation[document_id] = "unverified"

    retrieval_states: dict[str, str] = {}
    candidate_support: dict[str, dict[str, Any]] = {}
    conflicted_ids = {
        document_id for conflict in conflict_queue for document_id in conflict["document_ids"]
    }
    for document_id in document_ids:
        document = indexed[document_id]
        if document.get("memory_type") != "candidate":
            continue
        related = lineage([document_id])
        for cluster_name, keys in document_keys[document_id].items():
            for key in keys:
                related.update(cluster_members[cluster_name][key])
        counts = support(related, document.get("applicability") if isinstance(document.get("applicability"), dict) else {})
        candidate_support[document_id] = counts
        if (
            _normal(document.get("kind")) in _CORROBORATABLE_KINDS
            and counts["successful_direct_observations"] >= 2
            and counts["independent_reproductions"] >= 2
            and document.get("conflict_state") != "unresolved"
            and document_id not in conflicted_ids
            and repository_validation[document_id] == "valid"
            and document.get("review_state") not in {"human_rejected", "merged"}
        ):
            retrieval_states[document_id] = "machine_corroborated"

    stale_summaries: list[dict[str, Any]] = []
    summary_states: dict[str, str] = {}
    for document_id in document_ids:
        document = indexed[document_id]
        if document.get("memory_type") != "derived_view" or document.get("kind") in {
            "conflict-resolution",
            "merge-receipt",
        }:
            continue
        raw_refs = _values(document.get("source_refs"))
        raw_hashes = document.get("source_hashes")
        hash_by_id: dict[str, str] = {}
        positional_hashes: list[Any] = []
        if isinstance(raw_hashes, dict):
            hash_by_id = {str(key): str(value) for key, value in raw_hashes.items()}
        else:
            positional_hashes = _values(raw_hashes)
        reasons: list[dict[str, Any]] = []
        document_source_count = 0
        for index, item in enumerate(raw_refs):
            reference, kind, _explicit_document = _ref(item)
            if kind in _REPOSITORY_KINDS:
                continue
            document_source_count += 1
            inline_hash = item.get("document_hash") or item.get("hash") if isinstance(item, dict) else None
            expected_hash = hash_by_id.get(reference)
            if expected_hash is None and index < len(positional_hashes):
                hash_item = positional_hashes[index]
                if isinstance(hash_item, dict):
                    expected_hash = str(hash_item.get("hash") or hash_item.get("document_hash") or "")
                else:
                    expected_hash = str(hash_item or "")
            expected_hash = str(inline_hash or expected_hash or "")
            if reference not in indexed:
                reasons.append({"reason": "missing_source", "source_ref": reference})
                broken(document_id, "source_refs", reference)
                continue
            actual_hash = str(indexed[reference].get("document_hash") or "")
            if not expected_hash:
                reasons.append({"reason": "missing_source_hash", "source_ref": reference})
            elif not actual_hash:
                reasons.append({"reason": "source_hash_unavailable", "source_ref": reference})
            elif expected_hash.removeprefix("sha256:").casefold() != actual_hash.removeprefix("sha256:").casefold():
                reasons.append(
                    {
                        "reason": "source_hash_mismatch",
                        "source_ref": reference,
                        "expected_hash": expected_hash,
                        "actual_hash": actual_hash,
                    }
                )
        if not raw_refs:
            reasons.append({"reason": "missing_source_refs"})
        if positional_hashes and len(positional_hashes) != len(raw_refs):
            reasons.append(
                {"reason": "source_hash_count_mismatch", "source_refs": len(raw_refs), "source_hashes": len(positional_hashes)}
            )
        if raw_refs and not document_source_count:
            reasons.append({"reason": "missing_document_source_refs"})
        reasons = sorted({_finding_key(reason): reason for reason in reasons}.values(), key=_finding_key)
        invalid_summary = any(
            reason["reason"] in {
                "missing_source_refs",
                "missing_document_source_refs",
                "missing_source",
                "missing_source_hash",
                "source_hash_unavailable",
                "source_hash_count_mismatch",
            }
            for reason in reasons
        )
        summary_states[document_id] = "invalid" if invalid_summary else "stale" if reasons else "fresh"
        if reasons:
            stale_summaries.append(
                {"document_id": document_id, "path": str(document.get("path") or ""), "reasons": reasons}
            )
            findings.append({"code": "stale_summary", "document_id": document_id})

    alias_subjects: dict[str, set[str]] = defaultdict(set)
    alias_documents: dict[str, set[str]] = defaultdict(set)
    for document_id in document_ids:
        subject_keys = document_keys[document_id]["subjects"]
        aliases = _named_values(
            _values(indexed[document_id].get("subject_aliases"))
            + _values(indexed[document_id].get("aliases"))
        )
        if aliases and not subject_keys:
            findings.append({"code": "aliases_without_subject", "document_id": document_id})
        for subject_key in subject_keys:
            alias_subjects[subject_key].add(subject_key)
            alias_documents[subject_key].add(document_id)
            for alias_key, _label in aliases:
                alias_subjects[alias_key].add(subject_key)
                alias_documents[alias_key].add(document_id)
    alias_collisions = [
        {
            "alias": alias,
            "subject_ids": sorted(subjects),
            "document_ids": sorted(alias_documents[alias]),
        }
        for alias, subjects in sorted(alias_subjects.items())
        if len(subjects) > 1
    ]
    findings.extend(
        {"code": "alias_collision", "alias": item["alias"], "subject_ids": item["subject_ids"]}
        for item in alias_collisions
    )

    invalid_repository_sources: list[dict[str, Any]] = []
    for document_id in document_ids:
        document = indexed[document_id]
        sources = [("source_refs", item) for item in _values(document.get("source_refs"))]
        sources += [("evidence_refs", item) for item in _metadata_values(document, "evidence_refs")]
        sources += [("repository_sources", item) for item in _values(document.get("repository_sources"))]
        for field, item in sources:
            if not isinstance(item, dict) or _normal(item.get("kind")) not in _REPOSITORY_KINDS:
                continue
            errors = _repository_errors(item)
            if errors:
                invalid = {
                    "document_id": document_id,
                    "field": field,
                    "source": _safe(item),
                    "errors": errors,
                }
                invalid_repository_sources.append(invalid)
                findings.append({"code": "invalid_repository_source", **invalid})
    invalid_repository_sources.sort(key=_finding_key)

    for document_id in document_ids:
        document = indexed[document_id]
        if not document.get("memory_type"):
            findings.append({"code": "missing_memory_type", "document_id": document_id})
        if document.get("memory_type") == "episode" and document.get("outcome") in {None, "", "unknown"}:
            findings.append({"code": "episode_missing_outcome", "document_id": document_id})
        if document.get("provenance_defaulted"):
            findings.append({"code": "provenance_defaulted", "document_id": document_id})
        if document.get("memory_type") == "episode" and document.get("provenance_mode") == "direct_observation":
            if not str(document.get("session_id") or "").strip():
                findings.append({"code": "direct_observation_missing_session", "document_id": document_id})
            if not document.get("applicability"):
                findings.append({"code": "episode_missing_applicability", "document_id": document_id})

    for cluster_name in ("topics", "error_signatures"):
        for cluster in clusters[cluster_name]:
            if len(cluster["document_ids"]) < 2:
                continue
            if not any(indexed[document_id].get("memory_type") == "candidate" for document_id in cluster["document_ids"]):
                findings.append(
                    {
                        "code": "recurring_cluster_without_candidate",
                        "cluster_type": cluster_name,
                        "key": cluster["key"],
                        "document_ids": cluster["document_ids"],
                    }
                )

    findings = sorted({_finding_key(finding): finding for finding in findings}.values(), key=_finding_key)
    document_analysis = {
        document_id: {
            "path": str(indexed[document_id].get("path") or ""),
            "memory_type": str(indexed[document_id].get("memory_type") or ""),
            "kind": str(indexed[document_id].get("kind") or ""),
            "subject_keys": document_keys[document_id]["subjects"],
            "topic_keys": document_keys[document_id]["topics"],
            "error_signature_keys": document_keys[document_id]["error_signatures"],
            "provenance_family_ids": document_families[document_id],
            "retrieval_state": retrieval_states.get(document_id),
            "summary_state": summary_states.get(document_id),
        }
        for document_id in document_ids
    }

    by_id: dict[str, dict[str, Any]] = {}
    for document_id in document_ids:
        document = dict(indexed[document_id])
        cluster_ids = [
            f"{cluster_name}:{key}"
            for cluster_name, keys in document_keys[document_id].items()
            for key in keys
        ]
        document.update(
            {
                "provenance_family_ids": document_families[document_id],
                "support": support([document_id]),
                "cluster_ids": sorted(cluster_ids),
                "retrieval_state": retrieval_states.get(document_id),
                "summary_state": summary_states.get(document_id),
                "repository_validation": repository_validation[document_id],
            }
        )
        by_id[document_id] = document

    summary = {
        "documents": len(indexed),
        "provenance_families": len(family_documents),
        "clusters": sum(len(items) for items in clusters.values()),
        "unresolved_conflicts": len(conflict_queue),
        "machine_corroborated": len(retrieval_states),
        "stale_summaries": len(stale_summaries),
        "findings": len(findings),
    }

    return {
        "document_count": len(documents),
        "indexed_document_count": len(indexed),
        "documents": document_analysis,
        "by_id": by_id,
        "summary": summary,
        "provenance_families": [
            {
                "family_id": family_id,
                "root_ref": family_roots[family_id],
                "document_ids": sorted(family_documents[family_id]),
            }
            for family_id in sorted(family_documents)
        ],
        "clusters": clusters,
        "support_by_document": {document_id: support([document_id]) for document_id in document_ids},
        "retrieval_states": retrieval_states,
        "machine_corroborated": sorted(retrieval_states),
        "candidate_support": candidate_support,
        "conflict_queue": conflict_queue,
        "resolved_conflicts": resolved_conflicts,
        "stale_summaries": stale_summaries,
        "alias_collisions": alias_collisions,
        "invalid_repository_sources": invalid_repository_sources,
        "broken_references": sorted({_finding_key(item): item for item in broken_references}.values(), key=_finding_key),
        "provenance_cycles": provenance_cycles,
        "findings": findings,
    }


def _cluster_support(analysis: dict[str, Any], cluster_type: str, key: str, document_ids: list[str]) -> dict[str, Any]:
    cluster_name = {"subject": "subjects", "topic": "topics", "error_signature": "error_signatures"}.get(cluster_type)
    for cluster in analysis.get("clusters", {}).get(cluster_name, []) if cluster_name else []:
        if cluster.get("key") == key:
            return _safe(cluster.get("support") or {})
    support_by_document = analysis.get("support_by_document", {})
    return _safe(support_by_document.get(document_ids[0], {})) if document_ids else {}


def bundle_groups(bundle: str, results: list[dict[str, Any]], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Group search results by bundle purpose while preserving caller-owned results."""
    if bundle not in _BUNDLES:
        raise ValueError(f"bundle must be one of: {', '.join(sorted(_BUNDLES))}")
    if bundle == "conflicts":
        result_ids = {_document_id(result) for result in results}
        conflicts = [
            _safe(conflict)
            for conflict in analysis.get("conflict_queue", [])
            if result_ids.intersection(conflict.get("document_ids", []))
        ]
        if conflicts:
            for conflict in conflicts:
                conflict["conflict_state"] = "unresolved"
                conflict["winner"] = None
            return conflicts
        return [
            {
                "conflict_id": None,
                "conflict_state": "unresolved",
                "document_ids": sorted(result_ids),
                "claims": [_safe(result) for result in results],
                "winner": None,
                "warnings": ["One or more contradiction targets are missing."],
            }
        ] if results else []

    metadata = analysis.get("documents", {})
    retrieval_states = analysis.get("retrieval_states", {})
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for result in results:
        document_id = _document_id(result)
        info = metadata.get(document_id, {})
        if info.get("subject_keys"):
            cluster_type, key = "subject", info["subject_keys"][0]
        elif bundle in {"evidence", "experiences"} and info.get("error_signature_keys"):
            cluster_type, key = "error_signature", info["error_signature_keys"][0]
        elif info.get("topic_keys"):
            cluster_type, key = "topic", info["topic_keys"][0]
        elif info.get("error_signature_keys"):
            cluster_type, key = "error_signature", info["error_signature_keys"][0]
        else:
            cluster_type, key = "document", document_id
        item = _safe(result)
        if document_id in retrieval_states:
            item["retrieval_state"] = retrieval_states[document_id]
        grouped.setdefault((cluster_type, key), []).append(item)

    output: list[dict[str, Any]] = []
    for (cluster_type, key), items in grouped.items():
        document_ids = [_document_id(item) for item in items]
        support = _cluster_support(analysis, cluster_type, key, document_ids)
        conflict_state = "unresolved" if any(item.get("conflict_state") == "unresolved" for item in items) else "none"
        base: dict[str, Any] = {
            "key": key,
            "cluster_type": cluster_type,
            "conflict_state": conflict_state,
            "support": support,
        }
        if bundle == "current":
            canonicals = [item for item in items if item.get("memory_type") == "canonical"]
            base.update(
                {
                    "canonical": canonicals[0] if canonicals else None,
                    "pending_deltas": [item for item in items if item.get("memory_type") == "candidate"],
                    "warnings": (
                        [{"type": "unresolved_conflict", "document_ids": document_ids}]
                        if conflict_state == "unresolved"
                        else []
                    ),
                    "items": items,
                }
            )
        elif bundle in {"evidence", "experiences"}:
            base.update(
                {
                    "observations": support,
                    "outcomes": {
                        outcome: [item for item in items if item.get("outcome") == outcome]
                        for outcome in ("success", "failure", "mixed", "unknown", "not_applicable")
                    },
                    "applicability": [
                        {"document_id": _document_id(item), "value": _safe(item.get("applicability") or {})}
                        for item in items
                    ],
                    "representative_episodes": [item for item in items if item.get("memory_type") == "episode"],
                    "items": items,
                }
            )
        elif bundle == "history":
            base["timeline"] = sorted(
                items,
                key=lambda item: (
                    str(item.get("effective_from") or item.get("observed_at") or ""),
                    _document_id(item),
                ),
            )
        else:
            base["items"] = items
        output.append(base)
    return output


def health_report(analysis: dict[str, Any]) -> dict[str, Any]:
    """Return a compact health projection from analyze_documents output."""
    findings = _safe(analysis.get("findings", []))
    finding_counts: dict[str, int] = {}
    for finding in findings:
        code = str(finding.get("code") or "unknown")
        finding_counts[code] = finding_counts.get(code, 0) + 1
    return {
        "status": "attention" if findings else "ok",
        "document_count": int(analysis.get("document_count") or 0),
        "finding_count": len(findings),
        "finding_counts": {key: finding_counts[key] for key in sorted(finding_counts)},
        "unresolved_conflicts": len(analysis.get("conflict_queue", [])),
        "machine_corroborated_candidates": len(analysis.get("machine_corroborated", [])),
        "stale_summaries": _safe(analysis.get("stale_summaries", [])),
        "alias_collisions": _safe(analysis.get("alias_collisions", [])),
        "invalid_repository_sources": _safe(analysis.get("invalid_repository_sources", [])),
        "broken_references": _safe(analysis.get("broken_references", [])),
        "provenance_cycles": _safe(analysis.get("provenance_cycles", [])),
        "findings": findings,
    }


def _self_check() -> None:
    documents = [
        {"document_id": "run-a", "memory_type": "episode", "kind": "procedure", "outcome": "success", "session_id": "session-a", "provenance_mode": "direct_observation", "evidence_refs": ["exec-a"], "topics": ["Retry"], "applicability": {"repository": "demo"}},
        {"document_id": "run-b", "memory_type": "episode", "kind": "procedure", "outcome": "success", "session_id": "session-b", "provenance_mode": "direct_observation", "evidence_refs": ["exec-b"], "method_refs": ["run-a"], "topics": ["retry"], "applicability": {"repository": "demo"}},
        {"document_id": "candidate", "memory_type": "candidate", "kind": "procedure", "derived_from": ["run-a", "run-b"], "topics": [" retry "], "applicability": {"repository": "demo"}, "repository_sources": [{"repo_id": "demo", "commit": "abcdef1", "path": "README.md"}]},
    ]
    before = json.dumps(documents, sort_keys=True)
    analysis = analyze_documents(documents)
    assert analysis["machine_corroborated"] == ["candidate"]
    assert analysis["clusters"]["topics"][0]["support"]["independent_reproductions"] == 2
    assert json.dumps(documents, sort_keys=True) == before
    json.dumps(analysis, ensure_ascii=False, sort_keys=True, allow_nan=False)


if __name__ == "__main__":
    _self_check()
