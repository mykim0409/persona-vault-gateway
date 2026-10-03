"""Tests for gateway.bootstrap (first-run setup).

Temp directories and synthetic data only; no network, no real Git host, no Docker daemon. `git ls-remote` is
simulated; the real ssh-keygen is used only when it is installed.
"""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import bootstrap  # noqa: E402

URL = "git@github.com:synthetic-owner/synthetic-vault.git"
PASSWORD = "Synthetic$HOME-pass#9"  # contains $ on purpose
HAS_KEYGEN = shutil.which("ssh-keygen") is not None


def no_input(*_):
    raise AssertionError("must not prompt")


class Prompts:
    """Scripted answers for input()/getpass()."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, _prompt):
        return self.answers.pop(0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "setup"
        self.root.mkdir()
        self.paths = bootstrap.Paths(self.root)

    def main(self, argv=(), *, input_fn=no_input, getpass_fn=no_input, run=None):
        out, err = io.StringIO(), io.StringIO()
        kwargs = {"run": run} if run else {}
        code = bootstrap.main([*argv, "--setup-dir", str(self.root)], input_fn=input_fn, getpass_fn=getpass_fn,
                              out=out, err=err, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def init(self, url=URL, password=PASSWORD, **kw):
        return self.main(input_fn=Prompts(url), getpass_fn=Prompts(password, password), **kw)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                for p in sorted(self.root.rglob("*")) if p.is_file()}


class ValidationTest(unittest.TestCase):
    def test_vault_url_accepts_only_github_ssh(self):
        for ok in (URL, "git@github.com:o/r", "ssh://git@github.com/o/r.git", "git@github.com:o-1/my_repo.v2.git"):
            self.assertEqual(bootstrap.validate_vault_url(ok), ok)
        bad = ("", "   ", "https://github.com/o/r.git", "git@gitlab.com:o/r.git", "git@github.com.evil.example:o/r.git",
               "git@github.com:o", "git@github.com:o/r/extra.git", "git@github.com:o/..", "git@github.com:o/.git",
               "git@github.com:-o/r.git", "ssh://git@github.com:2222/o/r.git", "github.com:o/r.git",
               "git@github.com:o/r.git; touch /tmp/x", "git@github.com:o/r.git\nfoo", "git@github.com:o/r.git foo",
               "git@github.com:o/$(id).git", "git@github.com:o/`id`.git", "-oProxyCommand=id", "ext::sh -c id",
               "file:///etc/passwd", "/srv/git/vault.git")
        for text in bad:
            with self.subTest(url=text), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.validate_vault_url(text)

    def test_other_hosts_get_manual_path_guidance(self):
        with self.assertRaisesRegex(bootstrap.BootstrapError, "github_known_hosts"):
            bootstrap.validate_vault_url("git@gitlab.com:o/r.git")

    def test_password_rules(self):
        self.assertEqual(bootstrap.validate_password(PASSWORD), PASSWORD)
        bad = ("", "short-1", "a" * 16, " leading-space-password1", "trailing-space-password1 ",
               "has\nnewline-in-password1", "has\x00nul-in-password-1", "has'quote-in-password-1", "x" * 129)
        for text in bad:
            with self.subTest(length=len(text)), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.validate_password(text)

    def test_env_quote_is_literal_and_round_trips(self):
        for value in ("a$b", "$(id)", "${HOME}", "x#y", 'say "hi"', "back`tick`", "back\\slash", "sp ace", "=eq"):
            with self.subTest(value=value):
                self.assertEqual(bootstrap.env_quote(value), f"'{value}'")
        for value in ("it's", "line\nbreak", "nul\x00"):
            with self.subTest(value=value), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.env_quote(value)

    def test_read_env_value_never_sources_or_expands(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("# VAULT_REPO_SSH_URL=commented\nexport OTHER=1\nVAULT_REPO_SSH_URL=old\n"
                           "VAULT_REPO_SSH_URL='git@github.com:o/$(touch pwned).git'\n")
            self.assertEqual(bootstrap.read_env_value(env, "VAULT_REPO_SSH_URL"), "git@github.com:o/$(touch pwned).git")
            self.assertIsNone(bootstrap.read_env_value(env, "MISSING"))
            self.assertFalse((Path(d) / "pwned").exists())

    def test_pinned_github_host_key_matches_official_fingerprint(self):
        self.assertEqual(bootstrap.GITHUB_ED25519_SHA256, "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU")
        line = bootstrap.github_known_hosts_line()
        self.assertEqual(line, f"github.com ssh-ed25519 {bootstrap.GITHUB_ED25519_KEY}\n")
        original = bootstrap.GITHUB_ED25519_KEY
        self.addCleanup(setattr, bootstrap, "GITHUB_ED25519_KEY", original)
        bootstrap.GITHUB_ED25519_KEY = original[:-4] + "AAAA"  # any tampered key must be refused
        with self.assertRaisesRegex(bootstrap.BootstrapError, "pinned fingerprint"):
            bootstrap.github_known_hosts_line()


@unittest.skipUnless(HAS_KEYGEN, "ssh-keygen not installed on this machine: init tests SKIPPED, not passed")
class InitTest(Base):
    def test_creates_files_with_safe_modes_and_prints_no_secret(self):
        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        mode = lambda p: stat.S_IMODE(p.stat().st_mode)
        self.assertEqual(mode(self.paths.env), 0o600)
        self.assertEqual(mode(self.paths.secrets), 0o700)
        self.assertEqual(mode(self.paths.key), 0o600)
        self.assertEqual(self.paths.known_hosts.read_text(), bootstrap.github_known_hosts_line())
        pub = self.paths.pub.read_text().strip()
        self.assertRegex(pub, r"^ssh-ed25519 [A-Za-z0-9+/=]+ persona-vault-sync$")
        self.assertIn(pub, out)  # only the public key is shown
        private = self.paths.key.read_text()
        self.assertIn("OPENSSH PRIVATE KEY", private)
        for secret in (PASSWORD, private.splitlines()[1]):
            self.assertNotIn(secret, out + err)
        self.assertEqual(sorted(p.name for p in self.paths.secrets.iterdir()),
                         ["github_known_hosts", "persona_vault_sync", "persona_vault_sync.pub"])  # scratch dir removed

    def test_env_is_proxy_neutral_keyword_only_and_literal(self):
        self.assertEqual(self.init()[0], 0)
        env = self.paths.env.read_text()
        keys = ("VAULT_REPO_SSH_URL", "ADMIN_PASSWORD", "GATEWAY_BIND_ADDR", "EMBEDDING_PROVIDER",
                "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")
        values = {k: bootstrap.read_env_value(self.paths.env, k) for k in keys}
        self.assertEqual(values, {"VAULT_REPO_SSH_URL": URL, "ADMIN_PASSWORD": PASSWORD,
                                  "GATEWAY_BIND_ADDR": "127.0.0.1", "EMBEDDING_PROVIDER": "none",
                                  "CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_API_TOKEN": ""})
        self.assertIn(f"ADMIN_PASSWORD='{PASSWORD}'", env)
        active = "\n".join(l for l in env.splitlines() if not l.startswith("#")).lower()
        for forbidden in ("compose_file", "compose_profiles", "public_ip", "https", "caddy", "acme"):
            self.assertNotIn(forbidden, active)

    def test_output_makes_no_remote_access_or_encryption_claim(self):
        _, out, _ = self.init()
        self.assertIn("provides no remote access or encryption", out)
        self.assertIn("127.0.0.1:18080", out)
        self.assertIn("persona-vault-init --check", out)
        for forbidden in ("caddy", "acme", "let's encrypt", "certificate", "ready for remote"):
            self.assertNotIn(forbidden, out.lower())

    def test_no_https_or_public_ip_options_exist(self):
        for flag in ("--https-ip", "--public-ip"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.main([flag, "203.0.113.10"])
        self.assertEqual(self.snapshot(), {})
        source = (ROOT / "gateway/bootstrap.py").read_text().lower()
        for word in ("caddy", "acme", "ipaddress", "public_ip", "compose_file"):
            self.assertNotIn(word, source)

    def test_vault_url_flag_skips_only_the_url_prompt(self):
        code, _, err = self.main(["--vault-url", URL], getpass_fn=Prompts(PASSWORD, PASSWORD))
        self.assertEqual(code, 0, err)
        self.assertEqual(bootstrap.read_env_value(self.paths.env, "VAULT_REPO_SSH_URL"), URL)

    def test_invalid_input_writes_nothing(self):
        cases = {
            "bad url": dict(input_fn=Prompts("https://github.com/o/r", "git@gitlab.com:o/r", "x"), getpass_fn=no_input),
            "short password": dict(input_fn=Prompts(URL), getpass_fn=Prompts("short", "short", "short")),
            "mismatch": dict(input_fn=Prompts(URL), getpass_fn=Prompts(*[PASSWORD, PASSWORD + "x"] * 3)),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                code, out, err = self.main(**kw)
                self.assertEqual(code, 1)
                self.assertIn("ERROR:", err)
                self.assertNotIn(PASSWORD, out + err)
                self.assertEqual(self.snapshot(), {})
                self.assertFalse(self.paths.secrets.exists())

    def test_rerun_is_idempotent_and_never_prompts(self):
        self.assertEqual(self.init()[0], 0)
        before = self.snapshot()
        code, out, err = self.main()  # no_input fails the test if any prompt is attempted
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("kept existing .env (not read or modified)", out)
        self.assertIn(self.paths.pub.read_text().strip(), out)

    def test_existing_env_is_never_parsed_or_changed(self):
        weird = "ADMIN_PASSWORD=$(touch pwned)\nnot a dotenv line\n\x00\n"
        self.paths.env.write_text(weird)
        os.chmod(self.paths.env, 0o640)
        code, _, err = self.main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.paths.env.read_text(), weird)
        self.assertEqual(stat.S_IMODE(self.paths.env.stat().st_mode), 0o640)
        self.assertFalse((self.root / "pwned").exists())
        self.assertTrue(self.paths.key.exists())  # the credential files are still created

    def test_existing_key_and_known_hosts_are_not_overwritten(self):
        self.paths.secrets.mkdir()
        self.paths.key.write_text("existing-private\n")
        self.paths.known_hosts.write_text("custom host list\n")
        self.paths.pub.write_text("ssh-ed25519 EXISTING custom\n")
        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.paths.key.read_text(), "existing-private\n")
        self.assertEqual(self.paths.known_hosts.read_text(), "custom host list\n")
        self.assertEqual(self.paths.pub.read_text(), "ssh-ed25519 EXISTING custom\n")
        self.assertIn("WARNING", out)  # known_hosts lacks the pinned key

    def test_missing_public_key_is_derived_from_the_private_key(self):
        self.assertEqual(self.init()[0], 0)
        original = self.paths.pub.read_text()
        private = self.paths.key.read_bytes()
        self.paths.pub.unlink()
        code, out, err = self.main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.paths.pub.read_text(), original)
        self.assertEqual(self.paths.key.read_bytes(), private)
        self.assertEqual(stat.S_IMODE(self.paths.pub.stat().st_mode), 0o644)
        self.assertIn(original.strip(), out)

    def test_symlinks_fail_without_touching_the_target(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.write_text("keep\n")
        layouts = {
            ".env": lambda: (self.root / ".env").symlink_to(victim),
            "secrets dir": lambda: (self.root / "secrets").symlink_to(outside),
            "key": lambda: (self.paths.secrets.mkdir(), self.paths.key.symlink_to(victim)),
            "pub": lambda: (self.paths.secrets.mkdir(), self.paths.pub.symlink_to(victim)),
            "known_hosts": lambda: (self.paths.secrets.mkdir(), self.paths.known_hosts.symlink_to(victim)),
            "dangling .env": lambda: (self.root / ".env").symlink_to(outside / "does-not-exist"),
        }
        for name, make in layouts.items():
            with self.subTest(name):
                shutil.rmtree(self.root)
                self.root.mkdir()
                make()
                code, out, err = self.init()
                self.assertEqual(code, 1)
                self.assertIn("symlink", err)
                self.assertEqual(victim.read_text(), "keep\n")
                self.assertEqual(sorted(p.name for p in outside.iterdir()), ["victim"])

    def test_secrets_path_that_is_a_file_fails(self):
        self.paths.secrets.write_text("not a dir")
        code, _, err = self.init()
        self.assertEqual(code, 1)
        self.assertIn("not a directory", err)

    def test_ssh_keygen_failure_is_reported_without_leaking_its_output(self):
        def failing(cmd, **kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom " + PASSWORD)

        code, out, err = self.init(run=failing)
        self.assertEqual(code, 1)
        self.assertNotIn(PASSWORD, out + err)
        self.assertFalse(self.paths.key.exists())


class CheckTest(Base):
    """`--check` with a simulated git: only the arguments and environment it passes are inspected."""

    def setUp(self):
        super().setUp()
        self.paths.secrets.mkdir()
        self.paths.env.write_text(f"VAULT_REPO_SSH_URL='{URL}'\nADMIN_PASSWORD='{PASSWORD}'\n")
        for path in (self.paths.key, self.paths.known_hosts):
            path.write_text("synthetic\n")
        self.calls = []

    def fake(self, returncode=0, stdout="", stderr="", exc=None):
        def run(cmd, **kw):
            self.calls.append((cmd, kw))
            if exc:
                raise exc
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

        return run

    def test_success_is_read_only_pinned_and_bounded(self):
        before = self.snapshot()
        code, out, err = self.main(["--check"], run=self.fake(stdout="abc123\trefs/heads/main\n"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("CHECK OK", out)
        self.assertIn("cannot prove the key has write access", out)
        (cmd, kw), = self.calls
        self.assertEqual(cmd, ["git", "ls-remote", "--heads", URL])
        self.assertGreater(kw["timeout"], 0)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        ssh = kw["env"]["GIT_SSH_COMMAND"]
        for part in (str(self.paths.key), f"UserKnownHostsFile={self.paths.known_hosts}", "GlobalKnownHostsFile=/dev/null",
                     "StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes", "ConnectTimeout="):
            self.assertIn(part, ssh)
        self.assertNotIn(PASSWORD, json.dumps(kw["env"]) + " ".join(cmd) + out + err)
        self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(self.snapshot(), before)

    def test_empty_repository_gives_initialization_guidance(self):
        code, out, err = self.main(["--check"], run=self.fake(stdout=""))
        self.assertEqual(code, 1)
        self.assertIn("EMPTY", err)
        self.assertIn("README", err)
        self.assertEqual(out, "")

    def test_failures_are_classified_and_never_dump_secrets(self):
        cases = {
            "Permission denied (publickey).\nfatal: Could not read from remote repository.": "deploy key is not registered",
            "ERROR: Repository not found.": "repository name is wrong",
            "Host key verification failed.": "did not match the pinned GitHub key",
            "ssh: Could not resolve hostname github.com": "see the message below",
        }
        for stderr, hint in cases.items():
            with self.subTest(stderr=stderr[:30]):
                code, out, err = self.main(["--check"], run=self.fake(returncode=128, stderr=stderr))
                self.assertEqual(code, 1)
                self.assertIn("CHECK FAILED", err)
                self.assertIn(hint, err)
                self.assertNotIn(PASSWORD, out + err)

    def test_timeout_fails_cleanly(self):
        code, _, err = self.main(["--check"], run=self.fake(exc=subprocess.TimeoutExpired("git", 30)))
        self.assertEqual(code, 1)
        self.assertIn("CHECK FAILED: no answer", err)

    def test_missing_files_symlinks_and_bad_url_fail_before_running_git(self):
        for name in ("known_hosts", "key", "env"):
            with self.subTest(missing=name):
                path = getattr(self.paths, name)
                saved = path.read_bytes()
                path.unlink()
                code, _, err = self.main(["--check"], run=self.fake())
                self.assertEqual(code, 1)
                self.assertIn("missing", err)
                path.write_bytes(saved)
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("x")
        self.paths.key.unlink()
        self.paths.key.symlink_to(victim)
        code, _, err = self.main(["--check"], run=self.fake())
        self.assertEqual((code, "symlink" in err), (1, True))
        self.paths.key.unlink()
        self.paths.key.write_text("synthetic\n")
        self.paths.env.write_text("VAULT_REPO_SSH_URL='git@github.com:o/r.git; id'\n")
        code, _, err = self.main(["--check"], run=self.fake())
        self.assertEqual(code, 1)
        self.assertIn("Not an accepted Vault URL", err)
        self.paths.env.write_text("ADMIN_PASSWORD='x'\n")
        self.assertEqual(self.main(["--check"], run=self.fake())[0], 1)
        self.assertEqual(self.calls, [])  # git was never started

    def test_symlinked_secrets_directory_is_rejected_even_with_real_files_behind_it(self):
        outside = Path(self.tmp.name) / "outside"
        self.paths.secrets.rename(outside)  # real key and known_hosts now live outside the project
        self.paths.secrets.symlink_to(outside)
        before = {p.name: p.read_bytes() for p in outside.iterdir()}
        code, out, err = self.main(["--check"], run=self.fake(stdout="a\tb\n"))
        self.assertEqual(code, 1)
        self.assertIn("secrets is a symlink", err)
        self.assertEqual(self.calls, [])  # git never started
        self.assertEqual({p.name: p.read_bytes() for p in outside.iterdir()}, before)

    def test_symlinked_env_is_rejected_by_check(self):
        real = Path(self.tmp.name) / "real.env"
        self.paths.env.rename(real)
        self.paths.env.symlink_to(real)
        code, _, err = self.main(["--check"], run=self.fake(stdout="a\tb\n"))
        self.assertEqual((code, ".env is a symlink" in err, self.calls), (1, True, []))

    def test_check_rejects_other_options_and_reads_env_without_sourcing(self):
        self.paths.env.write_text(f"X=$(touch {self.root}/pwned)\nVAULT_REPO_SSH_URL='{URL}'\n")
        self.assertEqual(self.main(["--check"], run=self.fake(stdout="a\tb\n"))[0], 0)
        self.assertFalse((self.root / "pwned").exists())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.main(["--check", "--vault-url", URL])


@unittest.skipUnless(HAS_KEYGEN, "ssh-keygen not installed on this machine: SKIPPED, not passed")
class ComposeInterpolationTest(Base):
    """With a Compose v2 binary, a generated .env must reach the container literally ($ is not interpolated)."""

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI not installed: SKIPPED, not passed")
    def test_compose_reads_generated_env_literally(self):
        if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode:
            self.skipTest("Docker Compose v2 plugin not available: SKIPPED, not passed")
        self.assertEqual(self.init()[0], 0)
        (self.root / "compose.yml").write_text('services:\n  s:\n    image: x\n    environment:\n'
                                               '      ADMIN_PASSWORD: "${ADMIN_PASSWORD:-}"\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "ADMIN_PASSWORD"))}
        r = subprocess.run(["docker", "compose", "--env-file", str(self.paths.env), "-f", str(self.root / "compose.yml"),
                            "config", "--format", "json"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        # `config` re-escapes `$` as `$$` so its output can be parsed again; an interpolated $HOME would differ.
        rendered = json.loads(r.stdout)["services"]["s"]["environment"]["ADMIN_PASSWORD"]
        self.assertEqual(rendered, PASSWORD.replace("$", "$$"))


if __name__ == "__main__":
    unittest.main()
