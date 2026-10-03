"""Tests for the Compose smoke test (tests/test_compose_smoke.py) and its building blocks.

The Git sync itself now runs inside the Gateway process and is covered by tests/test_onboarding.py. Everything here is
Docker-free: the smoke test's skip rules (mocked), its backup/restore and test-only transport helpers, and the whole
first-use HTTP journey against a NATIVE `python -m gateway.server` process, so a broken step in the real Docker scenario
is caught without a daemon. The Docker scenario itself still only runs in tests/test_compose_smoke.py.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_smoke():
    spec = importlib.util.spec_from_file_location("compose_smoke", ROOT / "tests/test_compose_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComposeSmokePreflightTest(unittest.TestCase):
    """The smoke test must SKIP (exit 2) before touching Docker when its prerequisites are missing. All mocked."""

    def setUp(self):
        self.smoke = load_smoke()

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
        self.assertEqual([c.args for c in smoke_cls.call_args_list], [("keyword",), ("semantic",)])  # both modes
        self.assertEqual(smoke_cls.return_value.prepare.call_count, 2)
        smoke_cls.return_value.scenario.assert_not_called()


class ComposeSmokeHelpersTest(unittest.TestCase):
    """Logic of the daemon-only smoke steps, exercised on temp files so a broken assertion is caught without Docker."""

    def setUp(self):
        self.smoke = load_smoke()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)

    def test_backup_scripts_round_trip_and_compare_detects_damage(self):
        src, bak, dst = self.t / "src", self.t / "bak", self.t / "dst"
        for d in (src, bak, dst):
            d.mkdir()
        (src / "vault/30_Conversations").mkdir(parents=True)
        (src / "vault/30_Conversations/note.md").write_text("synthetic\n")
        con = sqlite3.connect(src / "gateway.db")
        con.execute("CREATE TABLE agents (name TEXT)")
        con.execute("INSERT INTO agents VALUES ('pvg-smoke')")
        con.commit()
        con.close()

        def py(code, *argv, swap=()):
            for old, new in swap:
                code = code.replace(old, new)
            return subprocess.run([sys.executable, "-c", code, *argv], capture_output=True, text=True, timeout=60)

        self.assertEqual(py(self.smoke.TAR_PY, "d.tar", swap=[("/src", str(src)), ("/bak/", f"{bak}/")]).returncode, 0)
        self.assertEqual(py(self.smoke.UNTAR_PY, "d.tar", swap=[("/dst", str(dst)), ("/bak/", f"{bak}/")]).returncode, 0)
        ok = py(self.smoke.COMPARE_PY, str(src), str(dst), "gateway.db")
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "equal 2"), ok.stderr)
        (dst / "vault/30_Conversations/note.md").write_text("tampered\n")
        self.assertNotEqual(py(self.smoke.COMPARE_PY, str(src), str(dst)).returncode, 0)
        (dst / "vault/30_Conversations/note.md").write_text("synthetic\n")
        (dst / "extra.md").write_text("x")
        self.assertNotEqual(py(self.smoke.COMPARE_PY, str(src), str(dst)).returncode, 0)
        (dst / "extra.md").unlink()
        (src / "gateway.db").write_bytes(b"not sqlite")
        (dst / "gateway.db").write_bytes(b"not sqlite")
        self.assertNotEqual(py(self.smoke.COMPARE_PY, str(src), str(dst), "gateway.db").returncode, 0)  # integrity

    def test_test_only_transport_module_patches_only_what_it_is_mounted_for(self):
        (self.t / "sitecustomize.py").write_text(self.smoke.SITECUSTOMIZE.format(remote="file:///seed/vault.git"))
        probe = ("from gateway import onboarding as o;"
                 "print(o.ALLOWED_PROTOCOLS, o.remote_url('git@github.com:a/b.git'))")
        env = {**os.environ, "PYTHONPATH": f"{self.t}{os.pathsep}{ROOT}"}
        patched = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, env=env, cwd=self.t)
        self.assertEqual(patched.stdout.strip(), "file file:///seed/vault.git", patched.stderr)
        env["PYTHONPATH"] = str(ROOT)  # without the mounted module the Gateway only speaks SSH to the URL it was given
        plain = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, env=env, cwd=self.t)
        self.assertEqual(plain.stdout.strip(), "ssh git@github.com:a/b.git", plain.stderr)
        for source in (ROOT / "gateway").glob("*.py"):
            text = source.read_text(encoding="utf-8")
            for needle in ("PVG_SMOKE", "sitecustomize", "file://"):
                self.assertNotIn(needle, text, f"{source.name}: production code must not know about the smoke test")

    def test_smoke_never_mounts_a_docker_socket_and_copies_without_a_network(self):
        source = (ROOT / "tests/test_compose_smoke.py").read_text()
        self.assertNotIn("/var/run", source)
        self.assertNotIn("docker.sock\"", source.replace("docker.sock\" in", ""))
        self.assertIn('"--network", "none"', source)  # volume copies and the seed repository run without a network
        for gone in ("persona-vault-sync", "persona-vault-init", "secrets/", "VAULT_REPO_SSH_URL", "ADMIN_PASSWORD"):
            self.assertNotIn(gone, source)


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh-keygen"), "git and ssh-keygen needed: SKIPPED, not passed")
class NativeJourneyTest(unittest.TestCase):
    """The smoke test's whole HTTP journey against `python -m gateway.server` running natively (no Docker).

    Same Journey class, same test-only transport module, a local bare repository and temp directories."""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        self.smoke = load_smoke()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Path(tmp.name).resolve()
        self.proc = None
        self.addCleanup(self.stop)
        for name in ("py", "home", "data"):
            (self.t / name).mkdir()
        self.remote = self.t / "vault.git"
        (self.t / "py/sitecustomize.py").write_text(self.smoke.SITECUSTOMIZE.format(remote=f"file://{self.remote}"))
        self.git_env = {"PATH": os.environ["PATH"], "HOME": str(self.t / "home"), "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}
        self.git("init", "--bare", "-b", "main", str(self.remote))
        self.git("clone", str(self.remote), str(self.t / "w"))
        (self.t / "w/00_Inbox").mkdir()
        (self.t / "w" / self.smoke.NOTE).write_text(f"# note\n\nSynthetic {self.smoke.QUERY} sentence.\n")
        self.git("add", ".", cwd=self.t / "w")
        self.git("commit", "-m", "note", cwd=self.t / "w")
        self.git("push", "origin", "HEAD:main", cwd=self.t / "w")
        with socket_free_port() as port:
            self.port = port
        self.token = "native-journey-setup-code-0123456789"
        self.password = "Synthetic$Pass-0123456789"
        self.env = {**self.git_env, "PYTHONPATH": f"{self.t / 'py'}{os.pathsep}{ROOT}", "PORT": str(self.port),
                    "PVG_DATA_DIR": str(self.t / "data"), "VAULT_DIR": str(self.t / "data/vault"),
                    "DB_PATH": str(self.t / "data/gateway.db"), "EMBEDDING_PROVIDER": "none",
                    "PVG_SETUP_TOKEN": self.token, "VAULT_SYNC_INTERVAL_SECONDS": "3600", "HOST_ID": "native-journey"}

    # --- Journey target ---
    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd, env=self.git_env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def py(self, code):
        r = subprocess.run([sys.executable, "-c", code], env=self.env, cwd=self.t, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return r.stdout

    def remote_files(self):
        return set(self.git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main").split())

    def remote_commit(self):
        self.git("clone", str(self.remote), str(self.t / "o"))
        (self.t / "o" / self.smoke.REMOTE_NOTE).write_text("# Remote note\n")
        self.git("add", ".", cwd=self.t / "o")
        self.git("commit", "-m", "remote", cwd=self.t / "o")
        self.git("push", "origin", "HEAD:main", cwd=self.t / "o")

    # --- process ---
    def start(self):
        self.log = open(self.t / "server.log", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "gateway.server"], cwd=ROOT, env=self.env,
                                     stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if getattr(self, "log", None):
            self.log.close()

    def test_whole_journey_then_restart_then_restore_check(self):
        smoke = self.smoke
        journey = smoke.Journey(f"http://127.0.0.1:{self.port}", self, self.password)
        self.start()
        try:
            smoke.wait(journey.live, "the native Gateway", 60)
            journey.pending()
            journey.claim(self.token)
            journey.connect_vault()
            self.assertEqual(journey.web.json("GET", "/readyz")[1]["semantic"], "disabled")
            journey.agent_token()
            captured, digest = journey.use()
            journey.sync_roundtrip(captured)
            self.stop()
            self.start()
            journey.after_restart(captured, digest)
            self.stop()
            restored = subprocess.run([sys.executable, "-c", smoke.RESTORE_PY, captured],
                                      env={**self.env, "PVG_SMOKE_PASSWORD": self.password}, cwd=self.t,
                                      capture_output=True, text=True, timeout=60)
            self.assertEqual(restored.stdout.strip(), "restored ok", restored.stderr[-800:])
        except Exception:
            print((self.t / "server.log").read_text(errors="replace")[-3000:])
            raise


@contextlib.contextmanager
def socket_free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        yield s.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
