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
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_release_bundle as rb  # noqa: E402

DIGEST = "sha256:" + "ab" * 32
REPO = "ghcr.io/synthetic-owner/persona-vault-gateway"
IMAGE = f"{REPO}@{DIGEST}"
TAG = "gateway-v1.2.3"
TAGGED = f"{REPO}:{TAG}"
SECRET = "SYNTHETIC-NOT-A-REAL-SECRET"
COMPOSE = f"""services:
  persona-vault-gateway:
    image: "${{PVG_IMAGE:-persona-vault-gateway:local}}"
  qdrant:
    image: qdrant/qdrant@sha256:{"11" * 32}
"""
RENDER = f"services:\n  - type: web\n    image:\n      url: {TAGGED}\n"
RAILWAY = f'const GATEWAY_IMAGE = "{TAGGED}";\n'


def make_source(root, version="1.2.3", compose=COMPOSE, render=RENDER, railway=RAILWAY):
    """Synthetic tree: allowlisted files plus things that must never ship."""
    root = Path(root)
    (root / "docs").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    for rel in rb.ALLOWLIST:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"synthetic {rel}\n")
    (root / "compose.yml").write_text(compose)
    (root / "render.yaml").write_text(render)
    (root / ".railway/railway.ts").write_text(railway)
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

    def test_compose_pins_the_gateway_to_digest(self):
        archive, _ = self.build()
        compose = members(archive)["persona-vault-gateway-1.2.3/compose.yml"].decode()
        self.assertEqual(compose.count("${PVG_IMAGE:-" + IMAGE + "}"), 1)
        self.assertNotIn("persona-vault-gateway:local", compose)
        self.assertNotIn(":latest", compose)
        for line in compose.splitlines():
            if line.strip().startswith("image:"):
                self.assertIn("@sha256:", line)

    def test_compose_with_wrong_default_count_is_rejected(self):
        # The single Gateway service takes exactly one PVG_IMAGE default: none, two, or an unpinned extra image fail.
        self.assertEqual(rb.COMPOSE_IMAGE_COUNTS, {"compose.yml": 1})
        unpinned = "${PVG_IMAGE:-persona-vault-gateway:local}"
        for name, text in (("none", COMPOSE.replace(unpinned, "x")), ("two", COMPOSE + COMPOSE),
                           ("old init service", COMPOSE + f'  persona-vault-init:\n    image: "{unpinned}"\n'),
                           ("extra image", COMPOSE + "  extra:\n    image: example/app:1\n")):
            with self.subTest(case=name):
                (self.src / "compose.yml").write_text(text)
                with self.assertRaises(rb.ReleaseError):
                    self.build()
                with self.assertRaises(rb.ReleaseError):
                    rb.pin_compose(text, IMAGE)
                self.assertFalse(self.out.exists())

    def test_provider_recipes_are_pinned_to_the_digest_exactly_once(self):
        archive, _ = self.build()
        got = members(archive)
        for rel in ("render.yaml", ".railway/railway.ts"):
            text = got[f"persona-vault-gateway-1.2.3/{rel}"].decode()
            self.assertEqual(text.count(IMAGE), 1, rel)
            self.assertNotIn(TAGGED, text, rel)
            self.assertNotIn(f":{TAG}", text, rel)

    def test_provider_recipe_with_wrong_reference_count_or_foreign_tag_is_rejected(self):
        cases = {"render.yaml": (RENDER.replace(TAGGED, f"{REPO}:gateway-v9.9.9"), RENDER + RENDER, "services: []\n"),
                 ".railway/railway.ts": (RAILWAY.replace(TAGGED, "ghcr.io/other/x:" + TAG), RAILWAY + RAILWAY, "")}
        for rel, texts in cases.items():
            for text in texts:
                with self.subTest(rel=rel, text=text[:40]):
                    (self.src / rel).write_text(text)
                    with self.assertRaises(rb.ReleaseError):
                        self.build()
                    self.assertFalse(self.out.exists())
            (self.src / rel).write_text(RENDER if rel == "render.yaml" else RAILWAY)

    def test_standalone_compose_asset_matches_archive_copy_and_checksum(self):
        archive, _ = self.build()
        standalone, checksum = rb.standalone_paths(self.out)
        self.assertEqual((standalone.name, checksum.name), ("compose.yml", "compose.yml.sha256"))
        self.assertEqual(standalone.read_bytes(), members(archive)["persona-vault-gateway-1.2.3/compose.yml"])
        self.assertEqual(checksum.read_text(), f"{hashlib.sha256(standalone.read_bytes()).hexdigest()}  {standalone.name}\n")
        self.assertIn("${PVG_IMAGE:-" + IMAGE + "}", standalone.read_text())

    def test_provider_files_railway_tooling_and_hosting_doc_ship(self):
        for rel in ("docs/hosting.md", "render.yaml", ".railway/railway.ts", "compose.yml"):
            self.assertIn(rel, rb.ALLOWLIST)
        # railway.ts alone is not usable: the pinned tooling that typechecks it ships with it (never node_modules).
        for rel in (".railway/package.json", ".railway/package-lock.json", ".railway/tsconfig.json"):
            self.assertIn(rel, rb.ALLOWLIST)
        self.assertFalse([rel for rel in rb.ALLOWLIST if "node_modules" in rel])
        self.assertNotIn("compose.onboarding.yml", rb.ALLOWLIST)
        self.assertEqual(len(rb.ALLOWLIST), len(set(rb.ALLOWLIST)))

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
        (self.src / "LICENSE").unlink()
        (self.src / "LICENSE").symlink_to(self.src / ".env")
        with self.assertRaises(rb.ReleaseError):
            self.build()
        (self.src / "LICENSE").unlink()
        with self.assertRaises(rb.ReleaseError):
            self.build()
        self.assertFalse(self.out.exists())

    def test_real_allowlist_exists_and_real_compose_pins(self):
        text = (ROOT / "compose.yml").read_text()
        self.assertEqual(text.count(rb.UNPINNED), 1)
        self.assertIn("${PVG_IMAGE:-" + IMAGE + "}", rb.pin_compose(text, IMAGE))
        for rel in rb.ALLOWLIST:
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_real_provider_recipes_reference_the_current_release_tag(self):
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        real = f"ghcr.io/mykim0409/persona-vault-gateway@sha256:{'cd' * 32}"
        for rel in rb.PROVIDER_IMAGE_FILES:
            pinned = rb.pin_provider_image((ROOT / rel).read_text(), real, f"gateway-v{version}", rel)
            self.assertEqual(pinned.count(real), 1, rel)

    def test_shipped_docs_are_paired_with_korean_translations(self):
        shipped = set(rb.ALLOWLIST)
        docs = [rel for rel in rb.ALLOWLIST if rel.endswith(".md") and not rel.endswith(".ko.md")]
        self.assertTrue(docs)
        for rel in docs:
            with self.subTest(doc=rel):
                self.assertIn(rel[: -len(".md")] + ".ko.md", shipped)
        for rel in (r for r in rb.ALLOWLIST if r.endswith(".ko.md")):
            with self.subTest(translation=rel):
                self.assertIn(rel[: -len(".ko.md")] + ".md", shipped)

    def test_real_tree_bundle_includes_localized_docs(self):
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        image = f"ghcr.io/mykim0409/persona-vault-gateway@{DIGEST}"
        archive, _ = rb.build_bundle(ROOT, f"gateway-v{version}", image, self.out)
        files = {n.split("/", 1)[1] for n, d in members(archive).items() if d is not None}
        for rel in ("SECURITY.ko.md", "docs/setup.ko.md", "docs/hosting.ko.md", "docs/operations.ko.md",
                    "docs/CURATOR.ko.md", "docs/metadata.ko.md"):
            self.assertIn(rel, files)
        self.assertFalse([n for n in files if "WORKING_AGREEMENT" in n])

    def test_real_tree_bundle_does_not_ship_obsolete_files(self):
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        image = f"ghcr.io/mykim0409/persona-vault-gateway@{DIGEST}"
        archive, _ = rb.build_bundle(ROOT, f"gateway-v{version}", image, self.out)
        files = {n.split("/", 1)[1] for n, d in members(archive).items() if d is not None}
        for rel in (".env.example", "docs/evaluation-error-book.md", "docs/evaluation-error-book.ko.md"):
            self.assertNotIn(rel, rb.ALLOWLIST)
            self.assertNotIn(rel, files)


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
        self.assertEqual(len(stdout.split()), 4)  # archive, its checksum, standalone compose, its checksum


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

    def test_release_attaches_and_verifies_the_standalone_compose(self):
        create = self.release.split("gh release create", 1)[1]
        for asset in ('"dist/release/persona-vault-gateway-$VERSION-install.tar.gz"',
                      '"dist/release/persona-vault-gateway-$VERSION-install.tar.gz.sha256"',
                      '"dist/release/compose.yml"', '"dist/release/compose.yml.sha256"'):
            self.assertIn(asset, create)
        self.assertNotIn("compose.onboarding.yml", self.release)
        for check in ("sha256sum -c compose.yml.sha256", 'cmp compose.yml "$compose"',
                      'test "$(grep -cF -- "\\${PVG_IMAGE:-$IMAGE_REF}" compose.yml)" = 1'):
            self.assertIn(check, self.release)
        # The archive's compose.yml and both provider recipes must carry the digest exactly once.
        self.assertIn('"$compose")" = 1', self.release)
        self.assertIn('test "$(grep -cF -- "$IMAGE_REF" "$base/$recipe")" = 1', self.release)

    def test_release_gates_and_draft_flow_are_unchanged(self):
        for text in ("needs: [validate, ci]", "needs: [validate, image]", "--draft", "--verify-tag",
                     "platforms: linux/amd64,linux/arm64", "gateway-v(0|[1-9][0-9]*)", "persona-vault-gateway-$VERSION-install.tar.gz"):
            self.assertIn(text, self.release)
        self.assertNotIn("gh release edit", self.release)
        self.assertNotIn("--draft=false", self.release)

    def test_ci_runs_and_compiles_the_deployment_and_onboarding_tests(self):
        for name in ("test_onboarding.py", "test_deployment_surfaces.py"):
            self.assertIn(f"python tests/{name}\n", self.ci)
            self.assertRegex(self.ci, rf"py_compile [^\n]*tests/{name}")

    def test_ci_checks_real_compose_config_and_the_railway_typecheck(self):
        for text in ("config --quiet", "-f compose.yml -f compose.build.yml config --quiet", "--profile semantic",
                     "npm ci --ignore-scripts", "npm run typecheck", "python tests/test_compose_smoke.py"):
            self.assertIn(text, self.ci)


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
