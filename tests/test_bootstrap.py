"""Tests for gateway.bootstrap (SSH/URL helpers used by browser onboarding).

Temp directories and synthetic data only; no network, no Git host. The real ssh-keygen is used only when installed.
"""
import base64
import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import bootstrap  # noqa: E402

URL = "git@github.com:synthetic-owner/synthetic-vault.git"
HAS_KEYGEN = shutil.which("ssh-keygen") is not None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "setup"
        self.root.mkdir()
        self.paths = bootstrap.Paths(self.root)


class ValidationTest(unittest.TestCase):
    def test_vault_url_accepts_only_github_ssh(self):
        for ok in (URL, "git@github.com:o/r", "ssh://git@github.com/o/r.git", "git@github.com:o-1/my_repo.v2.git"):
            self.assertEqual(bootstrap.validate_vault_url(ok), ok)
        bad = ("", "   ", "https://github.com/o/r.git", "git@gitlab.com:o/r.git", "git@github.com.evil.example:o/r.git",
               "git@github.com:o", "git@github.com:o/r/extra.git", "git@github.com:o/..", "git@github.com:o/.git",
               "git@github.com:-o/r.git", "ssh://git@github.com:2222/o/r.git", "github.com:o/r.git",
               "git@github.com:o/r.git; touch /tmp/x", "git@github.com:o/r.git\nfoo", "git@github.com:o/r.git foo",
               "git@github.com:o/$(id).git", "git@github.com:o/`id`.git", "-oProxyCommand=id", "ext::sh -c id",
               "file:///etc/passwd", "/srv/git/vault.git", None)
        for text in bad:
            with self.subTest(url=text), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.validate_vault_url(text)

    def test_rejection_does_not_tell_users_to_edit_files(self):
        with self.assertRaises(bootstrap.BootstrapError) as ctx:
            bootstrap.validate_vault_url("git@gitlab.com:o/r.git")
        self.assertNotIn(".env", str(ctx.exception))
        self.assertNotIn("secrets/", str(ctx.exception))

    def test_pinned_host_key_matches_published_fingerprint(self):
        line = bootstrap.github_known_hosts_line()
        self.assertEqual(line, f"github.com ssh-ed25519 {bootstrap.GITHUB_ED25519_KEY}\n")
        digest = base64.b64encode(hashlib.sha256(base64.b64decode(bootstrap.GITHUB_ED25519_KEY)).digest()).decode()
        self.assertEqual("SHA256:" + digest.rstrip("="), bootstrap.GITHUB_ED25519_SHA256)

    def test_tampered_host_key_is_refused(self):
        original = bootstrap.GITHUB_ED25519_KEY
        bootstrap.GITHUB_ED25519_KEY = original[:-4] + "AAAA"
        try:
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.github_known_hosts_line()
        finally:
            bootstrap.GITHUB_ED25519_KEY = original

    def test_no_interactive_cli_is_left(self):
        for name in ("main", "run_init", "run_check", "render_env", "env_quote", "read_env_value", "ask"):
            self.assertFalse(hasattr(bootstrap, name), name)


class GitEnvTest(Base):
    def test_ssh_is_pinned_and_non_interactive(self):
        env = bootstrap.git_env(self.paths)
        ssh = shlex.split(env["GIT_SSH_COMMAND"])
        self.assertEqual(ssh[0], "ssh")
        for option in ("IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "BatchMode=yes", "GlobalKnownHostsFile=/dev/null",
                       f"UserKnownHostsFile={self.paths.known_hosts}"):
            self.assertIn(option, ssh)
        self.assertEqual(ssh[ssh.index("-i") + 1], str(self.paths.key))
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_SYSTEM"]),
                         ("0", "/dev/null", "/dev/null"))
        self.assertNotIn("ADMIN_PASSWORD", env)


class FilesTest(Base):
    def test_preflight_refuses_symlinks_and_non_directories(self):
        bootstrap.preflight(self.paths)
        target = Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        self.paths.secrets.symlink_to(target, target_is_directory=True)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.preflight(self.paths)
        self.paths.secrets.unlink()
        self.paths.secrets.write_text("not a directory")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.preflight(self.paths)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.preflight(bootstrap.Paths(self.root / "missing"))

    def test_create_file_never_overwrites_or_follows(self):
        self.paths.secrets.mkdir(mode=0o700)
        bootstrap.create_file(self.paths, self.paths.known_hosts, "one\n", 0o644)
        with self.assertRaises(FileExistsError):
            bootstrap.create_file(self.paths, self.paths.known_hosts, "two\n", 0o644)
        self.assertEqual(self.paths.known_hosts.read_text(), "one\n")
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("keep")
        self.paths.pub.symlink_to(victim)
        with self.assertRaises(OSError):
            bootstrap.create_file(self.paths, self.paths.pub, "x\n", 0o644)
        self.assertEqual(victim.read_text(), "keep")

    @unittest.skipUnless(HAS_KEYGEN, "ssh-keygen is not installed")
    def test_key_generation_and_public_key(self):
        self.paths.secrets.mkdir(mode=0o700)
        bootstrap.generate_private_key(self.paths, subprocess.run)
        self.assertEqual(stat.S_IMODE(self.paths.key.stat().st_mode) & 0o077, 0)
        bootstrap.derive_public_key(self.paths, subprocess.run)
        pub = self.paths.pub.read_text()
        self.assertTrue(pub.startswith("ssh-ed25519 ") and pub.strip().endswith(bootstrap.KEY_COMMENT))
        self.assertNotIn("PRIVATE", pub)
        before = self.paths.key.read_bytes()
        with self.assertRaises(FileExistsError):  # an existing public key is never overwritten
            bootstrap.derive_public_key(self.paths, subprocess.run)
        self.assertEqual(self.paths.key.read_bytes(), before)

    def test_keygen_failures_are_generic(self):
        self.paths.secrets.mkdir(mode=0o700)

        def failing(*_a, **_k):
            raise OSError("secret path /x")

        with self.assertRaises(bootstrap.BootstrapError) as ctx:
            bootstrap.generate_private_key(self.paths, failing)
        self.assertNotIn("/x", str(ctx.exception))
        self.assertEqual(list(self.paths.secrets.iterdir()), [])  # scratch dir cleaned up


if __name__ == "__main__":
    unittest.main()
