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
from urllib.parse import unquote


__all__ = [
    "analyze_documents", "build_compaction_plan", "check_compaction", "bundle_groups", "health_report",
    "ledger_entries", "brief_sections", "BRIEF_CAP",
]

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
# Claim provenance. item: (raw item id) and sess: (hash of a session id) are written by compact-annotate only and
# are one-way hashes: no path, locator or quote leaks. doc:<path>[#<heading>] is typed by hand for migration.
_SRC_RE = re.compile(r"<!-- pvg-src:((?: (?:item:[0-9a-f]{16}|sess:[0-9a-f]{8}|doc:[^\s>]+))+) -->")
BRIEF_CAP = 8000
PROJECT_DISPOSITIONS = {"decision", "deferral", "supersession", "goal_change", "question"}
USER_DISPOSITIONS = {"user_preference", "user_constraint", "user_feedback", "user_context", "user_retraction"}
CLAIM_DISPOSITIONS = PROJECT_DISPOSITIONS | USER_DISPOSITIONS | {"knowledge"}
LEGACY_DISPOSITIONS = {"merge", "replace"}  # deprecated in protocol 23, removed in 24
MARKED_DISPOSITIONS = CLAIM_DISPOSITIONS | LEGACY_DISPOSITIONS  # these owe a pvg-src marker in each target
SUBAGENT_DISPOSITIONS = {"knowledge", "already-covered", "discard", "hold"}
DISPOSITIONS = MARKED_DISPOSITIONS | {"already-covered", "discard", "hold"}
# Curator documents are detected by frontmatter kind, never by file name. Ledger kinds list their table header and
# vocabularies; brief kinds list their fixed headings and the section that may only cite ledger rows of one status.
CURATOR_KINDS: dict[str, dict[str, Any]] = {
    "decision_ledger": {
        "dir": "20_Projects/", "file": "DECISIONS.md", "template": "docs/templates/DECISIONS.md", "prefix": "D",
        "header": ("id", "날짜", "유형", "상태", "내용", "이유·근거", "후속"),
        "types": {"결정", "미룸", "대체", "목표변경", "질문"}, "statuses": {"유효", "대체됨", "보류", "종결"},
    },
    "user_ledger": {
        "dir": "10_User/", "file": "OBSERVATIONS.md", "template": "docs/templates/OBSERVATIONS.md", "prefix": "U",
        "header": ("id", "날짜", "유형", "상태", "내용", "근거", "독립 세션"),
        "types": {"선호(명시)", "선호(추론)", "제약", "피드백", "맥락", "철회"}, "statuses": {"확인", "가설", "철회됨"},
    },
    "brief": {
        "dir": "20_Projects/", "file": "BRIEF.md", "template": "docs/templates/BRIEF.md", "label": "brief",
        "headings": ("현재 목표", "유효한 결정", "미뤄진 것", "대체된 것", "열린 질문", "최근 변화"),
        "ledger": "decision_ledger", "section": "유효한 결정", "status": "유효", "soft": ("현재 목표", "열린 질문"),
    },
    "user_profile": {
        "dir": "10_User/", "file": "PROFILE.md", "template": "docs/templates/PROFILE.md", "label": "profile",
        "headings": ("역할·맥락", "확인된 선호", "가설", "제약", "에이전트에 준 피드백", "반례·철회", "최근 변화"),
        "ledger": "user_ledger", "section": "확인된 선호", "status": "확인",
    },
}


def _normal(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def _src_tokens(text: str, prefix: str) -> set[str]:
    return {token for match in _SRC_RE.finditer(text) for token in match.group(1).split() if token.startswith(prefix)}


def _src_ids(text: str) -> set[str]:
    return _src_tokens(text, "item:")


def _src_docs(text: str) -> set[str]:
    return _src_tokens(text, "doc:")


def _sess(session_id: Any) -> str:
    value = str(session_id or "")
    return "sess:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] if value else ""


def _doc_sess(document: dict[str, Any]) -> str:
    """Session hash of a source file. An agent memo's session_id is a fresh note id, so it names no session."""
    return "" if document.get("capture_kind") == "agent_note" else _sess(document.get("session_id"))


def _cells(line: str) -> list[str] | None:
    """Cells of a Markdown table row with pvg-src markers removed, or None for any other line."""
    line = _SRC_RE.sub("", line).strip()
    if not (line.startswith("|") and line.endswith("|")):
        return None
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]


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


def _raw_items(document: dict[str, Any], *, body: bool = False) -> list[dict[str, Any]]:
    """Items of one raw file. `body=True` adds each item's own text; only check_compaction uses it, never the plan."""
    text = str(document.get("text") or "")
    path = str(document.get("path") or "")
    matches = list(_RAW_ITEM_RE.finditer(text))

    def item(locator: str, role: str, value: str, **extra: str) -> dict[str, Any]:
        return {"locator": locator, "role": role, **extra, "characters": len(value), **({"body": value} if body else {})}

    if not matches:
        return [item(f"{path}#document", "document", text.strip())]

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
        event_role = str(metadata.get("role") or role.split(":", 1)[0]).strip()
        delegation_marker = "**Agent delegation (not a user statement)**"
        result_marker = "**Result**"
        if delegation_marker in content and result_marker in content:
            request, result = content.split(result_marker, 1)
            request = request.split(delegation_marker, 1)[1].strip()
            for suffix, component_role, value in (
                ("delegation", "agent_delegation", request),
                ("result", "agent_result", result.strip()),
            ):
                items.append(item(f"{event_id}#{suffix}", component_role, value, event_role=event_role))
            continue
        items.append(item(event_id, role, content, agent_type=str(metadata.get("agent_type") or ""), event_role=event_role))
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
    brief_cap: int = BRIEF_CAP,
) -> dict[str, Any]:
    """Build a deterministic, read-only graph for the next raw compaction batch."""
    cutoff = date.fromisoformat(before).isoformat()
    if max_sources < 1 or max_characters < 1 or brief_cap < 1:
        raise ValueError("source, character and brief limits must be positive")
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

    # Curator documents are offered in every plan: a missing one is a create node, an existing one of another kind is
    # an adopt candidate. Under 10_User only PROFILE.md and OBSERVATIONS.md are ever added, never other files.
    curator = [("user_profile", "10_User"), ("user_ledger", "10_User")]
    if selected_routing[0] != "_unscoped":
        project_docs = [
            doc for doc in target_documents.values()
            if str(doc.get("path") or "").startswith("20_Projects/") and len(PurePosixPath(str(doc["path"])).parts) > 2
            and _routing_key(doc)[0] == selected_routing[0]
        ]
        top = lambda doc: "/".join(PurePosixPath(str(doc["path"])).parts[:2])
        label = _named_values(_values(selected_sources[0].get("projects")))[0][1].replace("/", "-")
        holding = sorted({top(doc) for doc in project_docs if doc.get("kind") in {"brief", "decision_ledger"}})
        project = (holding or sorted({top(doc) for doc in project_docs}) or [f"20_Projects/{label}"])[0]
        curator = [("brief", project), ("decision_ledger", project), *curator]
    curator_paths: list[tuple[str, str]] = []
    for kind, folder in curator:
        found = sorted(
            path for path, doc in documents_by_path.items()
            if doc.get("kind") == kind and doc.get("memory_type") == "canonical" and PurePosixPath(path).parent.as_posix() == folder
        )
        path = found[0] if found else f"{folder}/{CURATOR_KINDS[kind]['file']}"
        curator_paths.append((kind, path))
        if path in documents_by_path:
            target_documents[_document_id(documents_by_path[path])] = documents_by_path[path]

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    source_node_ids: dict[str, str] = {}
    for source in selected_sources:
        source_node = f"source:{_document_id(source)}"
        source_node_ids[source["path"]] = source_node
        session = _doc_sess(source)
        nodes.append(
            {
                "id": source_node,
                "type": "source_file",
                "document_id": _document_id(source),
                "path": source["path"],
                "date": source["source_date"],
                "date_basis": source["date_basis"],
                "sha256": source.get("document_hash"),
                **({"session": session} if session else {}),
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

    ledgers = []
    for kind, path in curator_paths:
        spec, document = CURATOR_KINDS[kind], documents_by_path.get(path)
        create, adopt = document is None, document is not None and document.get("kind") != kind
        node_id = f"create:{path}" if create else f"document:{_document_id(document)}"
        if create:
            nodes.append(
                {"id": node_id, "type": "canonical_candidate", "path": path, "create": True, "kind": kind, "template": spec["template"]}
            )
        edges.append({"source": anchor_node, "target": node_id, "type": "curator_target"})
        text = str(document.get("text") or "") if document else ""
        if "header" in spec:
            rows = ledger_entries(text, kind)[0]
            size = {"rows": len(rows), "next_id": f"{spec['prefix']}-{max((int(row['id'][2:]) for row in rows), default=0) + 1:03d}"}
        else:
            size = {"chars": _brief_chars(text), "cap": brief_cap}
        ledgers.append({
            "path": path, "kind": kind, "create": create, "adopt": adopt,
            **({"template": spec["template"]} if create or adopt else {}), **size,
        })

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
            "ledgers": ledgers,
            "subagent_items": sum(node.get("event_role") == "subagent" for node in nodes if node["type"] == "raw_item"),
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


def ledger_entries(text: str, kind: str = "decision_ledger", path: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """Parse the one ledger table of a curator document into rows plus path-qualified problems."""
    spec, problems = CURATOR_KINDS[kind], []
    header, lines = list(spec["header"]), text.split("\n")
    starts = [index for index, line in enumerate(lines) if _cells(line) == header]
    if len(starts) != 1:
        return [], [f"ledger table missing or header differs: {path}"]
    index = starts[0] + 1
    separator = _cells(lines[index]) if index < len(lines) else None
    if not separator or not all(re.fullmatch(r":?-+:?", cell) for cell in separator):
        return [], [f"ledger separator row missing: {path}"]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines[index + 1 :]:
        if not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        if cells is None or len(cells) != len(header):
            problems.append(f"ledger row needs {len(header)} cells: {path}: {line.strip()[:40]}")
            continue
        row_id, day, row_type, status = cells[:4]
        if not re.fullmatch(rf"{spec['prefix']}-\d{{3,}}", row_id):
            problems.append(f"ledger id invalid: {path}: {row_id}")
            continue
        if row_id in seen:
            problems.append(f"ledger id duplicated: {path}: {row_id}")
            continue
        seen.add(row_id)
        for label, value, allowed in (("유형", row_type, spec["types"]), ("상태", status, spec["statuses"])):
            if value not in allowed:
                problems.append(f"ledger {label} invalid: {path}: {row_id}")
        try:
            known_date = day == "-" or (re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and date.fromisoformat(day))
        except ValueError:
            known_date = False
        if not known_date:
            problems.append(f"ledger 날짜 invalid: {path}: {row_id}")
        successor = "" if kind != "decision_ledger" or cells[6] in {"", "-", "—", "–"} else cells[6]
        rows.append({
            "id": row_id, "date": day, "type": row_type, "status": status, "successor": successor, "cells": cells,
            "items": _src_tokens(line, "item:"), "docs": _src_tokens(line, "doc:"), "sess": _src_tokens(line, "sess:"),
        })
    for row in rows:
        if row["successor"] and row["successor"] not in seen - {row["id"]}:
            problems.append(f"ledger 후속 not found: {path}: {row['id']}")
        if row["status"] == "대체됨" and not row["successor"]:
            problems.append(f"ledger 대체됨 requires 후속: {path}: {row['id']}")
    return rows, problems


def brief_sections(text: str) -> list[tuple[str, str]]:
    """Level-2 sections as (heading, body); `###` and deeper stay inside their section."""
    matches = list(re.finditer(r"(?m)^## +(.+?) *$", text))
    return [
        (match.group(1), text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)])
        for index, match in enumerate(matches)
    ]


def _brief_chars(text: str) -> int:
    return len(_SRC_RE.sub("", text).strip())


def _section_ids(body: str) -> list[str]:
    """Ledger ids that open a table row (first cell) or a bullet."""
    ids: list[str] = []
    for line in body.split("\n"):
        cells = _cells(line)
        ids += re.findall(r"\b[DU]-\d{3,}\b", cells[0]) if cells else re.findall(r"^\s*[-*]\s+(?:\*\*)?([DU]-\d{3,})\b", line)
    return ids


def _doc_source_ok(token: str, original: dict[str, dict[str, Any]]) -> bool:
    """A migration source `doc:<path>[#<heading>]` must name a Git-base 20_Projects/10_User topic or user document (never a
    brief or ledger, or a row could cite itself) and its heading."""
    path, _, heading = token[4:].partition("#")
    path, heading = unquote(path), unquote(heading)
    document = original.get(path)
    if not document or not path.startswith(("20_Projects/", "10_User/")) or document.get("kind") in CURATOR_KINDS:
        return False
    headings = {_normal(match) for match in re.findall(r"(?m)^#{1,6} +(.+?) *$", str(document.get("text") or ""))}
    return not heading or _normal(heading) in headings


def _curator_checks(
    targets: set[str], current: dict[str, dict[str, Any]], original: dict[str, dict[str, Any]],
    session_of: dict[str, str], brief_cap: int,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Mechanical checks for BRIEF/PROFILE and the two ledgers; the meaning of a claim stays with the reviewer."""
    errors: list[str] = []
    warnings: list[str] = []
    numbers: dict[str, Any] = {"brief_cap": brief_cap, "brief_chars": {}, "ledger_rows": {}, "superseded_rows": {}}
    # A brief cites ledger rows by id, so a changed ledger also re-checks the brief beside it, targeted or not.
    ledger_dirs = {
        PurePosixPath(path).parent for path in targets if "header" in CURATOR_KINDS.get(current.get(path, {}).get("kind"), {})
    }
    targets = targets | {
        path for path, doc in current.items()
        if "label" in CURATOR_KINDS.get(doc.get("kind"), {}) and PurePosixPath(path).parent in ledger_dirs
    }
    for path in sorted(targets):
        document, base = current.get(path, {}), original.get(path, {})
        kind = document.get("kind")
        if base.get("kind") in CURATOR_KINDS and kind != base["kind"]:
            errors.append(f"curator document kind changed: {path}")
        spec = CURATOR_KINDS.get(kind)
        if spec is None:
            continue
        text, base_text = str(document.get("text") or ""), str(base.get("text") or "")
        if not path.startswith(spec["dir"]):
            errors.append(f"curator document not allowed here: {path}")
        errors += [
            f"doc source invalid: {path}: {token}" for token in sorted(_src_docs(text) - _src_docs(base_text))
            if not _doc_source_ok(token, original)
        ]
        if "header" in spec:
            rows, problems = ledger_entries(text, kind, path)
            errors += problems
            old_rows = {row["id"]: row for row in ledger_entries(base_text, kind, path)[0]} if base.get("kind") == kind else {}
            ceiling = max((int(row_id[2:]) for row_id in old_rows), default=0)
            errors += [f"ledger row deleted: {path}: {row_id}" for row_id in sorted(old_rows.keys() - {row["id"] for row in rows})]
            numbers["ledger_rows"][path] = len(rows)
            if kind == "decision_ledger":
                numbers["superseded_rows"][path] = sum(row["status"] == "대체됨" for row in rows)
            for row in rows:
                old, where = old_rows.get(row["id"]), f"{path}: {row['id']}"
                if old is None:
                    if int(row["id"][2:]) <= ceiling:
                        errors.append(f"ledger id not above the base maximum: {where}")
                    if not row["items"] | row["docs"]:
                        errors.append(f"ledger row without provenance: {where}")
                else:
                    # Only 상태 and the last column (후속 / 독립 세션) may change; history is never rewritten.
                    if [cell for index, cell in enumerate(row["cells"]) if index not in (3, 6)] != [
                        cell for index, cell in enumerate(old["cells"]) if index not in (3, 6)
                    ]:
                        errors.append(f"ledger row changed outside its mutable columns: {where}")
                    if not (old["items"] <= row["items"] and old["docs"] <= row["docs"] and old["sess"] <= row["sess"]):
                        errors.append(f"ledger row lost provenance: {where}")
                if kind != "user_ledger":
                    continue
                # 독립 세션 is the tool's count of distinct sessions behind the row, never the Curator's claim.
                want = (old["sess"] if old else set()) | {
                    session_of[item_id] for item_id in row["items"] - (old["items"] if old else set()) if session_of.get(item_id)
                }
                if row["sess"] != want or row["cells"][6] != str(len(want)):
                    errors.append(f"ledger 독립 세션 differs from the tool count: {where}")
                if row["status"] == "확인" and (old is None or old["status"] != "확인"):
                    if not row["items"] | row["docs"]:
                        errors.append(f"ledger 확인 needs a source: {where}")
                    if row["type"] == "선호(추론)" and len(row["sess"]) < 3:
                        errors.append(f"ledger 확인 needs 3 independent sessions: {where}")
            continue
        label, sections = spec["label"], brief_sections(text)
        if [name for name, _body in sections if name in spec["headings"]] != list(spec["headings"]):
            errors.append(f"{label} headings missing or out of order: {path}")
        chars = numbers["brief_chars"][path] = _brief_chars(text)
        if chars > brief_cap:
            errors.append(f"{label} over the cap: {path} ({chars} > {brief_cap})")
        bodies = dict(sections[::-1])  # a duplicated heading is reported above; the first one is read here
        folder = PurePosixPath(path).parent
        ledger = next((
            current[other] for other in sorted(current)
            if current[other].get("kind") == spec["ledger"] and PurePosixPath(other).parent == folder
        ), None)
        status = {row["id"]: row["status"] for row in ledger_entries(str(ledger.get("text") or ""), spec["ledger"])[0]} if ledger else {}
        listed = _section_ids(bodies.get(spec["section"], ""))
        if listed and not ledger:
            errors.append(f"{label} needs a {spec['ledger']}{' in its folder' if label == 'brief' else ''}: {path}")
        for row_id in listed:
            if status.get(row_id) != spec["status"]:
                bad = "non-valid" if label == "brief" else "non-confirmed"
                errors.append(f"{label} {spec['section']} lists a {bad} entry: {path}: {row_id}")
        for name in spec.get("soft", ()):
            warnings += [
                f"{label} references ledger id {row_id} that is not 유효: {path}"
                for row_id in _section_ids(bodies.get(name, "")) if status.get(row_id) != "유효"
            ]
    return errors, warnings, numbers


def check_compaction(
    plan: dict[str, Any], before: list[dict[str, Any]], after: list[dict[str, Any]],
    *, integrity_after: list[dict[str, Any]] | None = None, brief_cap: int = BRIEF_CAP,
) -> dict[str, Any]:
    """Check batch changes against Git, while preserving live-tree integrity checks."""
    if brief_cap < 1:
        raise ValueError("brief cap must be positive")
    errors: list[str] = []
    warnings: list[str] = []
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
    facts: dict[str, tuple[str, str]] = {}  # item id -> (event role, normalized own text) for quote and role checks
    for path in paths:
        source = original.get(path)
        if not source or not path.startswith(("30_Conversations/raw/", "40_Agents/")) or frozen.get(path) != source.get("document_hash"):
            errors.append(f"source does not match Git base: {path}")
            continue
        for item in _raw_items(source, body=True):
            item_id = "item:" + hashlib.sha256(f"{path}\0{item['locator']}".encode("utf-8")).hexdigest()[:16]
            if item_id in expected:
                errors.append(f"ambiguous item identity: {item_id}")
            expected[item_id] = path
            facts[item_id] = (item.get("event_role", ""), _normal(item["body"]))
    session_of = {item_id: _doc_sess(original[path]) for item_id, path in expected.items()}
    entries = _review_items(review.get("items"))
    errors += [
        f"claim items cannot be grouped under ids: {raw['disposition']}"
        for raw in review["items"] if "ids" in raw and raw.get("disposition") in CLAIM_DISPOSITIONS
    ]
    deleted = review.get("delete_paths")
    if not isinstance(deleted, list) or any(not isinstance(path, str) for path in deleted):
        raise ValueError("review.delete_paths must be a list of paths")
    if not deleted or len(set(deleted)) != len(deleted) or not set(deleted) <= set(paths):
        errors.append("delete_paths must select distinct planned source files")
    classified: set[str] = set()
    targets: set[str] = set()
    retained: set[str] = set()
    owed: defaultdict[str, set[str]] = defaultdict(set)
    for entry in entries:
        item_id = entry.get("id")
        disposition = entry.get("disposition")
        destinations = entry.get("targets")
        if not isinstance(item_id, str) or item_id not in expected or item_id in classified:
            errors.append(f"unknown or duplicate reviewed item: {item_id}")
            continue
        classified.add(item_id)
        if disposition not in DISPOSITIONS:
            errors.append(f"unclassified item: {item_id}")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            errors.append(f"item requires a reason: {item_id}")
        if not isinstance(destinations, list) or any(not isinstance(path, str) for path in destinations):
            raise ValueError("item.targets must be a list of paths")
        if disposition in MARKED_DISPOSITIONS | {"already-covered"} and not destinations:
            errors.append(f"durable item requires a target: {item_id}")
        if disposition in LEGACY_DISPOSITIONS:
            warnings.append("legacy disposition merge/replace is deprecated: use a typed disposition")
        quote = entry.get("quote")
        if quote is None:
            if disposition in CLAIM_DISPOSITIONS:
                errors.append(f"claim item requires a quote: {item_id}")
        elif not isinstance(quote, str) or not 4 <= len(_normal(quote)) <= 200:
            errors.append(f"quote must be 4 to 200 characters: {item_id}")
        elif _normal(quote) not in facts[item_id][1]:
            errors.append(f"quote not found in item: {item_id}")
        if facts[item_id][0] == "subagent" and disposition not in SUBAGENT_DISPOSITIONS:
            errors.append(f"subagent item may only be knowledge, already-covered, discard or hold: {item_id}")
        if disposition == "hold":
            retained.add(expected[item_id])
            if expected[item_id] in deleted:
                errors.append(f"held source must be retained: {expected[item_id]}")
        for target in destinations:
            kind = current.get(target, {}).get("kind")
            if (
                target not in current
                or not target.startswith(("10_User/", "20_Projects/", "50_Knowledge/", "30_Conversations/summaries/"))
                or current[target].get("metadata_valid") is False
            ):
                errors.append(f"missing or invalid knowledge target: {target}")
            elif disposition in PROJECT_DISPOSITIONS and kind not in {"brief", "decision_ledger"}:
                errors.append(f"project claim must target a brief or decision_ledger: {target}")
            elif disposition in USER_DISPOSITIONS and kind not in {"user_profile", "user_ledger"}:
                errors.append(f"user claim must target a user_profile or user_ledger: {target}")
            elif disposition == "knowledge" and kind in CURATOR_KINDS:
                errors.append(f"knowledge item cannot target a curator document: {target}")
            elif disposition in LEGACY_DISPOSITIONS and kind in CURATOR_KINDS:
                errors.append(f"legacy merge/replace cannot target a curator document: {target}")
            targets.add(target)
            if disposition in MARKED_DISPOSITIONS:
                owed[target].add(item_id)
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
    # New markers must be backed by a claim review of that target. A missing marker is an error in a curator
    # document and only a count elsewhere, until compact-annotate has been used on a real batch.
    unmarked: set[str] = set()
    for target in targets:
        have = _src_ids(str(current.get(target, {}).get("text") or ""))
        if have - _src_ids(str(original.get(target, {}).get("text") or "")) - owed[target]:
            errors.append(f"provenance marker without a claim review: {target}")
        missing = owed[target] - have
        if missing and current.get(target, {}).get("kind") in CURATOR_KINDS:
            errors.append(f"claim without provenance marker: {target}")
        else:
            unmarked |= missing
    curator_errors, curator_warnings, curation = _curator_checks(targets, current, original, session_of, brief_cap)
    errors += curator_errors
    warnings += curator_warnings
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
        "warnings": sorted(set(warnings)),
        "curation": curation,
        "classified_items": len(classified),
        "item_count": len(expected),
        "active_characters": {"before": before_chars, "after": after_chars, "delta": after_chars - before_chars},
        "retained_sources": sorted(retained),
        "provenance": {"unmarked_items": len(unmarked), "examples": sorted(unmarked)[:20]},
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
