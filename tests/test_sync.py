import contextlib
import importlib.util
import io
import os
import re
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "/tmp/pv-sync-clone-ready"


def service_block(name):
    """Text of one compose service (2-space-indented key) without a YAML dependency."""
    lines = (ROOT / "compose.yml").read_text().splitlines()
    start = lines.index(f"  {name}:")
    end = next((j for j in range(start + 1, len(lines))
                if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) <= 2), len(lines))
    return "\n".join(lines[start:end])


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


class SandboxCase(unittest.TestCase):
    """Temp HOME, bare remote with one commit, and a sandboxed copy of the sync script."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.home = t / "home"
        self.home.mkdir()
        self.remote = t / "remote.git"
        self.vault = t / "vault"
        self.other = t / "other"
        self.marker = t / "clone-ready"
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

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.vault, env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def sandboxed_script(self, loops=1, prelude=""):
        script = extract_sync_script()
        self.assertIn("git pull --rebase --autostash", script)
        # Point /vault and the ready marker at temp paths, skip global git config, bound the loop.
        script = script.replace("/vault", str(self.vault)).replace(MARKER, str(self.marker))
        # Nothing production-owned may remain once the sandbox paths (which may live under /tmp) are removed.
        leftover = script.replace(str(self.vault), "").replace(str(self.marker), "")
        self.assertNotIn("/vault", leftover)
        self.assertNotIn(MARKER, leftover)
        self.assertNotIn(str(self.marker), (MARKER, "/vault"))
        script = "\n".join(l for l in script.splitlines() if "git config --global" not in l)
        harness = (
            f'n=0\nsleep() {{ n=$((n+1)); [ "$n" -lt {loops} ] || exit 0; }}\n'
        )
        return harness + prelude + script

    def run_sync(self, loops=1, prelude=""):
        return subprocess.run(["/bin/sh", "-ec", self.sandboxed_script(loops, prelude)], env=self.env,
                              capture_output=True, text=True, timeout=60)


class SyncScriptTest(SandboxCase):
    def setUp(self):
        super().setUp()
        self.git("clone", str(self.remote), str(self.vault), cwd=Path(self.tmp.name))

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
        self.assertTrue(self.marker.exists())  # blocked sync does not retract initial clone readiness
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

    def test_existing_repo_is_validated_then_marked_ready(self):
        self.marker.write_text("stale\n")
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.marker.exists())


class CloneReadinessTest(SandboxCase):
    def run_blocked_clone(self):
        """Start sync with a clone that blocks until released; the git wrapper only affects `clone`."""
        gate = Path(self.tmp.name) / "gate"
        gate.mkdir()
        self.started, self.release = gate / "started", gate / "release"
        prelude = (
            'git() { if [ "$1" = clone ]; then : > "$GATE_STARTED"; '
            'while [ ! -e "$GATE_RELEASE" ]; do command sleep 0.05; done; fi; command git "$@"; }\n'
        )
        env = {**self.env, "GATE_STARTED": str(self.started), "GATE_RELEASE": str(self.release)}
        proc = subprocess.Popen(["/bin/sh", "-ec", self.sandboxed_script(1, prelude)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not self.started.exists():
            self.assertLess(time.monotonic(), deadline, "clone never started")
            if proc.poll() is not None:
                self.fail(f"sync exited before clone started: {proc.communicate()[1]}")
            time.sleep(0.02)
        return proc

    def test_marker_absent_while_clone_is_slow_then_written_after_checkout(self):
        self.marker.write_text("stale\n")  # left behind by an earlier container run
        proc = self.run_blocked_clone()
        time.sleep(0.3)
        self.assertIsNone(proc.poll())
        self.assertFalse(self.marker.exists(), "marker must be cleared at start and absent mid-clone")
        self.assertFalse((self.vault / ".git").exists())
        self.release.touch()
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue(self.marker.exists())
        self.assertEqual((self.vault / "note.md").read_text(), "base\n")

    def test_failed_clone_leaves_no_marker_and_clears_stale_one(self):
        self.marker.write_text("stale\n")
        self.env["VAULT_REPO_SSH_URL"] = str(Path(self.tmp.name) / "missing.git")
        r = self.run_sync()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.vault / ".git").exists())

    def test_invalid_existing_repo_path_is_not_ready(self):
        (self.vault / ".git").mkdir(parents=True)  # a .git directory that is not a repository
        self.marker.write_text("stale\n")
        r = self.run_sync()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a usable git work tree", r.stderr)
        self.assertFalse(self.marker.exists())

    def test_script_orders_marker_around_the_single_clone(self):
        s = extract_sync_script()
        self.assertEqual(s.count("git clone"), 1)
        order = [s.index(f"rm -f {MARKER}"), s.index("git clone"), s.index(f": > {MARKER}"), s.index("while true; do")]
        self.assertEqual(order, sorted(order))


class ComposeContractTest(unittest.TestCase):
    def test_gateway_waits_for_healthy_sync_whose_probe_is_the_marker(self):
        gateway, sync = service_block("persona-vault-gateway"), service_block("persona-vault-sync")
        self.assertRegex(gateway, r"persona-vault-sync:\n\s+condition: service_healthy")
        self.assertRegex(gateway, r"qdrant:\n\s+condition: service_started")
        self.assertIn(f'test: ["CMD", "test", "-f", "{MARKER}"]', sync)
        self.assertIn("start_period:", sync)

    def test_images_are_pinned_by_digest(self):
        refs = re.findall(r"^\s+image: (\S+)$", (ROOT / "compose.yml").read_text(), re.M)
        refs.remove("persona-vault-gateway:local")  # locally built, not pulled
        refs.append(re.search(r"^FROM (\S+)$", (ROOT / "Dockerfile").read_text(), re.M).group(1))
        self.assertEqual(len(refs), 3)
        for ref in refs:
            self.assertRegex(ref, r"@sha256:[0-9a-f]{64}$")


class ComposeSmokePreflightTest(unittest.TestCase):
    """The smoke test must SKIP (exit 2) before touching Docker when its prerequisites are missing. All mocked."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("compose_smoke", ROOT / "tests/test_compose_smoke.py")
        self.smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.smoke)

    def main(self, argv, docker=True, compose_rc=0, info_rc=0):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[1:3])
            return SimpleNamespace(returncode=compose_rc if cmd[1] == "compose" else info_rc, stdout="", stderr="")

        out = io.StringIO()
        with mock.patch.object(self.smoke, "run", fake_run), \
                mock.patch.object(self.smoke.shutil, "which", lambda name: "/usr/bin/docker" if docker else None), \
                mock.patch.object(self.smoke, "Smoke") as smoke_cls, \
                mock.patch.object(self.smoke.sys, "argv", ["test_compose_smoke.py", *argv]), \
                contextlib.redirect_stdout(out):
            code = self.smoke.main()
        return code, out.getvalue(), calls, smoke_cls

    def test_missing_docker_cli_or_compose_plugin_skips_both_modes(self):
        for argv in ([], ["--prepare-only"]):
            for kwargs in ({"docker": False}, {"compose_rc": 1}):
                with self.subTest(argv=argv, **kwargs):
                    code, out, calls, smoke_cls = self.main(argv, **kwargs)
                    self.assertEqual(code, 2)
                    self.assertIn("SKIPPED", out)
                    smoke_cls.assert_not_called()  # no temp dir, no compose config
                    self.assertNotIn(["info"], calls)

    def test_full_run_without_daemon_skips(self):
        code, out, calls, smoke_cls = self.main([], info_rc=1)
        self.assertEqual(code, 2)
        self.assertIn("no running Docker daemon", out)
        smoke_cls.assert_not_called()

    def test_prepare_only_needs_compose_but_not_daemon(self):
        code, out, calls, smoke_cls = self.main(["--prepare-only"], info_rc=1)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["compose", "version"]])  # `docker info` never consulted
        smoke_cls.return_value.prepare.assert_called_once()
        smoke_cls.return_value.scenario.assert_not_called()


if __name__ == "__main__":
    unittest.main()
