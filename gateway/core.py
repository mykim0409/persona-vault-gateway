from __future__ import annotations

import json
import math
import os
import platform
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
import http.client
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any

from .wiki import _SRC_RE


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    allowed_roots TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    rotated_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    agent_id TEXT,
    action TEXT NOT NULL,
    route TEXT NOT NULL,
    status TEXT NOT NULL,
    remote_addr TEXT,
    user_agent TEXT,
    request_id TEXT,
    target_path TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS rag_index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
MIGRATIONS = (SCHEMA,)
DB_SCHEMA_VERSION = len(MIGRATIONS)

AGENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
REPOSITORY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,64}")
TOKEN_RE = re.compile(r"[0-9a-zA-Z가-힣_]+")
MAX_FRONTMATTER_DEPTH = 16
QUERY_STOPWORDS = frozenset(
    "about and are can does for from has have how into not that the this was what when where which who why will with you your".split()
    + "is it be to of in on at as by or an do we if so".split()
)
BLOCKED_ROOTS = tuple(
    PurePosixPath(path)
    for path in (
        ".obsidian",
        "90_Private",
        "30_Conversations/summaries",
        "30_Conversations/important",
        "50_Knowledge",
    )
)
RAG_EXCLUDED_DIRS = {".git", ".obsidian", ".tmp"}
HASH_EMBEDDING_MODEL = "local-hash-v1"
HASH_EMBEDDING_DIMENSIONS = 256
CLOUDFLARE_EMBEDDING_MODEL = "@cf/qwen/qwen3-embedding-0.6b"
CLOUDFLARE_EMBEDDING_DIMENSIONS = 1024
CLOUDFLARE_EMBEDDING_INSTRUCTION = (
    "Retrieve PersonaVault passages relevant to a Korean or English query about current decisions, "
    "project history, experiment outcomes, failures, and technical evidence."
)
CLOUDFLARE_EMBEDDING_TIMEOUT_SECONDS = 60
CLOUDFLARE_EMBEDDING_MAX_RETRIES = 3
# Interactive search must answer inside the shared client's 60s window. Per-request socket timeouts
# (urllib is not a wall-clock deadline): 1 query embedding + exists + count + <=3 bundle queries.
CLOUDFLARE_QUERY_TIMEOUT_SECONDS = 10
QDRANT_TIMEOUT_SECONDS = 30
QDRANT_SEARCH_TIMEOUT_SECONDS = 5
CLOUDFLARE_RETRYABLE_STATUS = {500, 502, 503, 504}
EMBEDDING_RETRY_GRACE_SECONDS = 60
RAG_CHUNK_CHARS = 1800
RAG_CHUNK_OVERLAP = 180
RAG_INDEX_SCHEMA = "5"
MAX_CONVERSATION_BYTES = 4 * 1024 * 1024
WORKING_AGREEMENT_PATH = PurePosixPath("10_User/WORKING_AGREEMENT.md")
MAX_WORKING_AGREEMENT_CHARS = 8_000
MEMORY_TYPES = {"transcript", "episode", "candidate", "canonical", "derived_view"}
AGENT_NOTE_TYPES = {"observation", "proposal", "handoff"}
OUTCOMES = {"success", "failure", "mixed", "unknown", "not_applicable"}
REVIEW_STATES = {"unreviewed", "human_accepted", "human_rejected", "merged"}
TEMPORAL_STATES = {"point_observation", "proposed_current", "current", "historical", "superseded", "unknown"}
RETRIEVAL_TIERS = {"primary", "supporting", "evidence", "history", "archive"}
RAG_BUNDLES = {"auto", "current", "evidence", "experiences", "history", "conflicts"}
RAG_INDEX_LOCK = Lock()


class EmbeddingLimitError(RuntimeError):
    pass


class ConversationConflictError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    vault_dir: Path
    db_path: Path
    host_id: str
    embedding_provider: str = "cloudflare"
    embedding_model: str = CLOUDFLARE_EMBEDDING_MODEL
    embedding_batch_size: int = 32
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "persona_vault"
    qdrant_api_key: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            vault_dir=Path(os.getenv("VAULT_DIR", "vault")).resolve(),
            db_path=Path(os.getenv("DB_PATH", "data/gateway.db")).resolve(),
            host_id=os.getenv("HOST_ID", platform.node() or "unknown-host"),
            embedding_provider=os.getenv("EMBEDDING_PROVIDER", "cloudflare").lower(),
            embedding_model=os.getenv("EMBEDDING_MODEL", CLOUDFLARE_EMBEDDING_MODEL),
            embedding_batch_size=max(1, int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))),
            cloudflare_account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(),
            cloudflare_api_token=os.getenv("CLOUDFLARE_API_TOKEN", "").strip(),
            qdrant_url=os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/"),
            qdrant_collection=os.getenv("QDRANT_COLLECTION", "persona_vault"),
            qdrant_api_key=os.getenv("QDRANT_API_KEY") or None,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ClosingConnection(sqlite3.Connection):
    """`with` commits or rolls back like sqlite3, then also closes the connection."""

    def __exit__(self, *exc_info: Any) -> bool | None:
        try:
            return super().__exit__(*exc_info)
        finally:
            self.close()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, factory=_ClosingConnection)
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: Path) -> None:
    with connect(db_path) as con:
        current = int(con.execute("PRAGMA user_version").fetchone()[0])
        if current > DB_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {current} is newer than supported version {DB_SCHEMA_VERSION}"
            )
        pending = [
            f"{script}\nPRAGMA user_version = {version};"
            for version, script in enumerate(MIGRATIONS, start=1)
            if version > current
        ]
        if pending:
            con.executescript(f"BEGIN IMMEDIATE;\n{'\n'.join(pending)}\nCOMMIT;")


def generate_token() -> str:
    return f"pvg_{secrets.token_urlsafe(32)}"


def comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def hash_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def upsert_agent(
    db_path: Path,
    agent_id: str,
    token: str,
    scopes: list[str],
    allowed_roots: list[str],
) -> None:
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError("agent_id must be lowercase letters, numbers, '-' or '_'")
    for root in allowed_roots:
        normalized = normalize_allowed_root(root)
        if normalized.parts[0] == "40_Agents" and normalized.parts[1] != agent_id:
            raise ValueError("agent may only write to its own 40_Agents root")
    now = utc_now()
    with connect(db_path) as con:
        con.execute(
            """
            INSERT INTO agents (
                agent_id, token_prefix, token_hash, scopes, allowed_roots,
                enabled, created_at, rotated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                token_prefix=excluded.token_prefix,
                token_hash=excluded.token_hash,
                scopes=excluded.scopes,
                allowed_roots=excluded.allowed_roots,
                enabled=1,
                rotated_at=excluded.rotated_at
            """,
            (
                agent_id,
                token[:12],
                hash_token(token),
                json.dumps(scopes),
                json.dumps(allowed_roots),
                now,
                now,
            ),
        )


def lookup_agent(db_path: Path, token: str) -> dict[str, Any] | None:
    digest = hash_token(token)
    with connect(db_path) as con:
        agent = con.execute(
            "SELECT * FROM agents WHERE token_hash = ? AND enabled = 1",
            (digest,),
        ).fetchone()
        if agent:
            con.execute(
                "UPDATE agents SET last_used_at = ? WHERE id = ?",
                (utc_now(), agent["id"]),
            )
            return {
                "agent_id": agent["agent_id"],
                "scopes": json.loads(agent["scopes"]),
                "allowed_roots": json.loads(agent["allowed_roots"]),
            }
    return None


def list_agents(db_path: Path) -> list[dict[str, Any]]:
    with connect(db_path) as con:
        rows = con.execute(
            """
            SELECT agent_id, token_prefix, scopes, allowed_roots, enabled,
                   last_used_at, rotated_at
            FROM agents
            ORDER BY agent_id
            """
        ).fetchall()
    return [
        {
            "agent_id": row["agent_id"],
            "token_prefix": row["token_prefix"],
            "scopes": json.loads(row["scopes"]),
            "allowed_roots": json.loads(row["allowed_roots"]),
            "enabled": bool(row["enabled"]),
            "last_used_at": row["last_used_at"],
            "rotated_at": row["rotated_at"],
        }
        for row in rows
    ]


def disable_agent(db_path: Path, agent_id: str) -> bool:
    with connect(db_path) as con:
        result = con.execute(
            "UPDATE agents SET enabled = 0 WHERE agent_id = ?",
            (agent_id,),
        )
        return result.rowcount > 0


def audit(
    db_path: Path,
    *,
    action: str,
    route: str,
    status: str,
    agent_id: str | None = None,
    remote_addr: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    target_path: str | None = None,
    error_message: str | None = None,
) -> None:
    with connect(db_path) as con:
        con.execute(
            """
            INSERT INTO audit_logs (
                timestamp, agent_id, action, route, status, remote_addr,
                user_agent, request_id, target_path, error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                agent_id,
                action,
                route,
                status,
                remote_addr,
                user_agent,
                request_id,
                target_path,
                error_message,
            ),
        )


def require_scope(agent: dict[str, Any], scope: str) -> None:
    if scope not in agent["scopes"]:
        raise PermissionError(f"missing scope: {scope}")


def slugify(value: str, fallback: str = "untitled") -> str:
    words = re.findall(r"[a-z0-9]+", value.lower())
    return "-".join(words)[:80] or fallback


def one_line(value: Any, fallback: str) -> str:
    text = str(value or fallback).replace("\r", " ").replace("\n", " ").strip()
    return text or fallback


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def yaml_scalar(value: Any) -> str:
    text = json.dumps("" if value is None else value, ensure_ascii=False)
    # str.splitlines() treats these as line breaks; keep them escaped so a value stays on one line.
    return text.replace("\x85", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def yaml_list(values: list[str], indent: str = "") -> str:
    if not values:
        return f"{indent}[]"
    return "\n".join(f"{indent}- {yaml_scalar(value)}" for value in values)


def clean_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip()[:80] for value in values if str(value).strip()]


def parse_frontmatter(text: str) -> dict[str, Any]:
    # Split on LF only: U+0085/U+2028/U+2029 may legitimately appear inside values.
    lines = text.split("\n")
    if lines[0].strip(" \t\r") != "---":
        return {}
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip(" \t\r") == "---")
        entries = [
            (len(line) - len(line.lstrip(" ")), line.strip(" \t\r"))
            for line in lines[1:end]
            if line.strip(" \t\r") and not line.lstrip(" \t").startswith("#")
        ]
        if not entries:
            return {}
        metadata, index = parse_frontmatter_node(entries, 0, entries[0][0])
        if index != len(entries) or not isinstance(metadata, dict) or frontmatter_depth(metadata) > MAX_FRONTMATTER_DEPTH:
            raise ValueError("unsupported frontmatter structure")
        return metadata
    except (StopIteration, ValueError, RecursionError):
        return {"_pvg_metadata_error": True}


def frontmatter_depth(value: Any) -> int:
    deepest = 0
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        deepest = max(deepest, depth)
        if deepest > MAX_FRONTMATTER_DEPTH:
            break
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return deepest


def parse_frontmatter_node(
    entries: list[tuple[int, str]],
    index: int,
    indent: int,
    depth: int = 1,
) -> tuple[Any, int]:
    if depth > MAX_FRONTMATTER_DEPTH:
        raise ValueError("frontmatter is nested too deeply")
    if entries[index][1].startswith("- "):
        values: list[Any] = []
        while index < len(entries):
            current_indent, content = entries[index]
            if current_indent != indent or not content.startswith("- "):
                break
            raw = content[2:].strip()
            index += 1
            if not raw:
                if index >= len(entries) or entries[index][0] <= indent:
                    values.append(None)
                else:
                    value, index = parse_frontmatter_node(entries, index, entries[index][0], depth + 1)
                    values.append(value)
                continue
            if ":" in raw and not raw.startswith(("{", "[", "'", '"')):
                key, scalar = raw.split(":", 1)
                item: dict[str, Any] = {key.strip(): parse_frontmatter_value(scalar.strip()) if scalar.strip() else None}
                if index < len(entries) and entries[index][0] > indent:
                    nested, index = parse_frontmatter_node(entries, index, entries[index][0], depth + 1)
                    if not isinstance(nested, dict):
                        raise ValueError("list mapping must contain a mapping")
                    item.update(nested)
                values.append(item)
            else:
                values.append(parse_frontmatter_value(raw))
        return values, index

    values: dict[str, Any] = {}
    while index < len(entries):
        current_indent, content = entries[index]
        if current_indent < indent:
            break
        if current_indent != indent or content.startswith("- ") or ":" not in content:
            raise ValueError("invalid mapping indentation")
        key, raw = content.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        index += 1
        if raw:
            values[key] = parse_frontmatter_value(raw)
        elif index < len(entries) and entries[index][0] > indent:
            values[key], index = parse_frontmatter_node(entries, index, entries[index][0], depth + 1)
        else:
            values[key] = None
    return values, index


def markdown_body(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4 :].lstrip() if end >= 0 else text


def parse_frontmatter_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        lowered = raw.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"null", "none", "~"}:
            return None
        return raw.strip('"\'')


def metadata_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def project_name(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    return text[:120]


def reference_ids(value: Any) -> list[str]:
    refs: list[str] = []
    for item in metadata_list(value):
        if isinstance(item, dict):
            item = item.get("id") or item.get("locator")
        text = str(item or "").strip()
        if text:
            refs.append(text[:300])
    return refs


def repository_sources(value: Any) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for item in metadata_list(value):
        if not isinstance(item, dict):
            raise ValueError("repository_sources entries must be objects")
        repo_id = str(item.get("repo_id") or "")
        commit = str(item.get("commit") or "")
        path = str(item.get("path") or "")
        rel = PurePosixPath(path)
        if not REPOSITORY_ID_RE.fullmatch(repo_id):
            raise ValueError("repository source repo_id is invalid")
        if not COMMIT_RE.fullmatch(commit):
            raise ValueError("repository source commit must be a 7-64 character hex revision")
        if (
            not path
            or "\0" in path
            or "\\" in path
            or rel.is_absolute()
            or not rel.parts
            or ".." in rel.parts
            or path.endswith("/")
        ):
            raise ValueError("repository source path must be a relative POSIX file path")
        source = {"repo_id": repo_id, "commit": commit, "path": path}
        anchor = str(item.get("anchor") or "").strip()
        if anchor:
            source["anchor"] = anchor[:160]
        sources.append(source)
    return sources


def document_metadata(rel_path: str, text: str) -> dict[str, Any]:
    frontmatter = parse_frontmatter(text)
    metadata_error = bool(frontmatter.pop("_pvg_metadata_error", False))
    review = frontmatter.get("review") if isinstance(frontmatter.get("review"), dict) else {}
    temporal = frontmatter.get("temporal") if isinstance(frontmatter.get("temporal"), dict) else {}
    provenance = frontmatter.get("provenance") if isinstance(frontmatter.get("provenance"), dict) else {}
    for flat, nested in (
        (frontmatter.get("review_state"), review.get("state")),
        (frontmatter.get("temporal_state"), temporal.get("state")),
        (frontmatter.get("observed_at"), temporal.get("observed_at")),
        (frontmatter.get("effective_from"), temporal.get("effective_from")),
        (frontmatter.get("effective_to"), temporal.get("effective_to")),
        (frontmatter.get("provenance_mode"), provenance.get("mode")),
    ):
        if flat is not None and nested is not None and str(flat) != str(nested):
            metadata_error = True
    if frontmatter.get("pv_schema") not in (None, 1, "1"):
        metadata_error = True
    parts = PurePosixPath(rel_path).parts
    agent_root = bool(parts and parts[0] == "40_Agents")
    raw_conversation_root = parts[:2] == ("30_Conversations", "raw")
    tags = clean_list(metadata_list(frontmatter.get("tags") or frontmatter.get("topics")))
    declared_type = str(frontmatter.get("memory_type") or "")
    if declared_type and declared_type not in MEMORY_TYPES and not agent_root:
        metadata_error = True

    if raw_conversation_root:
        memory_type = "transcript"
    elif agent_root:
        memory_type = (
            "candidate"
            if declared_type == "candidate" or "candidates" in parts or "knowledge-candidate" in tags
            else "episode"
        )
    elif metadata_error:
        memory_type = "unknown"
    elif declared_type in MEMORY_TYPES:
        memory_type = declared_type
    elif frontmatter.get("type") == "conversation":
        memory_type = "transcript"
    elif parts[:2] in {("30_Conversations", "summaries"), ("30_Conversations", "important")}:
        memory_type = "derived_view"
    elif parts and parts[0] == "00_Inbox":
        memory_type = "transcript"
    else:
        memory_type = "canonical"

    kind = str(frontmatter.get("kind") or "").strip()
    if not kind:
        kind = next(
            (
                tag
                for tag in tags
                if tag in {"debugging", "procedure", "decision", "lesson", "correction", "handoff", "personal-context"}
            ),
            "conversation" if memory_type == "transcript" else "note",
        )

    status = str(frontmatter.get("status") or "")
    review_state = str(frontmatter.get("review_state") or review.get("state") or "")
    if review_state and review_state not in REVIEW_STATES:
        metadata_error = True
    if agent_root or raw_conversation_root:
        review_state = "unreviewed"
    elif metadata_error:
        review_state = "unreviewed"
    elif not review_state:
        review_state = "human_accepted" if memory_type == "canonical" else status or "unreviewed"

    temporal_state = str(frontmatter.get("temporal_state") or temporal.get("state") or "")
    if temporal_state and temporal_state not in TEMPORAL_STATES:
        metadata_error = True
    if agent_root:
        temporal_state = "proposed_current" if memory_type == "candidate" else "point_observation"
    elif raw_conversation_root:
        temporal_state = "point_observation"
    elif metadata_error:
        temporal_state = "unknown"
    elif not temporal_state:
        if status in {"historical", "superseded"}:
            temporal_state = status
        elif memory_type == "canonical":
            temporal_state = "current"
        elif memory_type == "candidate":
            temporal_state = "proposed_current"
        elif memory_type in {"episode", "transcript"}:
            temporal_state = "point_observation"
        else:
            temporal_state = "unknown"

    outcome = str(frontmatter.get("outcome") or "unknown")
    if outcome not in OUTCOMES:
        outcome = "unknown"
        metadata_error = True

    retrieval_tier = str(frontmatter.get("retrieval_tier") or "")
    if retrieval_tier and retrieval_tier not in RETRIEVAL_TIERS:
        metadata_error = True
    if frontmatter.get("rag_index") is False or kind in {"smoke-test", "help-output"}:
        retrieval_tier = "archive"
    elif agent_root:
        retrieval_tier = "supporting"
    elif raw_conversation_root:
        retrieval_tier = "evidence"
    elif metadata_error:
        retrieval_tier = "evidence"
    elif not retrieval_tier:
        retrieval_tier = {
            "canonical": "primary",
            "episode": "supporting",
            "candidate": "supporting",
            "transcript": "evidence",
            "derived_view": "supporting",
        }.get(memory_type, "evidence")

    relations = frontmatter.get("relations") if isinstance(frontmatter.get("relations"), dict) else {}
    conflict_state = str(frontmatter.get("conflict_state") or "none")
    if agent_root:
        conflict_state = "unresolved" if relations.get("contradicts") else "none"
    elif relations.get("contradicts") and conflict_state == "none":
        conflict_state = "unresolved"

    declared_provenance = frontmatter.get("provenance_mode") or provenance.get("mode")
    provenance_mode = str(declared_provenance or "reported")
    if provenance_mode not in {"direct_observation", "derived", "reported", "human_asserted"}:
        provenance_mode = "reported"
        declared_provenance = None
        metadata_error = True
    evidence_refs = reference_ids(frontmatter.get("evidence_refs") or provenance.get("evidence_refs"))
    method_refs = reference_ids(frontmatter.get("method_refs") or provenance.get("method_refs"))
    derived_from = reference_ids(frontmatter.get("derived_from") or provenance.get("derived_from"))
    projects = [project_name(item) for item in metadata_list(frontmatter.get("projects") or frontmatter.get("project"))]
    projects = [project for project in projects if project]

    document_id = str(frontmatter.get("id") or frontmatter.get("conversation_id") or "")
    if not document_id:
        document_id = f"doc_{sha256(rel_path.encode('utf-8')).hexdigest()[:20]}"

    applicability = frontmatter.get("applicability")
    observed_at = (
        frontmatter.get("observed_at")
        or temporal.get("observed_at")
        or frontmatter.get("started_at")
        or ""
    )
    if metadata_error and not (agent_root or raw_conversation_root):
        memory_type = "unknown"
        review_state = "unreviewed"
        temporal_state = "unknown"
        retrieval_tier = "evidence"
    return {
        "document_id": document_id,
        "memory_type": memory_type,
        "kind": kind,
        "review_state": review_state,
        "temporal_state": temporal_state,
        "outcome": outcome,
        "retrieval_tier": retrieval_tier,
        "agent_id": parts[1] if agent_root and len(parts) > 1 else str(frontmatter.get("agent_id") or ""),
        "session_id": str(frontmatter.get("session_id") or ""),
        "capture_kind": str(frontmatter.get("capture_kind") or ""),
        "created_at": str(frontmatter.get("created_at") or ""),
        "observed_at": str(observed_at),
        "effective_from": str(frontmatter.get("effective_from") or temporal.get("effective_from") or ""),
        "effective_to": str(frontmatter.get("effective_to") or temporal.get("effective_to") or ""),
        "projects": projects,
        "topics": tags,
        "subject_id": str(frontmatter.get("subject_id") or frontmatter.get("target_subject_id") or ""),
        "subject_aliases": clean_list(metadata_list(frontmatter.get("subject_aliases"))),
        "error_signatures": clean_list(metadata_list(frontmatter.get("error_signatures"))),
        "applicability": applicability if isinstance(applicability, dict) else {},
        "provenance_mode": provenance_mode,
        "provenance_defaulted": bool(frontmatter.get("provenance_defaulted")) or not bool(declared_provenance),
        "evidence_refs": evidence_refs,
        "method_refs": method_refs,
        "derived_from": derived_from,
        "conflict_state": conflict_state,
        "conflict_id": str(frontmatter.get("conflict_id") or ""),
        "resolution_state": str(frontmatter.get("resolution_state") or ""),
        "curator": str(frontmatter.get("curator") or ""),
        "curation_note": str(frontmatter.get("curation_note") or ""),
        "relations": relations,
        "source_refs": reference_ids(frontmatter.get("source_refs")),
        "source_hashes": frontmatter.get("source_hashes") if isinstance(frontmatter.get("source_hashes"), dict) else {},
        "repository_sources": [
            item for item in metadata_list(frontmatter.get("repository_sources")) if isinstance(item, dict)
        ],
        "metadata_valid": not metadata_error,
    }


def load_working_agreement(settings: Settings, agent: dict[str, Any]) -> dict[str, Any]:
    require_scope(agent, "vault-rag")
    rel_path = WORKING_AGREEMENT_PATH.as_posix()
    target = settings.vault_dir.joinpath(*WORKING_AGREEMENT_PATH.parts)
    if not target.exists():
        return {"status": "absent", "path": rel_path, "content": ""}

    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(settings.vault_dir.resolve())
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError("working agreement must be a file inside the vault") from exc
    if not resolved.is_file():
        raise ValueError("working agreement must be a file inside the vault")

    text = resolved.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(text)
    metadata = document_metadata(rel_path, text)
    required = {
        "pv_schema": 1,
        "id": "user_working_agreement",
        "memory_type": "canonical",
        "review_state": "human_accepted",
        "temporal_state": "current",
        "provenance_mode": "human_asserted",
        "retrieval_tier": "primary",
    }
    if not metadata["metadata_valid"] or any(frontmatter.get(key) != value for key, value in required.items()):
        return {
            "status": "unavailable",
            "path": rel_path,
            "content": "",
            "reason": "working_agreement_not_approved",
        }

    content = markdown_body(text).strip()
    if not content or len(content) > MAX_WORKING_AGREEMENT_CHARS:
        return {
            "status": "unavailable",
            "path": rel_path,
            "content": "",
            "reason": "working_agreement_empty_or_too_large",
        }
    return {"status": "ok", "path": rel_path, "content": content}


def normalize_rel(path: str) -> PurePosixPath:
    if "\0" in path:
        raise ValueError("path contains null byte")
    rel = PurePosixPath(path)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise ValueError("invalid relative path")
    if {".git", ".obsidian"} & set(rel.parts) or any(is_under(rel, root) for root in BLOCKED_ROOTS):
        raise ValueError("blocked vault path")
    return rel


def normalize_allowed_root(path: str) -> PurePosixPath:
    rel = normalize_rel(path)
    if rel == PurePosixPath("30_Conversations/raw"):
        return rel
    if len(rel.parts) == 2 and rel.parts[0] == "40_Agents" and AGENT_ID_RE.fullmatch(rel.parts[1]):
        return rel
    raise ValueError("allowed root must be 30_Conversations/raw or one 40_Agents/<agent_id> root")


def is_under(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path.parts[: len(root.parts)] == root.parts


def target_path(settings: Settings, rel_path: str, allowed_roots: list[str]) -> Path:
    rel = normalize_rel(rel_path)
    if rel.suffix.lower() != ".md":
        raise ValueError("vault writes must be Markdown")
    roots = [normalize_allowed_root(root) for root in allowed_roots]
    if not any(is_under(rel, root) for root in roots):
        raise PermissionError("path is outside allowed roots")
    target = settings.vault_dir.joinpath(*rel.parts)
    # A symlink may stay inside its allowed root, but must not redirect a write to another vault area.
    resolved = target.resolve().relative_to(settings.vault_dir.resolve())
    if not any(is_under(PurePosixPath(resolved.as_posix()), root) for root in roots):
        raise PermissionError("resolved path is outside allowed roots")
    return target


def write_markdown(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            file.write(body)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_markdown(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as file:
            file.write(body)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def render_messages(messages: list[dict[str, Any]], *, framed: bool = False) -> str:
    rendered = []
    for message in messages:
        role = slugify(str(message.get("role", "message")), "message")
        agent_type = one_line(message.get("agent_type"), "") if message.get("agent_type") else ""
        heading = f"{role}: {agent_type}" if agent_type else role
        metadata = {
            key: message[key]
            for key in ("timestamp", "event_id", "turn_id", "agent_id", "agent_type")
            if message.get(key)
        }
        if framed:
            metadata.update(
                role=message.get("role", "message"),
                content_chars=len(message.get("content", "")),
                request_chars=len(message.get("request") or ""),
            )
        details = f"<!-- pvg-event {json.dumps(metadata, ensure_ascii=False, sort_keys=True)} -->\n\n" if metadata else ""
        request = message.get("request")
        if request:
            details += f"**Agent delegation (not a user statement)**\n\n{request}\n\n**Result**\n\n"
        rendered.append(f"### {heading}\n\n{details}{message.get('content', '')}")
    return "\n\n".join(rendered)


def unique_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for message in messages:
        event_id = str(message.get("event_id") or "")
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        result.append(message)
    return result


def search_vault(
    settings: Settings,
    agent: dict[str, Any],
    query: str,
    limit: int = 5,
    refresh: bool = False,
    bundle: str = "auto",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_scope(agent, "vault-rag")
    query = query.strip()
    if bundle not in RAG_BUNDLES:
        raise ValueError(f"bundle must be one of: {', '.join(sorted(RAG_BUNDLES))}")
    if not query:
        return {
            "query": query,
            "bundle_type": bundle,
            "results": [],
            "groups": [],
            "context": "",
            "index": {"files": 0, "chunks": 0, "updated": 0},
        }

    max_results = max(1, min(limit, 20))
    if settings.embedding_provider == "none":
        # Keyword-only mode: neither refresh nor search may touch Qdrant or an embedding provider.
        model = "none"
        reason = "semantic_disabled"
        index = {
            "files": 0,
            "chunks": 0,
            "updated": 0,
            "provider": "none",
            "model": "none",
            "dimension": 0,
            "store": "none",
            "stale": False,
        }
    else:
        refreshed = index_vault(settings) if refresh else None
        model = selected_embedding_model(settings)
        meta = rag_index_meta(settings)
        fingerprint = vault_fingerprint(settings.vault_dir)
        index = refreshed or {
            "files": int_meta(meta.get("files")),
            "chunks": int_meta(meta.get("chunks")),
            "updated": 0,
            "provider": meta.get("provider") or settings.embedding_provider,
            "model": model,
            "dimension": embedding_size(settings),
            "store": "qdrant",
            "stale": meta.get("fingerprint") != fingerprint,
        }
        reason = ""
        try:
            exists = qdrant_json(
                settings, "GET", f"/collections/{settings.qdrant_collection}/exists", timeout=QDRANT_SEARCH_TIMEOUT_SECONDS
            )["result"]["exists"]
            if exists:
                index["chunks"] = int(qdrant_json(
                    settings,
                    "POST",
                    f"/collections/{settings.qdrant_collection}/points/count",
                    {"exact": True},
                    timeout=QDRANT_SEARCH_TIMEOUT_SECONDS,
                )["result"]["count"])
            else:
                index["chunks"] = 0
            if RAG_INDEX_LOCK.locked() or meta.get("rebuild_state") == "building":
                reason = "index_building"
            elif not exists:
                reason = "missing_index"
            elif meta.get("rebuild_state") != "ready" or int_meta(meta.get("chunks"), -1) != index["chunks"]:
                reason = "incomplete_index"
            elif (
                meta.get("schema") != RAG_INDEX_SCHEMA
                or meta.get("provider") != settings.embedding_provider
                or meta.get("model") != model
                or int_meta(meta.get("dimension"), -1) != embedding_size(settings)
            ):
                reason = "incompatible_index"
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            reason = "qdrant_unavailable"
            index["chunks"] = None
    query_vector = None
    if not reason:
        try:
            query_vector, model = embed_query(settings, query)
        except EmbeddingLimitError:
            reason = "embedding_limit"
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            reason = "embedding_unavailable"
    if reason:
        index.update(search_mode="keyword", fallback_reason=reason)
    terms = keyword_terms(query)
    documents = vault_documents(settings, skip_unreadable=True)
    analysis: dict[str, Any] | None = None
    try:
        from .wiki import analyze_documents, bundle_groups

        analysis = analyze_documents(documents)
        documents = [
            {**document, **result_analysis_fields(analysis["by_id"].get(document["document_id"], {}))}
            for document in documents
        ]
    except ImportError:
        pass
    current_documents = {document["path"]: document for document in documents}
    filtered = 0
    scored: dict[tuple[str, int], dict[str, Any]] = {}

    def add_result(item: dict[str, Any]) -> None:
        key = (str(item.get("path", "")), int(item.get("chunk_index", 0)))
        existing = scored.get(key)
        if not existing:
            scored[key] = item
            return
        if float(item["score"]) > float(existing["score"]):
            previous_match = existing.get("match")
            scored[key] = item
            existing = item
            if previous_match != item.get("match"):
                existing["match"] = "hybrid"
            return
        if existing.get("match") != item.get("match"):
            existing["match"] = "hybrid"

    candidate_limit = max(20, max_results * 6)
    for query_filter in qdrant_bundle_filters(bundle) if query_vector is not None else []:
        request_body: dict[str, Any] = {
            "query": query_vector,
            "limit": candidate_limit,
            "with_payload": True,
        }
        if query_filter:
            request_body["filter"] = query_filter
        try:
            payload = qdrant_json(
                settings,
                "POST",
                f"/collections/{settings.qdrant_collection}/points/query",
                request_body,
                timeout=QDRANT_SEARCH_TIMEOUT_SECONDS,
            )
            points = payload["result"]["points"]
            if not isinstance(points, list):
                raise ValueError("invalid Qdrant query response")
            for point in points:
                payload_item = point["payload"]
                path = payload_item["path"]
                if not isinstance(path, str) or not isinstance(payload_item["text"], str):
                    raise ValueError("invalid Qdrant payload")
                semantic_score = float(point["score"])
                if not math.isfinite(semantic_score):
                    raise ValueError("invalid Qdrant score")
                if not path:
                    continue
                # The index may predate vault edits: trust a hit only for an unchanged document, and
                # take every metadata/analysis field from the current vault instead of the payload.
                current = current_documents.get(path)
                if not current or payload_item.get("document_hash") != current["document_hash"]:
                    filtered += 1
                    continue
                hit = {**current, "chunk_index": int(payload_item.get("chunk_index", 0)), "text": payload_item["text"]}
                if not bundle_accepts(hit, bundle):
                    continue
                keyword_score = rag_score(path, searchable_text(current, hit["text"]), terms)
                if semantic_score <= 0 and not keyword_score:
                    continue
                role_score = bundle_role_score(hit, bundle)
                context_score = applicability_score(hit, context or {})
                score = semantic_score + 0.03 * min(keyword_score, 10) + role_score + context_score
                if score <= 0:
                    continue
                add_result(
                    result_item(
                        hit,
                        terms,
                        score,
                        "hybrid" if keyword_score else "semantic",
                        semantic_score,
                        keyword_score,
                        role_score,
                        context_score,
                    )
                )
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            scored.clear()
            index.update(search_mode="keyword", fallback_reason="qdrant_unavailable")
            break

    if query_vector is not None and vault_fingerprint(settings.vault_dir) != fingerprint:
        scored.clear()
        index.update(stale=True, search_mode="keyword", fallback_reason="stale_index")
    elif index["stale"] and "fallback_reason" not in index:
        # Stale alone only filters hits per document; the diagnostics stay explicit.
        index.update(search_mode="hybrid", fallback_reason="stale_index")
    if filtered:
        index["semantic_filtered"] = filtered

    for item in keyword_matches(settings, terms, candidate_limit, bundle, context or {}, documents):
        add_result(item)

    candidates = list(scored.values())
    auto_tier_order = {"primary": 0, "supporting": 1, "evidence": 2, "history": 3, "archive": 4}
    ordered = sorted(
        candidates,
        key=lambda item: (
            # Lexical evidence first; semantic-only candidates are kept, never cut by an uncalibrated score.
            item.get("match") == "semantic",
            auto_tier_order.get(str(item.get("retrieval_tier")), 2) if bundle == "auto" else 0,
            -item["score"],
            item["path"],
            item["chunk_index"],
        ),
    )
    if bundle in {"evidence", "experiences"}:
        ordered = prioritize_experience_results(ordered)
    results: list[dict[str, Any]] = []
    seen_documents: set[str] = set()
    for item in ordered:
        document_id = item["document_id"]
        if document_id in seen_documents:
            continue
        seen_documents.add(document_id)
        results.append(item)
        if len(results) >= max_results:
            break

    # Only evidence seeds related context; an unrelated semantic filler must not pull in its own neighbours.
    expanded = expand_bundle_results(results, documents, bundle, terms, context or {}, evidence_items(results))
    if analysis:
        by_id = analysis["by_id"]
        expanded = [{**item, **result_analysis_fields(by_id.get(item["document_id"], {}))} for item in expanded]
    # Safety is judged on everything found; the cap below only limits what is returned.
    answer_state = rag_answer_state(expanded, bundle)
    omitted = 0
    results = expanded
    if len(expanded) > max_results:
        # Evidence, related context, and anything about the same claim outrank unrelated semantic fillers.
        anchors = evidence_items(expanded)
        required = {id(item) for item in expanded if item["match"] == "related" or claim_linked(item, anchors)}
        omitted = max(0, len(required) - max_results)
        results = sorted(expanded, key=lambda item: id(item) not in required)[:max_results]
        if omitted:
            index["results_truncated"] = omitted
    if analysis:
        groups = bundle_groups(bundle, results, analysis)
    else:
        groups = group_results(results, bundle)

    rendered_context = "\n\n".join(
        render_rag_context(item) for item in results
    )
    return {
        "query": query,
        "bundle_type": bundle,
        "embedding_model": model,
        "index": index,
        "results": results,
        "groups": groups,
        "context": rendered_context,
        "answer_state": answer_state,
        **({"analysis": analysis["summary"]} if analysis else {}),
    }


def prioritize_experience_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not results:
        return results
    threshold = float(results[0]["score"]) - 0.25
    relevant = [item for item in results if float(item["score"]) >= threshold]
    prioritized = [results[0]]
    selected = {id(results[0])}
    for outcome in ("failure", "success", "mixed", "unknown", "not_applicable"):
        candidate = next(
            (item for item in relevant if item.get("outcome") == outcome and id(item) not in selected),
            None,
        )
        if candidate:
            prioritized.append(candidate)
            selected.add(id(candidate))
    prioritized.extend(item for item in results if id(item) not in selected)
    return prioritized


def evidence_items(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lexical = [item for item in results if item.get("match") in {"keyword", "hybrid"}]
    return lexical or results[:3]


def claim_linked(item: dict[str, Any], anchors: list[dict[str, Any]]) -> bool:
    """True for an anchor itself, or a result on the same subject or tied to an anchor by explicit relations."""
    def relation_ids(entry: dict[str, Any]) -> set[str]:
        relations = entry.get("relations") if isinstance(entry.get("relations"), dict) else {}
        return {ref for values in relations.values() for ref in reference_ids(values)}

    subjects = {str(anchor.get("subject_id")) for anchor in anchors if anchor.get("subject_id")}
    anchor_ids = {anchor["document_id"] for anchor in anchors}
    return (
        item["document_id"] in anchor_ids
        or str(item.get("subject_id") or "") in subjects
        or item["document_id"] in set().union(*(relation_ids(anchor) for anchor in anchors))
        or bool(anchor_ids & relation_ids(item))
    )


def rag_answer_state(results: list[dict[str, Any]], bundle: str) -> dict[str, str]:
    if not results:
        return {"state": "abstain", "reason": "no_relevant_evidence"}
    evidence = evidence_items(results)
    # A semantic-only result that contradicts or shares a claim with the evidence must still gate it;
    # unrelated semantic fillers must not.
    linked = [item for item in results if claim_linked(item, evidence)]
    if bundle == "conflicts" or any(item.get("conflict_state") == "unresolved" for item in linked):
        return {"state": "review_required", "reason": "unresolved_conflict"}
    if bundle == "current" and any(
        item.get("memory_type") == "canonical"
        and item.get("review_state") == "human_accepted"
        and item.get("temporal_state") == "current"
        for item in evidence
    ):
        return {"state": "supported", "reason": "current_human_accepted_knowledge"}
    if bundle == "auto" and any(
        item.get("memory_type") == "canonical"
        and item.get("review_state") == "human_accepted"
        and item.get("temporal_state") == "current"
        and item.get("metadata_valid", True)
        and item.get("retrieval_tier") not in {"archive", "history"}
        and float((item.get("ranking") or {}).get("applicability") or 0) >= 0
        for item in evidence
    ):
        # Defaulted provenance is normal for legacy human notes; it only disqualifies non-canonical evidence.
        return {"state": "supported", "reason": "current_human_accepted_knowledge"}
    usable = [
        item
        for item in evidence
        if item.get("memory_type") != "transcript"
        and not item.get("provenance_defaulted")
        and float((item.get("ranking") or {}).get("applicability") or 0) >= 0
    ]
    if usable:
        return {"state": "evidence", "reason": "situated_noncanonical_evidence"}
    return {"state": "abstain", "reason": "insufficient_or_mismatched_evidence"}


def qdrant_bundle_filters(bundle: str) -> list[dict[str, Any] | None]:
    def memory_types(*values: str) -> dict[str, Any]:
        return {"must": [{"key": "memory_type", "match": {"any": list(values)}}]}

    if bundle == "current":
        return [memory_types("canonical"), memory_types("candidate"), memory_types("episode", "derived_view")]
    if bundle == "experiences":
        return [memory_types("episode"), memory_types("transcript"), memory_types("candidate", "canonical")]
    if bundle == "evidence":
        return [memory_types("transcript"), memory_types("episode", "candidate")]
    if bundle == "conflicts":
        return [{"must": [{"key": "conflict_state", "match": {"value": "unresolved"}}]}]
    if bundle == "history":
        return [
            {"must": [{"key": "temporal_state", "match": {"any": ["historical", "superseded"]}}]},
            memory_types("episode", "transcript"),
        ]
    return [None]


def bundle_accepts(metadata: dict[str, Any], bundle: str) -> bool:
    memory_type = metadata.get("memory_type")
    temporal_state = metadata.get("temporal_state")
    retrieval_tier = metadata.get("retrieval_tier")
    if bundle == "current":
        return (
            memory_type != "transcript"
            and metadata.get("review_state") not in {"human_rejected", "merged"}
            and retrieval_tier not in {"archive", "history"}
            and temporal_state not in {"historical", "superseded"}
            and metadata.get("metadata_valid", True)
        )
    if bundle == "experiences":
        return memory_type in {"episode", "transcript", "candidate", "canonical"}
    if bundle == "evidence":
        return memory_type in {"episode", "transcript", "candidate"}
    if bundle == "history":
        return temporal_state in {"historical", "superseded", "point_observation"}
    if bundle == "conflicts":
        return metadata.get("conflict_state") == "unresolved"
    return True


def bundle_role_score(metadata: dict[str, Any], bundle: str) -> float:
    memory_type = str(metadata.get("memory_type") or "transcript")
    scores = {
        "auto": {"canonical": 0.18, "episode": 0.12, "candidate": 0.08, "derived_view": 0.04, "transcript": 0.0},
        "current": {"canonical": 0.5, "candidate": 0.22, "episode": 0.08, "derived_view": 0.05},
        "evidence": {"transcript": 0.5, "episode": 0.35, "candidate": 0.15},
        "experiences": {"episode": 0.5, "transcript": 0.12, "candidate": 0.1, "canonical": 0.05},
        "history": {"canonical": 0.25, "episode": 0.2, "transcript": 0.12, "candidate": 0.1},
        "conflicts": {"candidate": 0.4, "episode": 0.3, "canonical": 0.2, "transcript": 0.1},
    }
    score = scores[bundle].get(memory_type, 0.0)
    if metadata.get("retrieval_tier") == "archive":
        score -= 0.25
    if bundle == "history" and metadata.get("temporal_state") in {"historical", "superseded"}:
        score += 0.3
    if bundle == "conflicts" and metadata.get("conflict_state") == "unresolved":
        score += 0.35
    if metadata.get("provenance_defaulted"):
        score -= 0.08
    if not metadata.get("metadata_valid", True):
        score -= 0.5
    if metadata.get("retrieval_state") == "machine_corroborated":
        score += 0.12
    return score


def applicability_score(metadata: dict[str, Any], context: dict[str, Any]) -> float:
    score = 0.0
    requested_projects = {str(item) for item in metadata_list(context.get("projects")) if str(item)}
    document_projects = set(metadata.get("projects") or [])
    if requested_projects and document_projects:
        score += 0.12 if requested_projects & document_projects else -0.08

    applicability = metadata.get("applicability") or {}
    requested_environment = context.get("environment") if isinstance(context.get("environment"), dict) else {}
    actual_environment = applicability.get("environment") if isinstance(applicability.get("environment"), dict) else {}
    for key in ("repository", "operating_system", "python"):
        expected = context.get(key, requested_environment.get(key))
        actual = applicability.get(key, actual_environment.get(key))
        if expected is not None and actual is not None:
            score += 0.04 if str(expected) == str(actual) else -0.08
    return score


def result_label(item: dict[str, Any]) -> str:
    memory_type = item.get("memory_type")
    if memory_type == "canonical":
        return f"CANONICAL - {str(item.get('temporal_state') or 'CURRENT').upper()}"
    if memory_type == "candidate":
        return "PROVISIONAL - CANDIDATE"
    if memory_type == "episode":
        return f"EXPERIENCE - {str(item.get('outcome') or 'UNKNOWN').upper()}"
    if memory_type == "derived_view":
        return "DERIVED VIEW"
    if memory_type == "unknown":
        return "EVIDENCE - UNCLASSIFIED"
    return "EVIDENCE - RAW TRANSCRIPT"


def result_item(
    payload: dict[str, Any],
    terms: list[str],
    score: float,
    match: str,
    semantic_score: float,
    keyword_score: float,
    role_score: float,
    context_score: float,
) -> dict[str, Any]:
    item = {
        key: payload.get(key)
        for key in (
            "document_id",
            "path",
            "chunk_index",
            "title",
            "memory_type",
            "kind",
            "review_state",
            "temporal_state",
            "outcome",
            "retrieval_tier",
            "agent_id",
            "session_id",
            "observed_at",
            "effective_from",
            "effective_to",
            "projects",
            "topics",
            "subject_id",
            "applicability",
            "provenance_mode",
            "provenance_defaulted",
            "evidence_refs",
            "method_refs",
            "derived_from",
            "conflict_state",
            "relations",
            "metadata_valid",
            "error_signatures",
            "subject_aliases",
            "source_refs",
            "source_hashes",
            "repository_sources",
            "provenance_family_ids",
            "support",
            "cluster_ids",
            "retrieval_state",
            "summary_state",
            "repository_validation",
        )
    }
    item.update(
        {
            "snippet": rag_snippet(str(payload.get("text") or ""), terms),
            "score": round(score, 6),
            "match": match,
            "ranking": {
                "semantic": round(semantic_score, 6),
                "keyword": keyword_score,
                "role": round(role_score, 6),
                "applicability": round(context_score, 6),
            },
        }
    )
    item["label"] = result_label(item)
    return item


def group_results(results: list[dict[str, Any]], bundle: str = "auto") -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for result in results:
        key = str(result.get("subject_id") or result["document_id"])
        group = groups.setdefault(
            key,
            {"key": key, "conflict_state": result.get("conflict_state", "none"), "items": []},
        )
        group["items"].append(result)
        if result.get("conflict_state") == "unresolved":
            group["conflict_state"] = "unresolved"
    structured: list[dict[str, Any]] = []
    for group in groups.values():
        items = group["items"]
        if bundle == "current":
            group.update(
                canonical=[item for item in items if item.get("memory_type") == "canonical"],
                pending_deltas=[item for item in items if item.get("memory_type") == "candidate"],
                support=[item for item in items if item.get("memory_type") in {"episode", "derived_view"}],
                warnings=(
                    [{"type": "unresolved_conflict", "document_ids": [item["document_id"] for item in items]}]
                    if group["conflict_state"] == "unresolved"
                    else []
                ),
            )
        elif bundle in {"evidence", "experiences"}:
            group["outcomes"] = {
                outcome: [item for item in items if item.get("outcome") == outcome]
                for outcome in ("success", "failure", "mixed", "unknown", "not_applicable")
            }
        elif bundle == "history":
            group["timeline"] = sorted(
                items,
                key=lambda item: (
                    str(item.get("effective_from") or item.get("observed_at") or ""),
                    item["document_id"],
                ),
            )
        elif bundle == "conflicts":
            group["claims"] = items
            group["winner"] = None
        structured.append(group)
    return structured


def result_analysis_fields(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: document[key]
        for key in (
            "provenance_family_ids",
            "support",
            "cluster_ids",
            "retrieval_state",
            "summary_state",
            "repository_validation",
        )
        if key in document
    }


def render_rag_context(item: dict[str, Any]) -> str:
    safety = {
        key: item.get(key)
        for key in (
            "document_id",
            "memory_type",
            "review_state",
            "temporal_state",
            "outcome",
            "applicability",
            "provenance_mode",
            "provenance_family_ids",
            "conflict_state",
            "retrieval_state",
        )
        if item.get(key) not in (None, "", [], {})
    }
    return (
        f"### {item['label']} | {item['path']}\n"
        f"Metadata: {json.dumps(safety, ensure_ascii=False, sort_keys=True)}\n"
        f"{item['snippet']}"
    )


def expand_bundle_results(
    results: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    bundle: str,
    terms: list[str],
    context: dict[str, Any],
    seeds: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if bundle not in {"current", "history", "conflicts"} or not results:
        return results
    seeds = results if seeds is None else seeds
    selected = {item["document_id"] for item in results}
    seed_ids = {item["document_id"] for item in seeds}
    subjects = {str(item.get("subject_id") or "") for item in seeds} - {""}
    related_ids: set[str] = set()
    for item in seeds:
        relations = item.get("relations") if isinstance(item.get("relations"), dict) else {}
        related_ids.update(reference_ids(relations.get("contradicts")))
        related_ids.update(reference_ids(relations.get("supersedes")))
    if bundle == "conflicts":
        for document in documents:
            relations = document.get("relations") if isinstance(document.get("relations"), dict) else {}
            if seed_ids & set(reference_ids(relations.get("contradicts"))):
                related_ids.add(document["document_id"])
    for document in documents:
        same_subject = document.get("subject_id") and document.get("subject_id") in subjects
        include = document["document_id"] in related_ids
        if bundle == "current":
            include = (include or bool(same_subject)) and bundle_accepts(document, "current")
        elif bundle == "history" and same_subject:
            include = document.get("temporal_state") in {
                "current",
                "historical",
                "superseded",
                "point_observation",
            }
        if not include or document["document_id"] in selected:
            continue
        role_score = bundle_role_score(document, bundle)
        context_score = applicability_score(document, context)
        item = result_item(
            {**document, "chunk_index": 0},
            terms,
            max(0.001, role_score + context_score),
            "related",
            0.0,
            0.0,
            role_score,
            context_score,
        )
        results.append(item)
        selected.add(document["document_id"])
    return results


def vault_documents(settings: Settings, *, skip_unreadable: bool = False) -> list[dict[str, Any]]:
    # Indexing must see every file (a missing document would delete its vectors); only search may skip.
    documents: list[dict[str, Any]] = []
    for rel_path, path in iter_markdown_files(settings.vault_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            if skip_unreadable:
                continue
            raise
        body = markdown_body(text)
        documents.append(
            {
                **document_metadata(rel_path, text),
                "path": rel_path,
                "title": markdown_title(path, body),
                "text": body,
                "document_hash": sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    return documents


def wiki_health(settings: Settings, agent: dict[str, Any]) -> dict[str, Any]:
    require_scope(agent, "vault-rag")
    from .wiki import analyze_documents, health_report

    report = health_report(analyze_documents(vault_documents(settings)))
    semantic_disabled = settings.embedding_provider == "none"
    report["index"] = rag_index_stats(settings) if semantic_disabled or qdrant_collection_exists(settings) else {
        "files": 0,
        "chunks": 0,
        "updated": 0,
        "model": selected_embedding_model(settings),
        "store": "qdrant",
    }
    return report


def analyzed_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        from .wiki import analyze_documents
    except ImportError:
        return documents
    by_id = analyze_documents(documents)["by_id"]
    return [
        {**document, **result_analysis_fields(by_id.get(document["document_id"], {}))}
        for document in documents
    ]


def searchable_text(document: dict[str, Any], chunk: str) -> str:
    metadata_terms = [
        document.get("subject_id"),
        *(document.get("subject_aliases") or []),
        *(document.get("error_signatures") or []),
        *(document.get("topics") or []),
    ]
    prefix = "\n".join(str(value) for value in metadata_terms if value)
    return f"{prefix}\n{chunk}" if prefix else chunk


def keyword_matches(
    settings: Settings,
    terms: list[str],
    limit: int,
    bundle: str = "auto",
    context: dict[str, Any] | None = None,
    documents: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not terms:
        return []
    # Best chunk per document, so one long transcript cannot fill the candidate limit.
    best: dict[str, tuple[float, str, int, str, int, float, float]] = {}
    # ponytail: O(n) vault scan is fine for a personal vault; move this into Qdrant payload filters if it becomes slow.
    if documents is None:
        documents = analyzed_documents(vault_documents(settings))
    by_path = {document["path"]: document for document in documents}
    for document in documents:
        rel_path = document["path"]
        if not bundle_accepts(document, bundle):
            continue
        role_score = bundle_role_score(document, bundle)
        context_score = applicability_score(document, context or {})
        for chunk_index, chunk in enumerate(chunk_text(document["text"])):
            score = rag_score(rel_path, searchable_text(document, chunk), terms)
            if not score:
                continue
            total = 1.0 + 0.03 * min(score, 10) + role_score + context_score
            key = str(document["document_id"])
            if key not in best or (-total, rel_path, chunk_index) < (-best[key][0], best[key][1], best[key][2]):
                best[key] = (total, rel_path, chunk_index, chunk, score, role_score, context_score)
    hits = []
    for total, rel_path, chunk_index, chunk, score, role_score, context_score in sorted(
        best.values(), key=lambda hit: (-hit[0], hit[1], hit[2])
    )[:limit]:
        hits.append(
            result_item(
                {**by_path[rel_path], "chunk_index": chunk_index, "text": chunk},
                terms,
                total,
                "keyword",
                0.0,
                score,
                role_score,
                context_score,
            )
        )
    return hits


def index_vault(settings: Settings) -> dict[str, int | str | bool]:
    if settings.embedding_provider == "none":
        # Explicit no-op: existing vectors and index metadata are left untouched.
        return disabled_index_stats()
    if not RAG_INDEX_LOCK.acquire(blocking=False):
        raise RuntimeError("RAG indexing is already in progress")
    try:
        # The CLI and server share a DB but not the in-process lock.
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        with settings.db_path.with_suffix(settings.db_path.suffix + ".index.lock").open("a+b") as lock:
            try:
                if os.name == "nt":
                    import msvcrt

                    if lock.tell() == 0:
                        lock.write(b"\0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("RAG indexing is already in progress in another process") from exc
            return _index_vault(settings)
    finally:
        RAG_INDEX_LOCK.release()


def _index_vault(settings: Settings) -> dict[str, int | str | bool]:
    settings.vault_dir.mkdir(parents=True, exist_ok=True)
    provider = settings.embedding_provider
    model = selected_embedding_model(settings)
    fingerprint = vault_fingerprint(settings.vault_dir)
    try:
        documents = analyzed_documents(vault_documents(settings))
    except OSError as exc:
        raise RuntimeError("Vault file could not be read; RAG index left unchanged") from exc
    files = len(documents)
    vector_size = embedding_size(settings)
    indexed_at = utc_now()
    chunks = [
        (document, document["path"], chunk_index, chunk)
        for document in documents
        for chunk_index, chunk in enumerate(chunk_text(document["text"]))
    ]
    specs: list[dict[str, Any]] = []
    for document, rel_path, chunk_index, chunk in chunks:
        embedding_input = searchable_text(document, chunk)
        payload = {
            **document,
            "path": rel_path,
            "chunk_index": chunk_index,
            "content_hash": sha256(chunk.encode("utf-8")).hexdigest(),
            "text": chunk,
            "embedding_input_hash": sha256(embedding_input.encode("utf-8")).hexdigest(),
            "embedding_provider": provider,
            "embedding_model": model,
            "embedding_dimension": vector_size,
        }
        payload["payload_hash"] = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        payload["updated_at"] = indexed_at
        specs.append({"id": qdrant_point_id(rel_path, chunk_index), "input": embedding_input, "payload": payload})

    meta = rag_index_meta(settings)
    collection_exists = bool(
        qdrant_json(settings, "GET", f"/collections/{settings.qdrant_collection}/exists")
        .get("result", {})
        .get("exists")
    )
    compatible = collection_exists and (
        meta.get("provider") == provider
        and meta.get("model") == model
        and int_meta(meta.get("dimension"), -1) == vector_size
    )
    rebuilding = meta.get("rebuild_state") == "building"
    if compatible:
        existing = qdrant_payload_hashes(settings)
    else:
        rebuilding = True
        recreate_qdrant_collection(settings, vector_size)
        existing = {}

    changed = [
        spec
        for spec in specs
        if existing.get(spec["id"], {}).get("embedding_input_hash") != spec["payload"]["embedding_input_hash"]
    ]
    changed_ids = {spec["id"] for spec in changed}
    payload_only = [
        spec
        for spec in specs
        if spec["id"] not in changed_ids
        and existing[spec["id"]].get("payload_hash") != spec["payload"]["payload_hash"]
    ]
    # Payload/schema upgrades keep the stored vectors but must not look current until finished,
    # so the schema stays old and the fingerprint is cleared until the final meta write.
    rebuilding = rebuilding or meta.get("schema") != RAG_INDEX_SCHEMA or bool(payload_only)
    set_rag_index_meta(
        settings,
        {
            "provider": provider,
            "model": model,
            "dimension": str(vector_size),
            "chunks": str(len(existing)),
            "fingerprint": "",
            "rebuild_state": "building" if rebuilding else "ready",
        },
    )
    stale_ids = sorted(set(existing) - {spec["id"] for spec in specs})
    document_hashes = {document["path"]: document["document_hash"] for document in documents}
    changed_by_path: dict[str, list[dict[str, Any]]] = {}
    payload_by_path: dict[str, list[dict[str, Any]]] = {}
    stale_by_path: dict[str, list[str]] = {}
    for spec in changed:
        changed_by_path.setdefault(spec["payload"]["path"], []).append(spec)
    for spec in payload_only:
        payload_by_path.setdefault(spec["payload"]["path"], []).append(spec)
    for point_id in stale_ids:
        stale_by_path.setdefault(existing[point_id].get("path", ""), []).append(point_id)

    affected_paths = set(changed_by_path) | set(payload_by_path) | set(stale_by_path)
    for rel_path in sorted(affected_paths, key=lambda path: (path not in changed_by_path, path)):
        file_changed = changed_by_path.get(rel_path, [])
        vectors = embed_documents(settings, [spec["input"] for spec in file_changed]) if file_changed else []

        if rel_path in document_hashes:
            source = settings.vault_dir / rel_path
            try:
                current_text = source.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise RuntimeError("Vault changed during RAG indexing; retry with a stable snapshot") from exc
            if sha256(current_text.encode("utf-8")).hexdigest() != document_hashes[rel_path]:
                raise RuntimeError("Vault changed during RAG indexing; retry with a stable snapshot")

        changed_points = [
            {"id": spec["id"], "vector": vector, "payload": spec["payload"]}
            for spec, vector in zip(file_changed, vectors, strict=True)
        ]
        for index in range(0, len(changed_points), 64):
            qdrant_upsert(settings, changed_points[index : index + 64])
        for spec in payload_by_path.get(rel_path, []):
            qdrant_overwrite_payload(settings, spec["id"], spec["payload"])
        for index in range(0, len(stale_by_path.get(rel_path, [])), 256):
            qdrant_delete(settings, stale_by_path[rel_path][index : index + 256])
        set_rag_index_meta(
            settings,
            {
                "files": str(files),
                "chunks": str(qdrant_count(settings)),
                "rebuild_state": "building" if rebuilding else "ready",
            },
        )

    actual_chunks = qdrant_count(settings)
    if actual_chunks != len(specs):
        raise RuntimeError(f"RAG index count mismatch: expected {len(specs)}, got {actual_chunks}")
    if vault_fingerprint(settings.vault_dir) != fingerprint:
        raise RuntimeError("Vault changed during RAG indexing; retry with a stable snapshot")

    set_rag_index_meta(
        settings,
        {
            "fingerprint": fingerprint,
            "schema": RAG_INDEX_SCHEMA,
            "provider": provider,
            "model": model,
            "dimension": str(vector_size),
            "files": str(files),
            "chunks": str(len(specs)),
            "rebuild_state": "ready",
        },
    )
    return {
        "files": files,
        "chunks": len(specs),
        "updated": len(changed),
        "payload_updated": len(payload_only),
        "deleted": len(stale_ids),
        "provider": provider,
        "model": model,
        "dimension": vector_size,
        "store": "qdrant",
        "stale": False,
    }


def disabled_index_stats() -> dict[str, int | str | bool]:
    return {
        "files": 0,
        "chunks": 0,
        "updated": 0,
        "payload_updated": 0,
        "deleted": 0,
        "provider": "none",
        "model": "none",
        "dimension": 0,
        "store": "none",
        "stale": False,
        "semantic": "disabled",
    }


def rag_index_stats(settings: Settings) -> dict[str, int | str | bool]:
    if settings.embedding_provider == "none":
        return disabled_index_stats()
    meta = rag_index_meta(settings)
    model = selected_embedding_model(settings)
    return {
        "files": int_meta(meta.get("files")),
        "chunks": qdrant_count(settings),
        "updated": 0,
        "provider": meta.get("provider") or settings.embedding_provider,
        "model": model,
        "dimension": int_meta(meta.get("dimension"), embedding_size(settings)),
        "store": "qdrant",
        "stale": meta.get("fingerprint") != vault_fingerprint(settings.vault_dir),
    }


def rag_readiness(settings: Settings) -> dict[str, Any]:
    init_db(settings.db_path)
    if settings.embedding_provider == "none":
        return {
            "status": "ok",
            "db": "ok",
            "qdrant": "disabled",
            "semantic": "disabled",
            "rag_indexed": False,
            "chunks": 0,
            "provider": "none",
        }
    try:
        exists_result = qdrant_json(settings, "GET", f"/collections/{settings.qdrant_collection}/exists")
        exists = bool(exists_result.get("result", {}).get("exists"))
        if exists:
            count_result = qdrant_json(
                settings,
                "POST",
                f"/collections/{settings.qdrant_collection}/points/count",
                {"exact": True},
            )
            chunks = int(count_result.get("result", {}).get("count") or 0)
        else:
            chunks = 0
        model = selected_embedding_model(settings)
        dimension = embedding_size(settings)
    except (RuntimeError, ValueError) as exc:
        return {"status": "error", "db": "ok", "qdrant": "error", "error": str(exc)}
    meta = rag_index_meta(settings)
    rebuild_state = meta.get("rebuild_state")
    expected_chunks = int_meta(meta.get("chunks"), -1)
    if exists and (
        rebuild_state != "ready"
        or meta.get("schema") != RAG_INDEX_SCHEMA
        or meta.get("provider") != settings.embedding_provider
        or meta.get("model") != model
        or int_meta(meta.get("dimension"), -1) != dimension
        or expected_chunks != chunks
    ):
        reason = (
            f"state={rebuild_state or 'missing'}, schema={meta.get('schema') or 'missing'}, "
            f"provider={meta.get('provider') or 'missing'}, model={meta.get('model') or 'missing'}, "
            f"dimension={meta.get('dimension') or 'missing'}, expected_chunks={expected_chunks}, "
            f"actual_chunks={chunks}"
        )
        return {
            "status": "error",
            "db": "ok",
            "qdrant": "ok",
            "rag_indexed": False,
            "chunks": chunks,
            "error": f"RAG index is not current: {reason}",
        }
    return {
        "status": "ok",
        "db": "ok",
        "qdrant": "ok",
        "rag_indexed": chunks > 0,
        "chunks": chunks,
        "provider": meta.get("provider") or settings.embedding_provider,
        "model": meta.get("model") or model,
        "dimension": int_meta(meta.get("dimension"), dimension),
    }


def embedding_size(settings: Settings) -> int:
    model = selected_embedding_model(settings)
    return CLOUDFLARE_EMBEDDING_DIMENSIONS if model == CLOUDFLARE_EMBEDDING_MODEL else HASH_EMBEDDING_DIMENSIONS


def qdrant_headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.qdrant_api_key:
        headers["api-key"] = settings.qdrant_api_key
    return headers


def qdrant_json(
    settings: Settings,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = QDRANT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if settings.embedding_provider == "none":
        raise RuntimeError("Qdrant is disabled when EMBEDDING_PROVIDER=none")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{settings.qdrant_url}{path}",
        data=data,
        headers=qdrant_headers(settings),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Qdrant {method} {path} failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Qdrant {method} {path} failed: {exc.reason}") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"Qdrant {method} {path} failed: {exc!r}") from exc
    try:
        return json.loads(payload.decode("utf-8")) if payload else {}
    except ValueError as exc:
        raise RuntimeError(f"Qdrant {method} {path} returned an invalid response") from exc


def qdrant_collection_exists(settings: Settings) -> bool:
    try:
        result = qdrant_json(settings, "GET", f"/collections/{settings.qdrant_collection}/exists")
    except RuntimeError:
        return False
    return bool(result.get("result", {}).get("exists"))


def qdrant_index_current(settings: Settings, *, allow_stale: bool = False) -> bool:
    if settings.embedding_provider == "none" or not qdrant_collection_exists(settings):
        return False
    meta = rag_index_meta(settings)
    if int_meta(meta.get("chunks"), -1) != qdrant_count(settings):
        return False
    return (
        meta.get("rebuild_state") == "ready"
        and meta.get("schema") == RAG_INDEX_SCHEMA
        and meta.get("provider") == settings.embedding_provider
        and meta.get("model") == selected_embedding_model(settings)
        and int_meta(meta.get("dimension"), -1) == embedding_size(settings)
        and (allow_stale or meta.get("fingerprint") == vault_fingerprint(settings.vault_dir))
    )


def recreate_qdrant_collection(settings: Settings, vector_size: int) -> None:
    if qdrant_collection_exists(settings):
        qdrant_json(settings, "DELETE", f"/collections/{settings.qdrant_collection}")
    qdrant_json(
        settings,
        "PUT",
        f"/collections/{settings.qdrant_collection}",
        {"vectors": {"size": vector_size, "distance": "Cosine"}},
    )


def qdrant_upsert(settings: Settings, points: list[dict[str, Any]]) -> None:
    qdrant_json(
        settings,
        "PUT",
        f"/collections/{settings.qdrant_collection}/points?wait=true",
        {"points": points},
    )


def qdrant_payload_hashes(settings: Settings) -> dict[str, dict[str, str]]:
    points: dict[str, dict[str, str]] = {}
    offset: str | int | None = None
    while True:
        body: dict[str, Any] = {
            "limit": 256,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        result = qdrant_json(
            settings,
            "POST",
            f"/collections/{settings.qdrant_collection}/points/scroll",
            body,
        ).get("result", {})
        for point in result.get("points", []):
            payload = point.get("payload") or {}
            embedding_input_hash = str(payload.get("embedding_input_hash") or "")
            if not embedding_input_hash:
                embedding_input_hash = sha256(
                    searchable_text(payload, str(payload.get("text") or "")).encode("utf-8")
                ).hexdigest()
            payload_hash = str(payload.get("payload_hash") or "")
            if not payload_hash:
                normalized = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"payload_hash", "updated_at"}
                }
                normalized["embedding_input_hash"] = embedding_input_hash
                payload_hash = sha256(
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
            points[str(point["id"])] = {
                "path": str(payload.get("path") or ""),
                "embedding_input_hash": embedding_input_hash,
                "payload_hash": payload_hash,
            }
        next_offset = result.get("next_page_offset")
        if next_offset is None:
            return points
        if next_offset == offset:
            raise RuntimeError("Qdrant scroll returned a repeated offset")
        offset = next_offset


def qdrant_overwrite_payload(settings: Settings, point_id: str, payload: dict[str, Any]) -> None:
    qdrant_json(
        settings,
        "PUT",
        f"/collections/{settings.qdrant_collection}/points/payload?wait=true",
        {"payload": payload, "points": [point_id]},
    )


def qdrant_delete(settings: Settings, point_ids: list[str]) -> None:
    qdrant_json(
        settings,
        "POST",
        f"/collections/{settings.qdrant_collection}/points/delete?wait=true",
        {"points": point_ids},
    )


def qdrant_count(settings: Settings) -> int:
    if not qdrant_collection_exists(settings):
        return 0
    result = qdrant_json(
        settings,
        "POST",
        f"/collections/{settings.qdrant_collection}/points/count",
        {"exact": True},
    )
    return int(result.get("result", {}).get("count") or 0)


def qdrant_point_id(path: str, chunk_index: int) -> str:
    digest = sha256(f"{path}:{chunk_index}".encode("utf-8")).hexdigest()[:32]
    return str(uuid.UUID(hex=digest))


def qdrant_compaction_neighbors(
    settings: Settings,
    source_documents: list[dict[str, Any]],
    limit: int = 6,
) -> dict[str, Any]:
    """Find semantic neighbors from stored vectors without embedding new text."""
    if not source_documents:
        return {"status": "empty", "edges": []}
    if settings.embedding_provider == "none":
        return {"status": "unavailable", "reason": "semantic_disabled", "edges": []}
    if not settings.db_path.is_file():
        return {"status": "unavailable", "reason": "missing_index_metadata", "edges": []}
    try:
        if not qdrant_index_current(settings):
            return {"status": "unavailable", "reason": "stale_or_missing_index", "edges": []}
    except (RuntimeError, sqlite3.Error, ValueError) as exc:
        return {"status": "unavailable", "reason": str(exc), "edges": []}

    queries = [
        (document["path"], qdrant_point_id(document["path"], chunk_index))
        for document in source_documents
        for chunk_index, _chunk in enumerate(chunk_text(str(document.get("text") or "")))
    ]
    if not queries:
        return {"status": "empty", "edges": []}

    edges: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for offset in range(0, len(queries), 64):
            batch = queries[offset : offset + 64]
            response = qdrant_json(
                settings,
                "POST",
                f"/collections/{settings.qdrant_collection}/points/query/batch",
                {
                    "searches": [
                        {
                            "query": point_id,
                            "filter": {"must_not": [{"key": "path", "match": {"value": source_path}}]},
                            "limit": max(1, limit),
                            "with_payload": True,
                        }
                        for source_path, point_id in batch
                    ]
                },
            )
            results = response.get("result")
            if not isinstance(results, list) or len(results) != len(batch):
                raise RuntimeError("Qdrant batch query returned an invalid response")
            for (source_path, _point_id), result in zip(batch, results, strict=True):
                for point in result.get("points", []):
                    payload = point.get("payload") or {}
                    target_path = str(payload.get("path") or "")
                    if not target_path or target_path == source_path:
                        continue
                    key = (source_path, str(payload.get("document_id") or target_path))
                    edge = {
                        "source_path": source_path,
                        "target_document_id": key[1],
                        "target_path": target_path,
                        "target_memory_type": str(payload.get("memory_type") or "unknown"),
                        "score": round(float(point.get("score") or 0.0), 6),
                    }
                    if key not in edges or edge["score"] > edges[key]["score"]:
                        edges[key] = edge
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "reason": str(exc), "edges": []}

    return {
        "status": "current",
        "queries": len(queries),
        "edges": sorted(
            edges.values(),
            key=lambda edge: (edge["source_path"], -edge["score"], edge["target_document_id"]),
        ),
    }


def iter_markdown_files(vault_dir: Path) -> list[tuple[str, Path]]:
    base = vault_dir.resolve()
    if not base.exists():
        return []
    files: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for path in base.rglob("*.md"):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            rel = resolved.relative_to(base)
        except ValueError:
            continue
        if rel.as_posix() in seen or any(part in RAG_EXCLUDED_DIRS for part in rel.parts):
            continue
        seen.add(rel.as_posix())
        files.append((rel.as_posix(), resolved))
    return sorted(files)


def vault_fingerprint(vault_dir: Path) -> str:
    digest = sha256()
    for rel_path, path in iter_markdown_files(vault_dir):
        stat = path.stat()
        digest.update(rel_path.encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def rag_index_meta(settings: Settings) -> dict[str, str]:
    with connect(settings.db_path) as con:
        rows = con.execute("SELECT key, value FROM rag_index_meta").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_rag_index_meta(settings: Settings, values: dict[str, str]) -> None:
    with connect(settings.db_path) as con:
        con.executemany(
            """
            INSERT INTO rag_index_meta (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            values.items(),
        )


def int_meta(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def chunk_text(text: str) -> list[str]:
    # pvg-src provenance markers stay in the file but never reach the index, scoring, snippets or search results.
    text = _SRC_RE.sub("", text).strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + RAG_CHUNK_CHARS)
        if end < len(text):
            split = text.rfind("\n\n", start, end)
            if split > start + RAG_CHUNK_CHARS // 2:
                end = split + 2
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - RAG_CHUNK_OVERLAP)
    return chunks


def selected_embedding_model(settings: Settings) -> str:
    provider = settings.embedding_provider
    if provider == "cloudflare":
        if settings.embedding_model != CLOUDFLARE_EMBEDDING_MODEL:
            raise ValueError(f"EMBEDDING_MODEL must be {CLOUDFLARE_EMBEDDING_MODEL}")
        return settings.embedding_model
    if provider == "hash":
        return HASH_EMBEDDING_MODEL
    if provider == "none":
        # Fail closed: a caller that forgot the keyword-only guard must not fall through to Qdrant.
        raise ValueError("semantic search is disabled (EMBEDDING_PROVIDER=none)")
    raise ValueError("EMBEDDING_PROVIDER must be cloudflare, hash, or none")


def embed_documents(settings: Settings, texts: list[str]) -> list[list[float]]:
    model = selected_embedding_model(settings)
    if model == HASH_EMBEDDING_MODEL:
        return [hash_embedding(text) for text in texts]
    return cloudflare_embeddings(settings, texts, query=False)


def embed_query(settings: Settings, text: str) -> tuple[list[float], str]:
    model = selected_embedding_model(settings)
    if model == HASH_EMBEDDING_MODEL:
        return hash_embedding(text), model
    return cloudflare_embeddings(settings, [text], query=True)[0], model


def cloudflare_embeddings(settings: Settings, texts: list[str], *, query: bool) -> list[list[float]]:
    selected_embedding_model(settings)
    if not texts:
        return []
    blocked_until = cloudflare_embedding_blocked_until(settings)
    if blocked_until:
        raise EmbeddingLimitError(f"Cloudflare embeddings are blocked until {blocked_until.isoformat()}")
    if not settings.cloudflare_account_id or not settings.cloudflare_api_token:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required for embeddings")
    if settings.embedding_batch_size < 1:
        raise ValueError("EMBEDDING_BATCH_SIZE must be greater than zero")

    vectors: list[list[float]] = []
    # Interactive queries get one short attempt: no retry or sleep, so a failure falls back to keyword
    # search. Document embedding keeps the full indexing retry policy.
    request_options = {"timeout": CLOUDFLARE_QUERY_TIMEOUT_SECONDS, "max_retries": 0} if query else {}
    for start in range(0, len(texts), settings.embedding_batch_size):
        batch = texts[start : start + settings.embedding_batch_size]
        body: dict[str, Any] = {"queries" if query else "documents": batch}
        if query:
            body["instruction"] = CLOUDFLARE_EMBEDDING_INSTRUCTION
        vectors.extend(cloudflare_embedding_request(settings, body, len(batch), **request_options))
    return vectors


def cloudflare_embedding_request(
    settings: Settings,
    body: dict[str, Any],
    expected_count: int,
    *,
    timeout: float = CLOUDFLARE_EMBEDDING_TIMEOUT_SECONDS,
    max_retries: int = CLOUDFLARE_EMBEDDING_MAX_RETRIES,
) -> list[list[float]]:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        (
            f"https://api.cloudflare.com/client/v4/accounts/{settings.cloudflare_account_id}"
            f"/ai/run/{settings.embedding_model}"
        ),
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.cloudflare_api_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    response_body = b""
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read()
            break
        except urllib.error.HTTPError as exc:
            error_body = exc.read()
            error_codes = cloudflare_error_codes(error_body)
            if exc.code == 429 and 3040 not in error_codes:
                if 3036 in error_codes:
                    now = datetime.now(timezone.utc)
                    blocked_until = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        seconds = max(1.0, float(retry_after)) if retry_after else 60.0
                    except ValueError:
                        seconds = 60.0
                    blocked_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                set_rag_index_meta(settings, {"embedding_blocked_until": blocked_until.isoformat()})
                detail = cloudflare_error_detail(error_body)
                raise EmbeddingLimitError(
                    f"Cloudflare embedding limit exceeded; blocked until {blocked_until.isoformat()}{detail}"
                ) from exc
            if exc.code in CLOUDFLARE_RETRYABLE_STATUS and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = min(30.0, max(0.0, float(retry_after))) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                time.sleep(delay)
                continue
            if exc.code == 429 and 3040 in error_codes and attempt < max_retries:
                time.sleep(2**attempt)
                continue
            detail = cloudflare_error_detail(error_body)
            raise RuntimeError(f"Cloudflare embeddings failed: HTTP {exc.code}{detail}") from exc
        except (OSError, http.client.HTTPException) as exc:
            if attempt < max_retries:
                time.sleep(2**attempt)
                continue
            reason = getattr(exc, "reason", str(exc))
            raise RuntimeError(f"Cloudflare embeddings failed: {reason}") from exc

    try:
        response = json.loads(response_body.decode("utf-8"))
        if response.get("success") is False:
            raise ValueError(cloudflare_error_detail(response_body).removeprefix(": ") or "request failed")
        result = response.get("result", response)
        data = result["data"]
        if not isinstance(data, list) or len(data) != expected_count:
            raise ValueError("embedding count does not match input count")
        if any(not isinstance(vector, list) for vector in data):
            raise ValueError("embedding data must be a list of vectors")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for vector in data
            for value in vector
        ):
            raise ValueError("embedding contains a non-numeric value")
        vectors = [[float(value) for value in vector] for vector in data]
        if any(len(vector) != CLOUDFLARE_EMBEDDING_DIMENSIONS for vector in vectors):
            raise ValueError(f"embedding dimension must be {CLOUDFLARE_EMBEDDING_DIMENSIONS}")
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise ValueError("embedding contains a non-finite value")
        return vectors
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cloudflare embeddings returned an invalid response: {exc}") from exc


def cloudflare_error_detail(payload: bytes) -> str:
    try:
        errors = json.loads(payload.decode("utf-8")).get("errors", [])
        messages = [str(error.get("message")) for error in errors if error.get("message")]
        return f": {'; '.join(messages)}" if messages else ""
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return ""


def cloudflare_error_codes(payload: bytes) -> set[int]:
    try:
        errors = json.loads(payload.decode("utf-8")).get("errors", [])
        return {int(error["code"]) for error in errors if "code" in error}
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def cloudflare_embedding_blocked_timestamp(settings: Settings) -> datetime | None:
    init_db(settings.db_path)
    value = rag_index_meta(settings).get("embedding_blocked_until")
    if not value:
        return None
    try:
        blocked_until = datetime.fromisoformat(value)
    except ValueError:
        return None
    if blocked_until.tzinfo is None:
        blocked_until = blocked_until.replace(tzinfo=timezone.utc)
    return blocked_until


def cloudflare_embedding_blocked_until(settings: Settings) -> datetime | None:
    blocked_until = cloudflare_embedding_blocked_timestamp(settings)
    return blocked_until if blocked_until and blocked_until > datetime.now(timezone.utc) else None


def retry_due_embeddings(settings: Settings, now: datetime | None = None) -> bool:
    if settings.embedding_provider == "none":
        # Keep any stored limit marker so switching back to a provider resumes it.
        return False
    blocked_until = cloudflare_embedding_blocked_timestamp(settings)
    retry_at = blocked_until + timedelta(seconds=EMBEDDING_RETRY_GRACE_SECONDS) if blocked_until else None
    if not retry_at or retry_at > (now or datetime.now(timezone.utc)):
        return False
    if not qdrant_index_current(settings):
        index_vault(settings)
    set_rag_index_meta(settings, {"embedding_blocked_until": ""})
    return True


def hash_embedding(text: str) -> list[float]:
    vector = [0.0] * HASH_EMBEDDING_DIMENSIONS
    for term in text_terms(text):
        digest = sha256(term.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % HASH_EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] % 2 else -1.0
        vector[index] += sign
    return normalize_vector(vector)


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


def text_terms(text: str) -> list[str]:
    return [term.lower() for term in TOKEN_RE.findall(text) if len(term) > 1]


def keyword_terms(text: str) -> list[str]:
    terms = text_terms(text)
    # A stopword-only query is still an exact query.
    return [term for term in terms if term not in QUERY_STOPWORDS] or terms


def term_in(term: str, text: str) -> int:
    """Count occurrences; short Latin terms must be whole words ('ai' is not in 'maintain')."""
    if len(term) <= 3 and term.isascii():
        return len(re.findall(rf"(?<![0-9a-z]){re.escape(term)}(?![0-9a-z])", text))
    return text.count(term)


def rag_score(path: str, text: str, terms: list[str]) -> int:
    haystack = text.lower()
    path_text = path.lower()
    unique_terms = list(dict.fromkeys(terms))
    matched = sum(bool(term_in(term, haystack) or term_in(term, path_text)) for term in unique_terms)
    minimum = 2 if len(unique_terms) >= 3 else 1
    if matched < minimum:
        return 0
    return sum(term_in(term, haystack) + 3 * term_in(term, path_text) for term in unique_terms)


def rag_snippet(text: str, terms: list[str], radius: int = 260) -> str:
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - radius) if positions else 0
    snippet = text[start : start + radius * 2].replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", snippet).strip()


def markdown_title(path: Path, text: str) -> str:
    for line in text.split("\n"):
        if line.startswith("# "):
            return one_line(line.removeprefix("# "), path.stem)
    return path.stem


def parse_conversation(text: str) -> tuple[dict[str, Any], str, list[dict[str, Any]], str]:
    """Read only losslessly recognizable gateway transcripts; never guess boundaries."""
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate metadata key")
            result[key] = value
        return result

    try:
        front, body = text.removeprefix("---\n").split("\n---\n\n", 1)
        if not text.startswith("---\n"):
            raise ValueError("missing frontmatter")
        keys = []
        for line in front.split("\n"):
            if line:
                key, _value = line.split(": ", 1)
                if not re.fullmatch(r"[a-z_]+", key) or key in keys:
                    raise ValueError("ambiguous frontmatter")
                keys.append(key)
        metadata = parse_frontmatter(text)
        count = metadata["event_count"]
        if type(count) is not int or count < 1 or metadata.get("_pvg_metadata_error"):
            raise ValueError("invalid event count")
        heading = re.match(r"# Conversation: [^\n]+\n\n## Messages\n\n", body)
        if not heading:
            raise ValueError("invalid conversation heading")
        offset = len(text) - len(body) + heading.end()
        prefix = text[:offset]
        messages = []
        event_header = re.compile(r"### ([^\n]+)\n\n<!-- pvg-event ([^\n]+) -->\n\n")
        context_marker = "\n\n## Context Snapshot\n\n```json\n"
        delegation = "**Agent delegation (not a user statement)**\n\n"
        result_marker = "\n\n**Result**\n\n"
        for index in range(count):
            start = offset
            match = event_header.match(text, offset)
            if not match:
                raise ValueError("invalid event header")
            event = json.loads(match[2], object_pairs_hook=unique_object)
            if not isinstance(event, dict) or not event.get("event_id") or not event.get("timestamp"):
                raise ValueError("missing event identity")
            parse_time(event["timestamp"])
            role = match[1].split(": ", 1)[0]
            framed = "content_chars" in event
            offset = match.end()
            if framed:
                content_chars = event.pop("content_chars")
                request_chars = event.pop("request_chars")
                if any(type(size) is not int or size < 0 for size in (content_chars, request_chars)):
                    raise ValueError("invalid event lengths")
                if request_chars:
                    if not text.startswith(delegation, offset):
                        raise ValueError("invalid delegation")
                    offset += len(delegation)
                    event["request"] = text[offset:offset + request_chars]
                    offset += request_chars
                    if not text.startswith(result_marker, offset):
                        raise ValueError("invalid delegation result")
                    offset += len(result_marker)
                event["content"] = text[offset:offset + content_chars]
                offset += content_chars
            else:
                event["role"] = role
                boundary = text.find(context_marker, offset)
                if boundary < 0:
                    raise ValueError("missing context snapshot")
                following = event_header.search(text, offset, boundary)
                end = following.start() - 2 if following else boundary
                content = text[offset:end]
                if "<!-- pvg-event" in content or "## Context Snapshot" in content or "## Messages" in content:
                    raise ValueError("ambiguous legacy event markers")
                if content.startswith(delegation):
                    raise ValueError("legacy delegation cannot be distinguished from a quoted example")
                event["content"] = content
                offset = end
            if render_messages([event], framed=framed) != text[start:offset]:
                raise ValueError("event does not round-trip")
            messages.append(event)
            if index < count - 1:
                if not text.startswith("\n\n", offset):
                    raise ValueError("missing event separator")
                offset += 2
        suffix = text[offset:]
        if not suffix.startswith(context_marker) or not suffix.endswith("\n```\n"):
            raise ValueError("invalid context boundary")
        context = json.loads(suffix[len(context_marker):-5], object_pairs_hook=unique_object)
        if not isinstance(context, dict):
            raise ValueError("invalid context snapshot")
        if len({message["event_id"] for message in messages}) != count:
            raise ValueError("duplicate persisted event IDs")
        return metadata, prefix, messages, suffix
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise ConversationConflictError("existing conversation Markdown is ambiguous or unparseable") from exc


def save_conversation(
    settings: Settings,
    agent: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, str]:
    require_scope(agent, "conversation-log")
    return _save_conversation(settings, agent, payload)


def _save_conversation(
    settings: Settings,
    agent: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, str]:
    # ponytail: serialize captures across workers using the existing DB transaction;
    # use per-session locks if capture throughput makes this a bottleneck.
    with connect(settings.db_path) as con:
        con.execute("PRAGMA busy_timeout = 30000")
        con.execute("BEGIN IMMEDIATE")
        return _save_conversation_locked(settings, agent, payload)


def _save_conversation_locked(
    settings: Settings,
    agent: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, str]:
    if len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")) > MAX_CONVERSATION_BYTES:
        raise ValueError("conversation payload is too large")
    mode = payload.get("mode", "snapshot")
    if mode not in {"snapshot", "merge"}:
        raise ValueError("conversation mode must be snapshot or merge")
    incoming = payload.get("messages") or []
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 500:
        raise ValueError("conversation requires 1-500 messages per request")

    parse_time(payload.get("started_at"))
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    title = one_line(payload.get("title") or session_id, "conversation")
    project = payload.get("project")
    topics = clean_list(payload.get("tags"))
    extra_metadata = "".join(
        f"{key}: {yaml_scalar(payload[key])}\n"
        for key in (
            "kind",
            "subject_id",
            "subject_aliases",
            "error_signatures",
            "applicability",
            "provenance",
            "relations",
            "source_refs",
            "source_hashes",
            "repository_sources",
        )
        if payload.get(key) not in (None, "", [], {})
    )
    messages = incoming if mode == "merge" else unique_messages(incoming)
    if any(not isinstance(message, dict) or not message.get("event_id") or not message.get("timestamp") for message in messages):
        raise ValueError("every conversation message requires event_id and timestamp")
    try:
        event_times = [parse_time(message["timestamp"]) for message in messages]
    except (TypeError, ValueError) as exc:
        raise ValueError("message timestamp must be ISO 8601") from exc
    session_key = sha256(f"{agent['agent_id']}\0{session_id}".encode("utf-8")).hexdigest()[:20]
    existing: dict[str, tuple[Path, str, dict[str, Any], str, list[dict[str, Any]], str]] = {}
    events: dict[str, str] = {}
    if mode == "merge":
        paths = (
            path
            for date in sorted({event_at.strftime("%Y/%m/%d") for event_at in event_times})
            for path in sorted((settings.vault_dir / "30_Conversations/raw" / date).glob(f"*-{agent['agent_id']}-{session_key}.md"))
        )
        for path in paths:
            target_path(settings, path.relative_to(settings.vault_dir).as_posix(), agent["allowed_roots"])
            try:
                text = path.read_bytes().decode("utf-8")
            except UnicodeError as exc:
                raise ConversationConflictError("existing conversation is not UTF-8") from exc
            metadata, prefix, previous, suffix = parse_conversation(text)
            date = metadata.get("segment_date")
            if (
                not isinstance(date, str) or date in existing
                or metadata.get("session_id") != session_id
                or metadata.get("agent_id") != agent["agent_id"]
                or metadata.get("conversation_id") != f"conv_{session_key}_{date.replace('-', '')}"
                or path.parent.relative_to(settings.vault_dir / "30_Conversations/raw").as_posix() != date.replace("-", "/")
                or any(parse_time(message["timestamp"]).strftime("%Y-%m-%d") != date for message in previous)
            ):
                raise ConversationConflictError("existing conversation identity or local date is ambiguous")
            existing[date] = (path, text, metadata, prefix, previous, suffix)
            for message in previous:
                if message["event_id"] in events:
                    raise ConversationConflictError("duplicate persisted event ID across days")
                events[message["event_id"]] = render_messages([message], framed=True)
    segments: dict[str, dict[str, Any]] = {}
    for message, event_at in zip(messages, event_times, strict=True):
        date = event_at.strftime("%Y-%m-%d")
        segment = segments.setdefault(
            date,
            {"started_at": event_at, "ended_at": event_at.isoformat(), "messages": []},
        )
        if mode == "merge":
            rendered = render_messages([message], framed=True)
            if message["event_id"] in events:
                if events[message["event_id"]] != rendered:
                    raise ConversationConflictError(f"conflicting conversation event_id: {message['event_id']}")
                continue
            events[message["event_id"]] = rendered
        segment["ended_at"] = event_at.isoformat()
        segment["messages"].append(message)

    result: dict[str, str] = {}
    writes = []
    for segment_date, segment in segments.items():
        previous = existing.get(segment_date)
        added = bool(segment["messages"])
        if mode == "merge":
            segment["messages"] = (previous[4] if previous else []) + segment["messages"]
            segment["messages"].sort(key=lambda message: parse_time(message["timestamp"]))
            times = [parse_time(message["timestamp"]) for message in segment["messages"]]
            segment["started_at"] = min(times)
            segment["ended_at"] = max(times).isoformat()
        segment_started_at = segment["started_at"]
        segment_messages = segment["messages"]
        stamp = segment_started_at.strftime("%Y%m%d-%H%M%S")
        conversation_id = f"conv_{session_key}_{segment_started_at:%Y%m%d}"
        rel_path = (
            f"30_Conversations/raw/{segment_started_at:%Y/%m/%d}/"
            f"{stamp}-{agent['agent_id']}-{session_key}.md"
        )
        body = f"""---
pv_schema: 1
id: {yaml_scalar(conversation_id)}
memory_type: transcript
type: conversation
status: raw
conversation_id: {yaml_scalar(conversation_id)}
agent_id: {yaml_scalar(agent["agent_id"])}
session_id: {yaml_scalar(session_id)}
segment_date: {yaml_scalar(segment_date)}
host: {yaml_scalar(settings.host_id)}
started_at: {yaml_scalar(segment_started_at.isoformat())}
observed_at: {yaml_scalar(segment_started_at.isoformat())}
ended_at: {yaml_scalar(segment["ended_at"])}
event_count: {len(segment_messages)}
project: {yaml_scalar([f"[[{project}]]"] if project else [])}
topics: {yaml_scalar(topics)}
review_state: unreviewed
temporal_state: point_observation
outcome: {yaml_scalar(payload.get("outcome", "unknown"))}
provenance_mode: {yaml_scalar(payload.get("provenance_mode", "direct_observation"))}
conflict_state: none
retrieval_tier: evidence
privacy: {yaml_scalar(payload.get("privacy", "normal"))}
capture_kind: {yaml_scalar(payload.get("capture_kind", "conversation"))}
note_type: {yaml_scalar(payload.get("note_type"))}
{extra_metadata.rstrip()}
---

# Conversation: {title}

## Messages

{render_messages(segment_messages, framed=True)}

## Context Snapshot

```json
{json_block(payload.get("context") or {})}
```
"""
        target = target_path(settings, rel_path, agent["allowed_roots"])
        if previous:
            target, old_body, metadata, prefix, _messages, suffix = previous
            rel_path = target.relative_to(settings.vault_dir).as_posix()
            for key, value in {
                "started_at": yaml_scalar(segment_started_at.isoformat()),
                "observed_at": yaml_scalar(segment_started_at.isoformat()),
                "ended_at": yaml_scalar(segment["ended_at"]),
                "event_count": str(len(segment_messages)),
            }.items():
                prefix, replacements = re.subn(rf"(?m)^{key}: [^\n]*$", lambda _match: f"{key}: {value}", prefix)
                if replacements != 1:
                    raise ConversationConflictError(f"existing conversation has no unique {key}")
            body = prefix + render_messages(segment_messages, framed=True) + suffix if added else old_body
        stale_targets = [
            path
            for path in target.parent.glob(f"*-{agent['agent_id']}-{session_key}.md")
            if path != target
        ]
        operation = "updated" if target.exists() or stale_targets else "created"
        writes.append((target, body, stale_targets, not previous or body != previous[1]))
        result = {
            "status": "ok",
            "operation": operation,
            "conversation_id": conversation_id,
            "path": rel_path,
        }
    for target, body, stale_targets, changed in writes:
        if changed:
            replace_markdown(target, body)
        for stale_target in stale_targets:
            stale_target.unlink()
    return result


def save_agent_note(
    settings: Settings,
    agent: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, str]:
    require_scope(agent, "agent-memo")
    raw_title = str(payload.get("title") or "").strip()
    note_body = str(payload.get("body") or "").strip()
    if not raw_title or raw_title in {"-h", "--help"}:
        raise ValueError("note title is required")
    if not note_body or note_body == "(empty memo)":
        raise ValueError("note body is required")

    tags = clean_list(payload.get("tags"))
    note_type = str(payload.get("note_type") or "observation")
    if note_type not in AGENT_NOTE_TYPES:
        raise ValueError("note_type must be observation, proposal, or handoff")

    outcome = str(payload.get("outcome") or "unknown")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of: {', '.join(sorted(OUTCOMES))}")

    note_kind = one_line(payload.get("note_kind"), "handoff" if note_type == "handoff" else "note")[:80]
    if note_kind in {"smoke-test", "help-output"}:
        raise ValueError("temporary help or smoke output is not durable memory")
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    provenance_mode = str(provenance.get("mode") or "reported")
    if provenance_mode not in {"direct_observation", "derived", "reported", "human_asserted"}:
        raise ValueError("provenance mode must be direct_observation, derived, reported, or human_asserted")
    applicability = payload.get("applicability") if isinstance(payload.get("applicability"), dict) else {}
    relations = payload.get("relations") if isinstance(payload.get("relations"), dict) else {}
    repositories = repository_sources(payload.get("repository_sources"))
    observed_at = parse_time(payload.get("observed_at")) if payload.get("observed_at") else None
    now = datetime.now(timezone.utc)
    title = one_line(raw_title, "agent note")
    note_id = f"note_{now:%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"
    event_at = observed_at or now
    context = {
        "capture_kind": "agent_note",
        "source_session_id": str(payload.get("session_id") or ""),
        "note_type": note_type,
        "note_kind": note_kind,
        "outcome": outcome,
        "subject_id": str(payload.get("subject_id") or ""),
        "subject_aliases": clean_list(payload.get("subject_aliases")),
        "error_signatures": clean_list(payload.get("error_signatures")),
        "applicability": applicability,
        "provenance": {**provenance, "mode": provenance_mode},
        "relations": relations,
        "source_refs": reference_ids(payload.get("source_refs")),
        "source_hashes": payload.get("source_hashes") if isinstance(payload.get("source_hashes"), dict) else {},
        "repository_sources": repositories,
    }
    result = _save_conversation(
        settings,
        agent,
        {
            "session_id": note_id,
            "project": payload.get("project"),
            "title": f"Agent note: {title}",
            "started_at": event_at.isoformat(),
            "ended_at": event_at.isoformat(),
            "messages": [
                {
                    "role": "assistant",
                    "content": note_body,
                    "timestamp": event_at.isoformat(),
                    "event_id": f"{note_id}:assistant",
                    "agent_id": agent["agent_id"],
                }
            ],
            "context": context,
            "tags": tags,
            "privacy": payload.get("privacy", "normal"),
            "capture_kind": "agent_note",
            "note_type": note_type,
            "kind": note_kind,
            "outcome": outcome,
            "provenance_mode": provenance_mode,
            "subject_id": context["subject_id"],
            "subject_aliases": context["subject_aliases"],
            "error_signatures": context["error_signatures"],
            "applicability": applicability,
            "provenance": context["provenance"],
            "relations": relations,
            "source_refs": context["source_refs"],
            "source_hashes": context["source_hashes"],
            "repository_sources": repositories,
        },
    )
    return {**result, "note_id": note_id, "note_type": note_type}
