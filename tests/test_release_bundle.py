"""Tests for scripts/build_release_bundle.py and the release workflow contract.

Synthetic source trees, a fake digest and temp output only: no network, no Docker, no registry, no real secrets.
"""
import contextlib
import hashlib
import io
import posixpath
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_release_bundle as rb  # noqa: E402

DIGEST = "sha256:" + "ab" * 32
IMAGE = f"ghcr.io/synthetic-owner/persona-vault-gateway@{DIGEST}"
TAG = "gateway-v1.2.3"
SECRET = "SYNTHETIC-NOT-A-REAL-SECRET"
COMPOSE = f"""services:
  persona-vault-gateway:
    image: "${{PVG_IMAGE:-persona-vault-gateway:local}}"
  qdrant:
    image: qdrant/qdrant@sha256:{"11" * 32}
  persona-vault-init:
    image: "${{PVG_IMAGE:-persona-vault-gateway:local}}"
"""


def make_source(root, version="1.2.3", compose=COMPOSE):
    """Synthetic tree: allowlisted files plus things that must never ship."""
    root = Path(root)
    (root / "docs").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    for rel in rb.ALLOWLIST:
        (root / rel).write_text(f"synthetic {rel}\n")
    (root / "compose.yml").write_text(compose)
    (root / "secrets").mkdir()
    (root / "secrets" / "persona_vault_sync").write_text(SECRET)
    for rel in (".env", "personal-setup.md", "docs/personal.md", "docs/WORKING_AGREEMENT.md", "data.db"):
        (root / rel).write_text(SECRET)
    return root


def members(archive):
    with tarfile.open(archive, "r:gz") as tar:
        return {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}


class BundleCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.src = make_source(self.tmp / "src")
        self.out = self.tmp / "out"

    def build(self, **kw):
        args = dict(root=self.src, tag=TAG, image=IMAGE, output_dir=self.out)
        args.update(kw)
        return rb.build_bundle(**args)


class ContentTests(BundleCase):
    def test_only_allowlisted_files_and_no_secrets(self):
        archive, _ = self.build()
        base = "persona-vault-gateway-1.2.3"
        got = members(archive)
        files = {n for n, d in got.items() if d is not None}
        self.assertEqual(files, {f"{base}/{rel}" for rel in rb.ALLOWLIST})
        for name, data in got.items():
            self.assertNotIn(SECRET.encode(), data or b"", name)
        self.assertNotIn(".env", {n.rsplit("/", 1)[-1] for n in got})
        for bad in ("secrets", "personal", "WORKING_AGREEMENT", "data.db", "pyproject"):
            self.assertFalse([n for n in got if bad in n], bad)

    def test_compose_pins_gateway_and_init_to_digest(self):
        archive, _ = self.build()
        compose = members(archive)["persona-vault-gateway-1.2.3/compose.yml"].decode()
        self.assertEqual(compose.count("${PVG_IMAGE:-" + IMAGE + "}"), 2)
        self.assertNotIn("persona-vault-gateway:local", compose)
        self.assertNotIn(":latest", compose)
        for line in compose.splitlines():
            if line.strip().startswith("image:"):
                self.assertIn("@sha256:", line)

    def test_compose_with_wrong_default_count_is_rejected(self):
        for text in (COMPOSE.replace("${PVG_IMAGE:-persona-vault-gateway:local}", "x", 1), COMPOSE + COMPOSE):
            with self.assertRaises(rb.ReleaseError):
                rb.pin_compose(text, IMAGE)
        with self.assertRaises(rb.ReleaseError):
            rb.pin_compose(COMPOSE + "  extra:\n    image: example/app:1\n", IMAGE)

    def test_checksum_matches_and_names(self):
        archive, checksum = self.build()
        self.assertEqual(archive.name, "persona-vault-gateway-1.2.3-install.tar.gz")
        self.assertEqual(checksum.name, archive.name + ".sha256")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(checksum.read_text(), f"{digest}  {archive.name}\n")

    def test_archive_is_deterministic(self):
        first, _ = self.build()
        data = first.read_bytes()
        (self.src / "LICENSE").touch()  # new mtime must not change the bytes
        second, _ = self.build(output_dir=self.tmp / "out2")
        self.assertEqual(data, second.read_bytes())
        with tarfile.open(first, "r:gz") as tar:
            for m in tar.getmembers():
                self.assertEqual((m.mtime, m.uid, m.gid, m.uname, m.gname), (0, 0, 0, "", ""))
                self.assertEqual(m.mode, 0o755 if m.isdir() else 0o644)
        self.assertEqual(data[4:8], b"\x00\x00\x00\x00")  # gzip mtime

    def test_symlinked_or_missing_allowlist_file_is_rejected(self):
        (self.src / ".env.example").unlink()
        (self.src / ".env.example").symlink_to(self.src / ".env")
        with self.assertRaises(rb.ReleaseError):
            self.build()
        (self.src / ".env.example").unlink()
        with self.assertRaises(rb.ReleaseError):
            self.build()
        self.assertFalse(self.out.exists())

    def test_real_allowlist_exists_and_real_compose_pins(self):
        text = (ROOT / "compose.yml").read_text()
        self.assertEqual(text.count(rb.UNPINNED), 2)
        self.assertIn("${PVG_IMAGE:-" + IMAGE + "}", rb.pin_compose(text, IMAGE))
        for rel in rb.ALLOWLIST:
            self.assertTrue((ROOT / rel).is_file(), rel)


class ValidationTests(BundleCase):
    def test_invalid_tags(self):
        for tag in ("v1.2.3", "gateway-1.2.3", "gateway-v1.2", "gateway-v1.2.3-rc1", "gateway-v01.2.3", "gateway-v1.2.3\n",
                    "gateway-v1.2.3 ", "latest", "main", "refs/tags/gateway-v1.2.3", "gateway-v1.2.3/../x", "",
                    "gateway-v١.٢.٣"):
            with self.subTest(tag=tag), self.assertRaises(rb.ReleaseError):
                self.build(tag=tag)
        self.assertFalse(self.out.exists())

    def test_package_version_mismatch(self):
        with self.assertRaises(rb.ReleaseError):
            self.build(tag="gateway-v1.2.4")
        self.assertFalse(self.out.exists())

    def test_invalid_images(self):
        for image in ("persona-vault-gateway:local", f"ghcr.io/o/n:latest", f"ghcr.io/o/n:1.2.3", f"ghcr.io/o/n@{DIGEST}x",
                      f"ghcr.io/o/n@sha256:{'ab' * 31}", f"ghcr.io/o/n@sha256:{'AB' * 32}", f"docker.io/o/n@{DIGEST}",
                      f"ghcr.io/O/N@{DIGEST}", f"ghcr.io/n@{DIGEST}", f"ghcr.io/o/n@{DIGEST}\n", f" ghcr.io/o/n@{DIGEST}",
                      f"ghcr.io/o/n@{DIGEST}}}", f"ghcr.io/o/n@{DIGEST} x", "", f"ghcr.io/o//n@{DIGEST}",
                      f"ghcr.io/o/n:1@{DIGEST}"):
            with self.subTest(image=image), self.assertRaises(rb.ReleaseError):
                self.build(image=image)
        self.assertFalse(self.out.exists())

    def test_cli(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = rb.main(["--source-root", str(self.src), *argv])
            return code, out.getvalue(), err.getvalue()

        self.assertEqual(run("--tag", TAG, "--check-only"), (0, "1.2.3\n", ""))
        code, _, err = run("--tag", "gateway-v9.9.9", "--check-only")
        self.assertEqual(code, 2)
        self.assertIn("does not match", err)
        self.assertEqual(run("--tag", TAG, "--output-dir", str(self.out))[0], 2)  # --image missing
        self.assertFalse(self.out.exists())
        code, stdout, _ = run("--tag", TAG, "--image", IMAGE, "--output-dir", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [Path(l).name for l in sorted(stdout.split())])


class WorkflowContractTests(unittest.TestCase):
    """Text checks on the workflow files (no YAML dependency, nothing is executed)."""

    def setUp(self):
        self.release = (ROOT / ".github/workflows/release.yml").read_text()
        self.ci = (ROOT / ".github/workflows/ci.yml").read_text()

    def test_release_trigger_is_manual_only(self):
        on = self.release.split("\njobs:")[0]
        self.assertIn("workflow_dispatch:", on)
        for trigger in ("push:", "pull_request", "schedule:", "release:", "workflow_run:"):
            self.assertNotIn(trigger, on)

    def test_release_uses_only_github_token_and_no_mutable_image(self):
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", self.release)), {"GITHUB_TOKEN"})
        self.assertNotIn(":latest", self.release)
        self.assertNotIn("ssh", self.release.lower())
        for action in ("docker/login-action@v4", "docker/setup-qemu-action@v4", "docker/setup-buildx-action@v4",
                       "docker/build-push-action@v7"):
            self.assertIn(action, self.release)
        self.assertIn("platforms: linux/amd64,linux/arm64", self.release)
        self.assertIn("--draft", self.release)

    def test_release_runs_ci_and_ci_keeps_triggers_and_jobs(self):
        self.assertIn("uses: ./.github/workflows/ci.yml", self.release)
        for text in ("pull_request:", "push:", "workflow_dispatch:", "workflow_call:", "  test:", "  compose-smoke:",
                     "  windows-client:", "tests/test_release_bundle.py", "tests/test_bootstrap.py"):
            self.assertIn(text, self.ci)

    def test_release_notes_remind_about_public_package_and_failures_are_not_masked(self):
        for text in ("GHCR package must be public", "anonymous", "does not make the package public"):
            self.assertIn(text, self.release)
        self.assertNotIn("! grep", self.release)  # `! cmd` never trips `set -e`
        self.assertIn('docker --config "\\$cfg" pull', self.release)
        self.assertNotIn("docker logout", self.release)


class BundleDocLinkTests(unittest.TestCase):
    """Relative Markdown links in the shipped docs must point at other shipped files."""

    def test_local_links_stay_inside_the_bundle(self):
        shipped = set(rb.ALLOWLIST)
        for rel in sorted(shipped):
            if not rel.endswith(".md"):
                continue
            text = (ROOT / rel).read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                target = target.split("#")[0].split("?")[0]
                if not target or re.match(r"[a-z][a-z0-9+.-]*:", target, re.I):
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
                with self.subTest(doc=rel, link=target):
                    self.assertIn(resolved, shipped)


if __name__ == "__main__":
    unittest.main()
