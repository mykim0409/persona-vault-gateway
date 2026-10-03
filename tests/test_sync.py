import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def extract_sync_script():
    """Pull the persona-vault-sync entrypoint shell block out of compose.yml."""
    lines = (ROOT / "compose.yml").read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("  persona-vault-sync:"))
    i = next(j for j in range(start, len(lines)) if lines[j].strip() == "- |")
    indent = len(lines[i + 1]) - len(lines[i + 1].lstrip())
    block = []
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        block.append(line[indent:])
    return "\n".join(block).replace("$$", "$")


class SyncScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.home = t / "home"
        self.home.mkdir()
        self.remote = t / "remote.git"
        self.vault = t / "vault"
        self.other = t / "other"
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost",
            "VAULT_REPO_SSH_URL": str(self.remote),
        }
        self.git("init", "--bare", "-b", "main", str(self.remote), cwd=t)
        self.git("clone", str(self.remote), str(self.other), cwd=t)
        (self.other / "note.md").write_text("base\n")
        self.git("add", ".", cwd=self.other)
        self.git("commit", "-m", "base", cwd=self.other)
        self.git("push", "origin", "HEAD:main", cwd=self.other)
        self.git("clone", str(self.remote), str(self.vault), cwd=t)

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.vault, env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def run_sync(self, loops=1):
        script = extract_sync_script()
        self.assertIn("git pull --rebase --autostash", script)
        # Point at the temp clone, skip global git config, bound the loop.
        script = script.replace("/vault", str(self.vault))
        script = "\n".join(l for l in script.splitlines() if "git config --global" not in l)
        harness = (
            f'n=0\nsleep() {{ n=$((n+1)); [ "$n" -lt {loops} ] || exit 0; }}\n'
        )
        return subprocess.run(["/bin/sh", "-ec", harness + script], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def make_conflict(self):
        (self.other / "note.md").write_text("remote\n")
        self.git("commit", "-am", "remote", cwd=self.other)
        self.git("push", "origin", "HEAD:main", cwd=self.other)
        (self.vault / "note.md").write_text("local\n")

    def test_script_extracted(self):
        s = extract_sync_script()
        self.assertIn("while true; do", s)
        self.assertIn("git push", s)
        self.assertIn("sync_blocked", s)

    def test_happy_path_pushes(self):
        (self.vault / "new.md").write_text("hi\n")
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("new.md", self.git("ls-tree", "-r", "--name-only", "main", cwd=self.remote))

    def test_conflict_then_restart_is_blocked(self):
        self.make_conflict()
        r1 = self.run_sync()
        self.assertNotEqual(r1.returncode, 0)  # pull failed, container would restart
        self.assertTrue((self.vault / self.git("rev-parse", "--git-path", "rebase-merge")).exists())
        head = self.git("rev-parse", "HEAD")
        remote_head = self.git("rev-parse", "main", cwd=self.remote)
        status = self.git("status", "--porcelain")
        content = (self.vault / "note.md").read_text()
        self.assertIn("<<<<<<<", content)

        r2 = self.run_sync(loops=2)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("BLOCKED", r2.stderr)
        self.assertIn("note.md", r2.stderr)
        self.assertLess(len(r2.stderr.splitlines()), 40)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), remote_head)
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual((self.vault / "note.md").read_text(), content)
        self.assertNotIn("<<<<<<<", self.git("show", "main:note.md", cwd=self.remote))

    def test_unmerged_index_without_rebase_is_blocked(self):
        self.make_conflict()
        self.git("commit", "-am", "local")
        self.git("fetch", "origin")
        r = subprocess.run(["git", "merge", "origin/main"], cwd=self.vault, env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        # Drop MERGE_HEAD so only the unmerged index remains.
        (self.vault / self.git("rev-parse", "--git-path", "MERGE_HEAD")).unlink()
        head = self.git("rev-parse", "HEAD")
        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("BLOCKED", res.stderr)
        self.assertIn("unmerged files", res.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIn("<<<<<<<", (self.vault / "note.md").read_text())


if __name__ == "__main__":
    unittest.main()
