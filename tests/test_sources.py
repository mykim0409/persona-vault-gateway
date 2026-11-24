"""Read-only source coverage, independently of relevance ranking and embedding."""

import json
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import core
from gateway.app import search_response
from gateway.sources import SourceChanged, list_sources, read_source, source_path
from test_rag_benchmark import no_network

READER = {"agent_id": "reader", "scopes": ["vault-rag"], "allowed_roots": []}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = core.Settings(self.root / "vault", self.root / "gateway.db", "test", embedding_provider="none")
        self.settings.vault_dir.mkdir()

    def note(self, path, text):
        file = self.settings.vault_dir / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        return sha256(text.encode()).hexdigest()

    def listing(self, **kwargs):
        with no_network():
            return list_sources(self.settings, READER, **kwargs)

    def read(self, path, digest, **kwargs):
        with no_network():
            return read_source(self.settings, READER, path=path, document_hash=digest, **kwargs)

    def test_inventory_does_not_depend_on_query_top_k_or_embeddings(self):
        for i in range(31):
            self.note(f"raw/{i:02}.md", f"# Subject {i}\nUnrelated independent content {i}.")
        collected, cursor = [], None
        while True:
            page = self.listing(prefix="raw", cursor=cursor, limit=7)
            self.assertEqual(page["total_sources"], 31)
            self.assertEqual(page["span"][0], len(collected))
            collected.extend(r["path"] for r in page["sources"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(collected, sorted(set(collected)))
        self.assertEqual(len(collected), 31)
        self.assertFalse(self.settings.db_path.exists())

    def test_full_read_preserves_frontmatter_roles_conditions_and_late_corrections(self):
        text = "---\nobserved_at: 2026-11-24\n---\n# Imported conversation\n"
        text += ("user: Preserve the previous result. 한글 🙂\nassistant: Implementation done, deployment unknown.\n" * 160)
        text += "user (2026-11-16): Correction: do not remove implemented work just because deployment is unknown.\n"
        digest = self.note("raw/import.md", text)
        pages, cursor, end = [], None, 0
        while True:
            page = self.read("raw/import.md", digest, cursor=cursor, max_characters=257)
            self.assertEqual(page["span"][0], end)
            self.assertEqual(page["total_characters"], len(text))
            end = page["span"][1]
            pages.append(page["text"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual("".join(pages), text)
        self.assertEqual(end, len(text))

    def test_search_handle_reads_tail_missing_from_ranked_excerpts(self):
        core.init_db(self.settings.db_path)
        body = "# Recovery\n\nOrion restore completed after pausing the writer.\n\n"
        body += "\n\n".join(f"Unrelated section {i}. " + "neutral text " * 60 for i in range(50))
        body += "\n\nuser: Later correction: the rollback was not deployed to Windows.\n"
        digest = self.note("raw/long.md", body)
        with no_network():
            result = search_response(core.search_vault(self.settings, READER, "Orion restore", limit=1), "all")
        self.assertFalse(result["coverage"]["exhaustive"])
        row = result["results"][0]
        self.assertEqual(row["document_hash"], digest)
        self.assertIn(digest, result["context"])
        self.assertNotIn("Later correction", result["context"])
        text, cursor = "", None
        while True:
            page = self.read(row["path"], row["document_hash"], cursor=cursor)
            text += page["text"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(text, body)

    def test_changed_content_rejects_first_and_followup_reads(self):
        digest = self.note("a.md", "old " * 200)
        page = self.read("a.md", digest, max_characters=256)
        self.note("a.md", "new " * 200)
        for cursor in (None, page["next_cursor"]):
            with self.assertRaises(SourceChanged):
                self.read("a.md", digest, cursor=cursor)

    def test_inventory_cursor_rejects_add_delete_edit_and_prefix_change(self):
        self.note("a.md", "old")
        self.note("b.md", "old")
        cursor = self.listing(limit=1)["next_cursor"]
        self.note("b.md", "new")
        with self.assertRaises(SourceChanged):
            self.listing(cursor=cursor)
        cursor = self.listing(limit=1)["next_cursor"]
        self.note("c.md", "new")
        with self.assertRaises(SourceChanged):
            self.listing(cursor=cursor)
        cursor = self.listing(limit=1)["next_cursor"]
        (self.settings.vault_dir / "c.md").unlink()
        with self.assertRaises(SourceChanged):
            self.listing(cursor=cursor)
        with self.assertRaises(SourceChanged):
            self.listing(prefix="a.md", cursor=self.listing(limit=1)["next_cursor"])

    def test_cursor_cannot_cross_documents_or_read_and_list_operations(self):
        digest = self.note("a.md", "text " * 100)
        self.note("b.md", "text " * 100)
        cursor = self.read("a.md", digest, max_characters=256)["next_cursor"]
        with self.assertRaises(SourceChanged):
            self.read("b.md", digest, cursor=cursor)
        with self.assertRaises(SourceChanged):
            self.read("a.md", digest, cursor=self.listing(limit=1)["next_cursor"])

    def test_same_read_scope_and_same_exclusions_as_search(self):
        digest = self.note("90_Private/visible.md", "visible to the existing read scope")
        for hidden in (".git", ".obsidian", ".tmp"):
            self.note(f"{hidden}/secret.md", "secret")
            (self.settings.vault_dir / f"alias-{hidden}.md").symlink_to(self.settings.vault_dir / hidden / "secret.md")
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (self.settings.vault_dir / "outside.md").symlink_to(outside)
        self.assertEqual([r["path"] for r in self.listing()["sources"]], ["90_Private/visible.md"])
        self.assertEqual(self.read("90_Private/visible.md", digest)["text"], "visible to the existing read scope")
        with self.assertRaises(FileNotFoundError):
            self.read("outside.md", digest)
        writer = {"agent_id": "writer", "scopes": ["conversation-write"]}
        for fn, args in ((list_sources, {}), (read_source, {"path": "90_Private/visible.md", "document_hash": digest})):
            with self.assertRaises(PermissionError):
                fn(self.settings, writer, **args)

    def test_prefix_is_path_boundary_not_substring(self):
        self.note("raw/a.md", "a")
        self.note("raw-extra/b.md", "b")
        self.assertEqual(self.listing(prefix="raw")["total_sources"], 1)
        self.assertEqual(self.listing(prefix="raw/a.md")["total_sources"], 1)
        self.assertEqual(self.listing(prefix="absent")["sources"], [])

    def test_invalid_paths_and_cursors_fail_instead_of_partial_success(self):
        for value in (".", "..", "../a.md", "/tmp/a.md", "raw/../a.md", "raw//a.md", "raw/./a.md", "a\\b.md", "a\x00.md", ".tmp/a.md"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source_path(value, prefix=True)
        digest = self.note("a.md", "a" * 800)
        for cursor in ("bad", "0" * 64 + ":-1", "0" * 64 + ":01"):
            with self.assertRaises(ValueError):
                self.read("a.md", digest, cursor=cursor)
        for limit in (0, 51, True):
            with self.assertRaises(ValueError):
                self.listing(limit=limit)
        for size in (0, 8001, True):
            with self.assertRaises(ValueError):
                self.read("a.md", digest, max_characters=size)

    def test_empty_file_and_inventory_have_no_continuation(self):
        self.assertIsNone(self.listing()["next_cursor"])
        digest = self.note("empty.md", "")
        page = self.read("empty.md", digest)
        self.assertEqual(page["span"], [0, 0])
        self.assertEqual(page["text"], "")
        self.assertIsNone(page["next_cursor"])

    def test_wire_budget_and_read_does_not_sanitize_or_write_original(self):
        text = "\x01" * 9000
        digest = self.note("control.md", text)
        page = self.read("control.md", digest, max_characters=8000)
        self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 60000)
        self.assertEqual((self.settings.vault_dir / "control.md").read_text(), text)
        self.assertFalse(self.settings.db_path.exists())

    def test_unreadable_file_does_not_silently_disappear_from_inventory(self):
        self.note("a.md", "a")
        with patch.object(Path, "read_text", side_effect=OSError("read failed")):
            with self.assertRaises(OSError):
                self.listing()

    def test_large_inventory_pages_respect_wire_budget_without_dropping_files(self):
        directory = "/".join(["문" * 70] * 8)
        self.note("text.md", "# " + "제" * 160)
        files = [(f"{directory}/{i:02}.md", self.settings.vault_dir / "text.md") for i in range(50)]
        seen, cursor = [], None
        # Exercise long UTF-8 metadata without depending on the host filesystem's path-length limit.
        with patch.object(core, "iter_markdown_files", return_value=files):
            while True:
                page = self.listing(limit=50, cursor=cursor)
                self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 60000)
                seen.extend(row["path"] for row in page["sources"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        self.assertEqual(len(seen), 50)
        self.assertEqual(len(set(seen)), 50)

if __name__ == "__main__":
    unittest.main()
