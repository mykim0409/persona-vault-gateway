"""Contract tests for the single deployment path: Dockerfile, compose.yml, compose.build.yml, .env.example,
render.yaml and .railway/railway.ts.

Static checks only: nothing is deployed, no Render or Railway account, no Docker daemon, no network.

Honest limits:
- Compose is validated by the real `docker compose config` when the Compose v2 plugin is installed (CI has it);
  without it that test is SKIPPED, which is not a pass. The other Compose checks are plain text checks.
- render.yaml and railway.ts are checked with text patterns, not parsed. Whether Render or Railway accept them,
  bill them, or can reach a Vault host is not tested.
- The Railway SDK typecheck runs when the scoped tooling is installed (`npm ci --ignore-scripts` in .railway,
  which CI does); otherwise that test is skipped.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "persona-vault-gateway"
START = ["python", "-m", "gateway.server"]
DATA_ENV = {"PVG_DATA_DIR": "/data", "VAULT_DIR": "/data/vault", "DB_PATH": "/data/gateway.db"}
# Volumes of the previous multi-service stack. The single-service stack must never reuse or mount them.
LEGACY_VOLUMES = ("persona-vault", "persona-vault-gateway-data")


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def yaml_code(text):
    """YAML text without full-line and trailing ` #` comments (none of these files has a `#` inside a string)."""
    return "\n".join(re.sub(r"\s+#.*$", "", line) for line in text.splitlines() if not line.lstrip().startswith("#"))


def ts_code(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"(?m)(^|\s)//.*$", "", line) for line in text.splitlines())


def project_version():
    return tomllib.loads(read("pyproject.toml"))["project"]["version"]


def have_compose():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


def compose_config(*args):
    """`docker compose config --format json` on the real files with an EMPTY env file (never a local .env)."""
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty.env"
        empty.write_text("")
        scrubbed = ("COMPOSE_", "PVG_", "GATEWAY_", "EMBEDDING_", "FORWARDED_", "VAULT_", "CLOUDFLARE_", "QDRANT_", "HOST_ID")
        env = {k: v for k, v in os.environ.items() if not k.startswith(scrubbed)}
        return subprocess.run(["docker", "compose", "--env-file", str(empty), *args, "config", "--format", "json"],
                              cwd=tmp, capture_output=True, text=True, timeout=60, env=env)


class DockerfileTests(unittest.TestCase):
    def setUp(self):
        self.text = read("Dockerfile")

    def test_default_command_is_the_managed_launcher(self):
        self.assertEqual(re.findall(r"(?m)^CMD (.+)$", self.text), ['["python", "-m", "gateway.server"]'])
        self.assertNotIn("ENTRYPOINT", self.text)
        self.assertNotIn("uvicorn", self.text.split("CMD", 1)[1])

    def test_default_environment_uses_the_single_data_root(self):
        block = self.text.split("ENV PVG_DATA_DIR", 1)[1].split("EXPOSE")[0]
        env = dict(re.findall(r"(\w+)=(/\S+?)(?:\s*\\)?$", "PVG_DATA_DIR" + block, re.M))
        self.assertEqual(env, DATA_ENV)
        self.assertNotIn("VAULT_DIR=/vault", self.text)
        self.assertIn("EXPOSE 8000", self.text)
        self.assertRegex(self.text, r"install -y --no-install-recommends git openssh-client")  # clone + deploy key
        self.assertRegex(self.text, r"(?m)^FROM \S+@sha256:[0-9a-f]{64}$")


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.text = read("compose.yml")
        self.code = yaml_code(self.text)

    def test_single_gateway_service_and_no_legacy_services_or_preconditions(self):
        self.assertEqual(re.findall(r"(?m)^  ([a-z][\w-]*):$", self.code.split("\nvolumes:")[0]), [GATEWAY, "qdrant"])
        for gone in ("persona-vault-sync", "persona-vault-init", "alpine/git", "gateway.bootstrap", "secrets:", "/run/secrets",
                     "VAULT_REPO_SSH_URL", "ADMIN_PASSWORD", "./secrets", "depends_on", "persona-vault-onboarding",
                     "stdin_open", "tty:", "- .:/setup"):
            self.assertNotIn(gone, self.code, gone)
        self.assertFalse((ROOT / "compose.onboarding.yml").exists())

    def test_image_command_port_and_data_volume(self):
        for text in ('image: "${PVG_IMAGE:-persona-vault-gateway:local}"', "command: [python, -m, gateway.server]",
                     "container_name: persona-vault-gateway", "restart: unless-stopped",
                     '"${GATEWAY_BIND_ADDR:-127.0.0.1}:${GATEWAY_HOST_PORT:-18080}:8000"', "- persona-vault-data:/data"):
            self.assertIn(text, self.code)
        self.assertEqual(self.code.count("${PVG_IMAGE:-persona-vault-gateway:local}"), 1)  # release pinning needs exactly 1
        self.assertEqual(re.findall(r"(?m)^  ([a-z][\w-]*):\s*$", self.code.split("\nvolumes:\n")[1]),
                         ["persona-vault-data", "persona-vault-qdrant"])
        for legacy in LEGACY_VOLUMES:
            self.assertNotRegex(self.code, rf"(?m)^\s+(- )?{legacy}(:|\s*$)")
        self.assertNotIn(":/vault", self.code)

    def test_environment_keeps_the_useful_controls_and_one_data_root(self):
        env = dict(re.findall(r'(?m)^      ([A-Z_]+): "?([^"\n]*)"?$', self.code))
        for key, value in {"PORT": "8000", **DATA_ENV}.items():
            self.assertEqual(env[key], value)
        for key in ("PVG_SETUP_TOKEN", "PVG_SECURE_COOKIES", "VAULT_SYNC_INTERVAL_SECONDS", "HOST_ID", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
                    "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_BATCH_SIZE", "QDRANT_URL", "QDRANT_COLLECTION",
                    "FORWARDED_ALLOW_IPS"):
            self.assertIn(key, env)
        self.assertEqual(env["EMBEDDING_PROVIDER"], "${EMBEDDING_PROVIDER:-none}")
        self.assertEqual(env["PVG_SETUP_TOKEN"], "${PVG_SETUP_TOKEN:-}")
        self.assertEqual(env["PVG_SECURE_COOKIES"], "${PVG_SECURE_COOKIES:-false}")
        self.assertEqual(env["FORWARDED_ALLOW_IPS"], "${FORWARDED_ALLOW_IPS:-127.0.0.1}")
        self.assertNotIn('"*"', self.code)

    def test_no_parse_time_requirements_sockets_or_build(self):
        self.assertNotRegex(self.code, r"(?<!\$)\$\{[^}]*:\?")
        self.assertNotIn("docker.sock", self.code)
        self.assertNotRegex(self.code, r"(?m)^\s+build:")  # release users never build from source
        build = yaml_code(read("compose.build.yml"))
        self.assertEqual(re.findall(r"(?m)^  ([a-z][\w-]*):$", build), [GATEWAY])
        self.assertRegex(build, r"  persona-vault-gateway:\n    build: \.")

    def test_healthcheck_is_liveness(self):
        self.assertIn("http://127.0.0.1:8000/healthz", self.code)
        self.assertNotIn("/readyz", self.code)  # 503 until the Vault is connected would restart-loop the wizard

    def test_qdrant_stays_optional_and_pinned(self):
        qdrant = self.code.split("\n  qdrant:\n")[1].split("\nvolumes:")[0]
        self.assertIn("profiles: [semantic]", qdrant)
        self.assertRegex(qdrant, r"image: qdrant/qdrant@sha256:[0-9a-f]{64}")
        self.assertIn("persona-vault-qdrant:/qdrant/storage", qdrant)
        self.assertNotRegex(self.code.split("\n  qdrant:\n")[0], r"qdrant:\n\s+condition")  # the Gateway never waits for it

    @unittest.skipUnless(have_compose(), "Docker Compose v2 not available: real config check SKIPPED, not passed")
    def test_real_compose_config(self):
        base = compose_config("-f", str(ROOT / "compose.yml"))
        self.assertEqual(base.returncode, 0, base.stderr)
        cfg = json.loads(base.stdout)
        self.assertEqual(list(cfg["services"]), [GATEWAY])  # Qdrant only with the semantic profile
        gw = cfg["services"][GATEWAY]
        self.assertEqual(gw["command"], START)
        self.assertEqual(gw["image"], "persona-vault-gateway:local")
        env = gw["environment"]
        for key, value in {"PORT": "8000", **DATA_ENV, "EMBEDDING_PROVIDER": "none", "PVG_SETUP_TOKEN": "", "PVG_SECURE_COOKIES": "false",
                           "FORWARDED_ALLOW_IPS": "127.0.0.1"}.items():
            self.assertEqual(env[key], value, key)
        self.assertEqual([(p["host_ip"], p["target"], p["published"]) for p in gw["ports"]], [("127.0.0.1", 8000, "18080")])
        self.assertEqual([(v["source"], v["target"]) for v in gw["volumes"]], [("persona-vault-data", "/data")])
        self.assertNotIn("depends_on", gw)
        self.assertEqual(set(cfg["volumes"]), {"persona-vault-data"})  # the Qdrant volume only exists with its profile
        self.assertNotIn("secrets", cfg)
        semantic = json.loads(compose_config("--profile", "semantic", "-f", str(ROOT / "compose.yml")).stdout)
        self.assertEqual(set(semantic["services"]), {GATEWAY, "qdrant"})
        self.assertEqual(set(semantic["volumes"]), {"persona-vault-data", "persona-vault-qdrant"})
        built = json.loads(compose_config("-f", str(ROOT / "compose.yml"), "-f", str(ROOT / "compose.build.yml")).stdout)
        self.assertEqual(list(built["services"]), [GATEWAY])
        self.assertEqual(built["services"][GATEWAY]["build"]["context"], str(ROOT))

    def test_env_example_documents_only_the_single_path(self):
        text = read(".env.example")
        keys = set(re.findall(r"(?m)^#? ?([A-Z_]+)=", text))
        self.assertEqual(keys, {"PVG_SETUP_TOKEN", "PVG_SECURE_COOKIES", "HOST_ID", "GATEWAY_BIND_ADDR", "GATEWAY_HOST_PORT", "VAULT_SYNC_INTERVAL_SECONDS",
                                "EMBEDDING_PROVIDER", "COMPOSE_PROFILES", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
                                "EMBEDDING_MODEL", "EMBEDDING_BATCH_SIZE", "QDRANT_COLLECTION"})
        self.assertRegex(text, r"(?m)^# PVG_SETUP_TOKEN=")  # optional: never active by default
        self.assertRegex(text, r"(?m)^EMBEDDING_PROVIDER=none$")
        self.assertNotIn("VAULT_REPO_SSH_URL", text)
        self.assertNotIn("ADMIN_PASSWORD", text)


class RenderBlueprintTests(unittest.TestCase):
    def setUp(self):
        self.text = read("render.yaml")
        self.code = yaml_code(self.text)

    def value(self, key):
        found = re.findall(rf"(?m)^(?:    |  - ){key}: (.+)$", self.code)
        self.assertEqual(len(found), 1, key)
        return found[0].strip()

    def test_single_paid_web_service_on_the_released_image(self):
        self.assertEqual(len(re.findall(r"(?m)^  - type:", self.code)), 1)
        self.assertEqual(self.code.splitlines()[0], "services:")
        self.assertEqual((self.value("type"), self.value("runtime"), self.value("plan")), ("web", "image", "0.5c-512mb"))
        self.assertEqual(self.value("numInstances"), "1")  # a service with a disk cannot scale out
        self.assertNotRegex(self.code, r"(?m)^\s+(runtime: docker|dockerCommand|dockerfilePath|repo|branch|buildCommand|startCommand):")
        self.assertNotRegex(self.code, r"plan: free")
        tagged = f"ghcr.io/mykim0409/persona-vault-gateway:gateway-v{project_version()}"
        self.assertEqual(re.findall(r"(?m)^      url: (\S+)$", self.code), [tagged])  # the bundle swaps this for the digest
        self.assertNotIn(":latest", self.code)

    def test_health_check_and_deploys_are_manual(self):
        self.assertEqual(self.value("healthCheckPath"), "/healthz")
        self.assertRegex(self.text, r'(?m)^    autoDeployTrigger: "off"')  # quoted: a bare off is a YAML boolean

    def test_disk_matches_the_data_contract(self):
        disk = re.search(r"(?m)^    disk:\n      name: (\S+)\n      mountPath: (\S+)\n      sizeGB: (\d+)$", self.code)
        self.assertIsNotNone(disk)
        self.assertEqual(disk.group(2), "/data")
        self.assertGreaterEqual(int(disk.group(3)), 1)

    def test_env_and_setup_token_are_never_literal(self):
        env = dict(re.findall(r"(?m)^      - key: (\w+)\n        value: (\S+)$", self.code))
        self.assertEqual(env, {**DATA_ENV, "EMBEDDING_PROVIDER": "none", "PVG_SECURE_COOKIES": '"true"'})  # quoted: not a YAML boolean
        self.assertNotIn("FORWARDED_ALLOW_IPS", self.code)
        self.assertRegex(self.code, r"(?m)^      - key: PVG_SETUP_TOKEN\n        generateValue: true$")
        for forbidden in ("ADMIN_PASSWORD", "CLOUDFLARE", "QDRANT", "VAULT_REPO_SSH_URL", "FORWARDED_ALLOW_IPS"):
            self.assertNotIn(forbidden, self.code)

    def test_no_extra_ingress_or_resources(self):
        for bad in ("domains", "previews", "renderSubdomainPolicy", "databases", "envVarGroups", "routes", "headers", "scaling",
                    "cron", "registryCredential", "creds"):
            self.assertNotIn(bad, self.code, bad)


class RailwayIacTests(unittest.TestCase):
    def setUp(self):
        self.raw = read(".railway/railway.ts")
        self.code = ts_code(self.raw)

    def test_uses_the_iac_dsl_and_no_legacy_config(self):
        self.assertEqual(re.findall(r'from "([^"]+)"', self.code), ["railway/iac"])
        self.assertFalse((ROOT / "railway.toml").exists())
        self.assertFalse((ROOT / "railway.json").exists())
        self.assertEqual(len(re.findall(r"\bservice\(", self.code)), 1)
        self.assertEqual(len(re.findall(r"\bvolume\(", self.code)), 1)

    def test_source_is_the_pinned_release_image_with_auto_updates_disabled(self):
        tagged = f"ghcr.io/mykim0409/persona-vault-gateway:gateway-v{project_version()}"
        self.assertEqual(re.findall(r'const GATEWAY_IMAGE = "([^"]+)";', self.code), [tagged])
        self.assertIn('source: image(GATEWAY_IMAGE, { autoUpdates: { type: "disabled" } })', self.code)
        for bad in ("github(", "branch", "build:", "builder", "dockerfilePath", "template(", "checkSuites", ":latest"):
            self.assertNotIn(bad, self.code, bad)  # nothing that watches the shared repository

    def test_service_fields(self):
        for text in ('start: "python -m gateway.server"', 'healthcheck: "/healthz"', "replicas: 1,",
                     'volumeMounts: { "/data": data }', 'const data = volume("persona-vault-data");'):
            self.assertIn(text, self.code)
        self.assertEqual(re.findall(r"replicas: *(\w+)", self.code), ["1"])
        for bad in ("domains", "tcp", "regions", "numReplicas", "multiRegionConfig", "isDeleted", "randomString", "ctx.",
                    "sleepApplication", "cronSchedule"):
            self.assertNotIn(bad, self.code, bad)

    def test_env_matches_the_contract_and_leaves_the_setup_token_out(self):
        block = re.search(r"env: \{(.*?)\}", self.code, re.S).group(1)
        env = dict(re.findall(r'(\w+): "([^"]*)"', block))
        self.assertEqual(env, {"PORT": "8000", **DATA_ENV, "EMBEDDING_PROVIDER": "none", "PVG_SECURE_COOKIES": "true"})
        self.assertNotIn("FORWARDED_ALLOW_IPS", self.code)
        self.assertNotIn("PVG_SETUP_TOKEN", self.code)  # only the header comment may mention it
        self.assertIn("PVG_SETUP_TOKEN", self.raw)

    def test_scoped_tooling_is_exact_and_ships_no_install_hooks(self):
        package = json.loads(read(".railway/package.json"))
        self.assertTrue(package["private"])
        self.assertEqual(package["devDependencies"]["railway"], "3.12.0")
        self.assertEqual(package["scripts"], {"typecheck": "tsc --noEmit -p tsconfig.json"})
        for name, version in package["devDependencies"].items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+$", name)  # exact pins, no ranges
        for key in ("dependencies", "bin", "main", "exports"):
            self.assertNotIn(key, package)
        lock = json.loads(read(".railway/package-lock.json"))
        self.assertEqual(lock["packages"]["node_modules/railway"]["version"], "3.12.0")
        for path, entry in lock["packages"].items():
            if path:
                self.assertTrue(entry["resolved"].startswith("https://registry.npmjs.org/"), path)
                self.assertIn("integrity", entry, path)

    def test_no_javascript_tooling_leaks_into_the_runtime(self):
        for rel in ("package.json", "package-lock.json", "node_modules"):
            self.assertFalse((ROOT / rel).exists(), rel)
        dockerignore = read(".dockerignore")
        self.assertTrue(dockerignore.startswith("# Allowlist") and "\n*\n" in dockerignore)
        for rel in (".railway", "render.yaml", "compose.yml"):
            self.assertNotIn("!" + rel, dockerignore)
        gitignore = read(".gitignore").splitlines()
        self.assertIn(".railway/node_modules/", gitignore)
        self.assertNotIn("node_modules", gitignore)
        self.assertNotIn("node_modules/", gitignore)

    @unittest.skipUnless(shutil.which("node") and (ROOT / ".railway/node_modules/typescript/bin/tsc").is_file(),
                         "scoped SDK tooling not installed (npm ci --ignore-scripts in .railway): SKIPPED, not passed")
    def test_sdk_typecheck_when_tooling_is_installed(self):
        result = subprocess.run(["node", "node_modules/typescript/bin/tsc", "--noEmit", "-p", "tsconfig.json"],
                                cwd=ROOT / ".railway", capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class NoDestructiveVolumeTests(unittest.TestCase):
    def test_surfaces_never_delete_or_move_volumes(self):
        for rel in ("compose.yml", "compose.build.yml", "render.yaml", ".railway/railway.ts", ".github/workflows/release.yml",
                    "Dockerfile"):
            text = read(rel)
            for bad in ("down -v", "--volumes", "volume rm", "volume prune", "system prune", "isDeleted", "rm -rf /data",
                        "docker cp"):
                self.assertNotIn(bad, text, f"{rel}: {bad}")


if __name__ == "__main__":
    unittest.main()
