from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import tempfile
from datetime import date
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from .compaction import annotate_drafts, concurrent_raw_captures, finish_search, projected_documents, review_binding, staging_path
from .core import RAG_EXCLUDED_DIRS, Settings, document_metadata, markdown_body, qdrant_compaction_neighbors, vault_documents
from .wiki import BRIEF_CAP, analyze_documents, build_compaction_plan, check_compaction, health_report


def plan_brief_cap(plan: dict, args) -> int:
    # The flag wins; otherwise reuse the cap compact-plan recorded, so later commands need not repeat it.
    flag = getattr(args, "brief_cap", None)
    if flag is not None:
        return flag
    for ledger in (plan.get("selected") or {}).get("ledgers") or []:
        if "cap" in ledger:
            return int(ledger["cap"])
    return BRIEF_CAP


def settings_for(vault: str | None) -> Settings:
    base = Settings.from_env()
    return Settings(
        vault_dir=Path(vault or os.getenv("VAULT_DIR", "vault")).resolve(),
        db_path=base.db_path,
        host_id=base.host_id,
        embedding_provider=base.embedding_provider,
        embedding_model=base.embedding_model,
        embedding_batch_size=base.embedding_batch_size,
        cloudflare_account_id=base.cloudflare_account_id,
        cloudflare_api_token=base.cloudflare_api_token,
        qdrant_url=base.qdrant_url,
        qdrant_collection=base.qdrant_collection,
        qdrant_api_key=base.qdrant_api_key,
    )


def analysis_for(settings: Settings) -> dict[str, Any]:
    return analyze_documents(vault_documents(settings))


def git_snapshot(vault: Path, *, include_dates: bool = True) -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(vault), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout

    try:
        root = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
        if root != vault.resolve():
            return {"status": "blocked", "reason": "vault_must_be_git_root"}
        tracked = {
            path.decode()
            for path in git("ls-files", "-z").split(b"\0")
            if path and path.decode().endswith(".md")
        }
        dirty = {
            path.decode()
            for path in git("diff", "--name-only", "-z", "HEAD", "--").split(b"\0")
            if path and path.decode().endswith(".md")
        }
        fallback_dates = {}
        for path in tracked:
            if not include_dates or Path(path).parent.as_posix() != "30_Conversations/raw":
                continue
            additions = git(
                "log", "--follow", "--diff-filter=A", "--format=%aI", "--", path
            ).decode().splitlines()
            if additions:
                fallback_dates[path] = additions[-1]
        return {
            "status": "ok",
            "revision": git("rev-parse", "HEAD").decode().strip(),
            "tracked_paths": tracked,
            "dirty_paths": dirty,
            "fallback_dates": fallback_dates,
        }
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        return {"status": "blocked", "reason": f"git_snapshot_failed: {exc}"}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pvg-wiki", description="Inspect and curate PersonaVault wiki metadata.")
    root.add_argument("--vault", help="PersonaVault directory (defaults to VAULT_DIR).")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="Print the current wiki health report.")
    compact = commands.add_parser("compact-plan", help="Build the next read-only raw compaction graph.")
    compact.add_argument("--before", default=date.today().isoformat(), help="Exclude raw from this date onward.")
    compact.add_argument("--max-sources", type=int, default=8, help="Maximum source files in the selected batch.")
    compact.add_argument("--max-characters", type=int, default=60_000, help="Source-body character budget; an oversized oldest file stays whole.")
    compact.add_argument("--deferrals", help="Local JSON list of hash-bound source deferrals.")
    compact.add_argument("--output", help="Save the full plan to a new JSON file inside Vault .tmp/curating/.")
    compact.add_argument("--full", action="store_true", help="Print the full plan instead of the compact preview.")
    check = commands.add_parser("compact-check", help="Check applied Markdown against an annotated plan; does not authorize changes.")
    check.add_argument("plan", help="Saved compact-plan JSON with its review section completed.")
    annotate = commands.add_parser("compact-annotate", help="Write pvg-src provenance markers from review anchors into staged drafts only.")
    annotate.add_argument("plan")
    review = commands.add_parser("compact-review", help="Check staged targets and report reusable semantic-review bindings.")
    review.add_argument("plan")
    review.add_argument("--record", action="store_true", help="Record bindings AFTER independent review; not user approval.")
    finish = commands.add_parser("compact-finish", help="Check applied changes, index once and run final search probes; never apply/commit.")
    finish.add_argument("plan")
    compact.add_argument("--brief-cap", type=int, default=BRIEF_CAP, help="Maximum BRIEF.md/PROFILE.md body characters, pvg-src markers excluded.")
    for command in (check, review, finish):
        command.add_argument("--brief-cap", type=int, default=None, help="Override the cap recorded in the plan (default: the plan's cap).")

    conflicts = commands.add_parser("conflicts", help="List explicit conflicts.")
    conflict_commands = conflicts.add_subparsers(dest="conflict_command", required=True)
    conflict_commands.add_parser("list", help="Print unresolved conflicts.")
    return root


def documents_at_revision(vault: Path, revision: str) -> list[dict[str, Any]]:
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("plan revision must be a full commit ID")

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(vault), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).stdout

    entries = []
    for record in git("ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        if (
            header.split()[0] not in {b"100644", b"100755"}
            or not path.endswith(".md") or any(part in RAG_EXCLUDED_DIRS for part in Path(path).parts)
        ):
            continue
        entries.append((path, header.split()[2]))
    blobs = io.BytesIO(subprocess.run(
        ["git", "-C", str(vault), "cat-file", "--batch"],
        input=b"".join(object_id + b"\n" for _path, object_id in entries),
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout)
    documents = []
    for path, object_id in entries:
        oid, kind, size = blobs.readline().split()
        if oid != object_id or kind != b"blob":
            raise ValueError("unexpected Git blob response")
        raw = blobs.read(int(size))
        if len(raw) != int(size) or blobs.read(1) != b"\n":
            raise ValueError("incomplete Git blob response")
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        documents.append({
            **document_metadata(path, text), "path": path, "text": markdown_body(text),
            "document_hash": sha256(text.encode("utf-8")).hexdigest(),
        })
    return documents


def plan_output(plan: dict[str, Any], args: argparse.Namespace, vault: Path) -> dict[str, Any]:
    output = None
    if args.output:
        output = Path(args.output).resolve()
        if not output.is_relative_to(vault / ".tmp" / "curating") or output.suffix != ".json":
            return {"status": "blocked", "reason": "plan output must be a JSON file inside Vault .tmp/curating/"}
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            # Never replace a previously reviewed or approved plan.
            with output.open("x", encoding="utf-8") as file:
                json.dump(plan, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
        except OSError as exc:
            return {"status": "blocked", "reason": f"plan_output_failed: {exc}"}
    if args.full:
        return plan
    selected = plan.get("selected")
    return {
        **{key: plan[key] for key in ("status", "revision", "before", "eligible_sources", "excluded", "invalidated_deferrals") if key in plan},
        "plan_file": str(output) if output else None,
        "queue_components": len(plan.get("queue", [])),
        "deferred_sources": len(plan.get("deferred", [])),
        "selected": {
            **{key: selected[key] for key in (
                "component_id", "routing", "source_paths", "source_items", "source_characters",
                "character_budget", "oversized_source", "remaining_source_files", "semantic", "ledgers", "subagent_items",
            )},
            "target_candidates": [
                node["path"] for node in selected["graph"]["nodes"] if node["type"] == "canonical_candidate"
            ],
            "create_targets": [node["path"] for node in selected["graph"]["nodes"] if node.get("create")],
        } if selected else None,
    }


def update_plan(path: Path, plan: dict, expected: bytes) -> None:
    """Only checkpoint the local plan, refusing edits made while checks were running."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(plan, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        if path.read_bytes() != expected:
            raise ValueError("plan changed during checks")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_compaction(args: argparse.Namespace, settings: Settings, documents: list[dict]) -> dict:
    started = perf_counter()
    timings = {}
    try:
        path = Path(args.plan).resolve()
        if args.command != "compact-check":
            path = staging_path(settings.vault_dir, path)
        raw_plan = path.read_bytes()
        plan = json.loads(raw_plan)
        if not isinstance(plan, dict):
            raise ValueError("plan must be an object")
        snapshot = git_snapshot(settings.vault_dir, include_dates=False)
        if snapshot["status"] != "ok":
            return snapshot
        if snapshot["revision"] != plan.get("revision"):
            return {"status": "blocked", "reason": "plan_base_changed"}
        before = documents_at_revision(settings.vault_dir, plan["revision"])
        hashes = lambda docs: {doc["path"]: doc["document_hash"] for doc in docs}
        after, concurrent_raw = concurrent_raw_captures(settings.vault_dir, plan, before, documents)
        if args.command == "compact-review":
            original, current = hashes(before), hashes(after)
            changed = {p for p in original.keys() | current.keys() if original.get(p) != current.get(p)}
            deleted = set(plan["review"]["delete_paths"])
            targets = {p for item in plan["review"]["items"] for p in item["targets"]}
            if (
                (changed | (snapshot["dirty_paths"] - concurrent_raw.keys())) - deleted - targets
                or any(p in current and current[p] != original.get(p) for p in deleted)
            ):
                return {"status": "blocked", "reason": "unplanned_changes_during_review"}
            # A revised draft can be reviewed after partial apply, without restoring deleted raw.
            after = projected_documents(settings.vault_dir, plan, before)
        integrity_after = {doc["path"]: doc for doc in after}
        integrity_after.update((doc["path"], doc) for doc in documents if doc["path"] in concurrent_raw)
        result = check_compaction(
            plan, before, after, integrity_after=list(integrity_after.values()), brief_cap=plan_brief_cap(plan, args),
        )
        result["concurrent_raw_captures"] = concurrent_raw
        timings["mechanical_seconds"] = round(perf_counter() - started, 4)
        if result["status"] != "ok":
            return {**result, "timings": timings}
        review = plan["review"]
        deleted = set(review["delete_paths"])
        targets = {target for item in review["items"] for target in item["targets"]}
        if snapshot["dirty_paths"] - concurrent_raw.keys() - deleted - targets:
            return {"status": "blocked", "reason": "unplanned_git_changes"}
        if args.command != "compact-review" and any(
            (settings.vault_dir / target).exists() or (settings.vault_dir / target).is_symlink() for target in deleted
        ):
            return {"status": "blocked", "reason": "source_not_deleted"}
        if args.command != "compact-check":
            projected = projected_documents(settings.vault_dir, plan, before)
            if hashes(after) != hashes(projected):
                return {"status": "blocked", "reason": "applied_patch_differs_from_drafts"}
            binding = review_binding(plan, before, after)
            result["review_binding"] = {key: value for key, value in binding.items() if key != "fingerprints"}
            if args.command == "compact-review":
                result["semantic_review"] = "recorded" if args.record else "record_required" if binding["review_required_ids"] else "reusable"
                if args.record:
                    plan["review_checkpoint"] = binding["fingerprints"]
                    plan.pop("finish_checkpoint", None)
            else:
                if binding["review_required_ids"]:
                    return {**result, "status": "blocked", "reason": "semantic_review_changed_or_missing"}
                # Validate the same whole-tree snapshot before any index writes as well as afterward.
                if (
                    git_snapshot(settings.vault_dir, include_dates=False).get("revision") != plan["revision"]
                    or hashes(core_docs := vault_documents(settings)) != hashes(documents)
                    or path.read_bytes() != raw_plan
                ):
                    return {"status": "blocked", "reason": "vault_or_plan_changed_before_indexing"}
                search_started = perf_counter()
                final = finish_search(settings, plan, core_docs)
                timings["index_and_search_seconds"] = round(perf_counter() - search_started, 4)
                if final.get("status") == "blocked":
                    return {**result, **final, "timings": timings}
                plan["finish_checkpoint"] = final
                result.update(semantic_review="recorded", index_verification="passed", search=final)
        if (
            git_snapshot(settings.vault_dir, include_dates=False).get("revision") != plan["revision"]
            or hashes(vault_documents(settings)) != hashes(documents)
            or path.read_bytes() != raw_plan
        ):
            return {"status": "blocked", "reason": "vault_or_plan_changed_during_check"}
        if args.command != "compact-check" and hashes(projected_documents(settings.vault_dir, plan, before)) != hashes(after):
            return {"status": "blocked", "reason": "draft_changed_during_check"}
        if args.command == "compact-finish" or (args.command == "compact-review" and args.record):
            update_plan(path, plan, raw_plan)
        return {**result, "timings": timings}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, subprocess.CalledProcessError) as exc:
        return {"status": "blocked", "reason": f"invalid_compaction_check: {exc}", "timings": timings}


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = parser().parse_args(argv)
    settings = settings_for(args.vault)
    if args.command == "compact-annotate":
        try:
            plan = json.loads(staging_path(settings.vault_dir, Path(args.plan)).read_text(encoding="utf-8"))
            return {"status": "ok", "annotated": annotate_drafts(settings.vault_dir, plan)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return {"status": "blocked", "reason": f"invalid_annotation: {exc}"}
    read_started = perf_counter()
    documents = vault_documents(settings)
    read_seconds = round(perf_counter() - read_started, 4)
    if args.command == "health":
        return health_report(analyze_documents(documents))
    if args.command in {"compact-check", "compact-review", "compact-finish"}:
        result = run_compaction(args, settings, documents)
        result.setdefault("timings", {})["vault_read_seconds"] = read_seconds
        return result
    if args.command == "compact-plan":
        snapshot = git_snapshot(settings.vault_dir)
        if snapshot["status"] != "ok":
            return snapshot
        try:
            deferrals = json.loads(Path(args.deferrals).read_text(encoding="utf-8")) if args.deferrals else None
        except (OSError, ValueError) as exc:
            return {"status": "blocked", "reason": f"invalid_deferrals: {exc}"}
        if snapshot["dirty_paths"]:
            return {"status": "blocked", "reason": "dirty_markdown", "paths": sorted(snapshot["dirty_paths"])}
        try:
            plan = build_compaction_plan(
                documents,
                before=args.before,
                tracked_paths=snapshot["tracked_paths"],
                dirty_paths=snapshot["dirty_paths"],
                fallback_dates=snapshot["fallback_dates"],
                revision=snapshot["revision"],
                max_sources=args.max_sources,
                max_characters=args.max_characters,
                deferrals=deferrals,
                brief_cap=args.brief_cap,
            )
        except (TypeError, ValueError) as exc:
            return {"status": "blocked", "reason": f"invalid_compaction_plan: {exc}"}
        if not plan.get("selected"):
            return plan_output(plan, args, settings.vault_dir)
        selected = plan.get("selected") or {}
        selected_paths = set(selected.get("source_paths") or [])
        semantic = qdrant_compaction_neighbors(
            settings,
            [document for document in documents if document.get("path") in selected_paths],
        )
        if semantic.get("edges"):
            plan = build_compaction_plan(
                documents,
                before=args.before,
                tracked_paths=snapshot["tracked_paths"],
                dirty_paths=snapshot["dirty_paths"],
                fallback_dates=snapshot["fallback_dates"],
                semantic=semantic,
                revision=snapshot["revision"],
                max_sources=args.max_sources,
                max_characters=args.max_characters,
                deferrals=deferrals,
                brief_cap=args.brief_cap,
            )
        else:
            plan["selected"]["semantic"] = {key: value for key, value in semantic.items() if key != "edges"}
        return plan_output(plan, args, settings.vault_dir)
    return {"conflicts": analyze_documents(documents)["conflict_queue"]}


def main() -> None:
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("status") == "blocked":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
