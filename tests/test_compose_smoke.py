"""Fresh Docker Compose smoke test of the single install path (stdlib only).

Needs the Docker CLI + Compose v2, and a running daemon for the full run.

Runs the real compose.yml (+ compose.build.yml, so the image is built from source) with a minimal override under a
unique `pvg-smoke-*` project: own container name, own gateway image tag, loopback ephemeral port, fresh volumes.
Nothing is pre-seeded for the Gateway: it starts unclaimed, exactly like a first install. Two projects run one after
the other: `keyword` is the base deployment (EMBEDDING_PROVIDER=none, no Cloudflare keys, no Qdrant, the setup code
from PVG_SETUP_TOKEN) and `semantic` enables the `semantic` profile (real pinned Qdrant, hash embeddings, the setup
code read from the container log).

The journey, driven over HTTP like a browser (see Journey):
  pending   /healthz 200 and the container stays healthy and un-restarted, /readyz 503 (setup: claim), /setup open
  claim     a wrong code is refused, the setup code claims the Gateway and sets the admin password
  clone     a deploy key is generated, then the Vault is cloned from a synthetic bare repository
  use       /readyz 200, an agent token can search the cloned note and capture a new one
  sync      (keyword) the capture is pushed to the remote and a remote commit is pulled back
  restart   the container is recreated: admin login, agent token and Vault content persist, /setup stays closed
  restore   (keyword) the stopped /data volume is copied into a fresh volume and the copy still has the claim
The real SSH transport is replaced by a TEST-ONLY mounted module (sitecustomize.py) that patches
gateway.onboarding to clone from a local bare repository stored in a project-scoped Docker volume. Production code has
no switch for this: without the mounted file the Gateway only speaks SSH.
Cleanup removes only this project's containers/volumes, its image tag and its temp dir.
Exit 0 = passed, 1 = failed, 2 = skipped (no Docker CLI, no Compose v2, or no running daemon for the full run); a skip
is not a pass. `--prepare-only` still needs the Docker CLI and Compose v2 (`docker compose config`) but no daemon.
"""
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "persona-vault-gateway"
START = ["python", "-m", "gateway.server"]
NOTE = "00_Inbox/pvg-smoke-note.md"
REMOTE_NOTE = "00_Inbox/pvg-remote-note.md"
QUERY = "quartz heron canary"
REPO_URL = "git@github.com:pvg-smoke/synthetic-vault.git"  # validated like a real URL, never contacted
SEED_REMOTE = "/seed/vault.git"
# Variables compose.yml interpolates; never inherit them from the caller's shell (shell beats --env-file).
DROP = ("COMPOSE_", "PVG_", "FORWARDED_", "VAULT_", "GATEWAY_", "EMBEDDING_", "QDRANT_", "CLOUDFLARE_", "HOST_ID")
MODES = ("keyword", "semantic")
CSRF_RE = re.compile(r'name="csrf_token" value="([0-9a-f]+)"')
CODE_RE = re.compile(r"first-run setup code \(shown once\): (\S+)")

STATE_PY = "import os,json;print(json.load(open(os.environ['PVG_DATA_DIR']+'/setup/vault.json'))['state'])"
ERROR_PY = "import os,json;print(json.load(open(os.environ['PVG_DATA_DIR']+'/setup/vault.json')).get('error'))"
KEYMODE_PY = ("import stat;from gateway import bootstrap,onboarding as o;p=bootstrap.Paths(o.setup_dir());"
              "print(oct(stat.S_IMODE(p.key.stat().st_mode)),oct(stat.S_IMODE(o.setup_dir().stat().st_mode)))")
TOKEN_PY = ("from gateway.core import Settings as S, init_db, generate_token as g, upsert_agent as u;"
            "s=S.from_env();init_db(s.db_path);t=g();"
            "u(s.db_path,'pvg-smoke',t,['conversation-log','agent-memo','vault-rag'],['30_Conversations/raw']);print(t)")
INDEX_PY = "from gateway.core import Settings as S, index_vault as i;print(i(S.from_env()))"
DIGEST_PY = "import hashlib,os;print(hashlib.sha256(open(os.path.join(os.environ['VAULT_DIR'],{path!r}),'rb').read()).hexdigest())"
EXISTS_PY = "import os;print(os.path.isfile(os.path.join(os.environ['VAULT_DIR'],{path!r})))"
# Test-only transport replacement, mounted as sitecustomize.py. Not a production feature.
SITECUSTOMIZE = '''"""TEST ONLY (tests/test_compose_smoke.py): clone from a local bare repository instead of GitHub over SSH."""
from gateway import onboarding

onboarding.ALLOWED_PROTOCOLS = "file"
onboarding.remote_url = lambda url: {remote!r}
'''
# Run inside the Gateway image (root, so the repository has the same owner as the Gateway process).
_GIT = ("import os,subprocess,sys\n"
        "env=dict(os.environ,HOME='/tmp',GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_SYSTEM='/dev/null',"
        "GIT_AUTHOR_NAME='smoke',GIT_AUTHOR_EMAIL='s@localhost',GIT_COMMITTER_NAME='smoke',GIT_COMMITTER_EMAIL='s@localhost')\n"
        "def git(*a,cwd=None):\n"
        "    return subprocess.run(['git',*a],cwd=cwd,env=env,check=True,capture_output=True,text=True).stdout\n")
SEED_PY = _GIT + (
    "git('init','--bare','-b','main','{remote}')\n"
    "git('clone','{remote}','/tmp/w')\n"
    "os.makedirs('/tmp/w/00_Inbox')\n"
    "open('/tmp/w/{note}','w').write('# PVG smoke note\\n\\nSynthetic {query} sentence for the compose smoke test.\\n')\n"
    "git('add','.',cwd='/tmp/w');git('commit','-m','synthetic smoke note',cwd='/tmp/w')\n"
    "git('push','origin','HEAD:main',cwd='/tmp/w')\n")
REMOTE_COMMIT_PY = _GIT + (
    "git('clone','{remote}','/tmp/o')\n"
    "open('/tmp/o/{note}','w').write('# Remote note\\n\\nWritten by another device.\\n')\n"
    "git('add','.',cwd='/tmp/o');git('commit','-m','remote note',cwd='/tmp/o')\n"
    "git('push','origin','HEAD:main',cwd='/tmp/o')\n")
LS_PY = _GIT + "print(git('--git-dir','{remote}','ls-tree','-r','--name-only','main'))\n"
# Same file contents in two trees, and the copied SQLite file opens and is intact.
COMPARE_PY = """
import hashlib, os, sqlite3, sys
def tree(root):
    out = {}
    for base, _, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            out[os.path.relpath(path, root)] = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    return out
a, b = tree(sys.argv[1]), tree(sys.argv[2])
assert a and a == b, sorted(set(a) ^ set(b))[:5] or 'file content differs'
if len(sys.argv) > 3:
    con = sqlite3.connect('file:' + os.path.join(sys.argv[2], sys.argv[3]) + '?immutable=1', uri=True)
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
print('equal', len(a))
"""
TAR_PY = "import sys,tarfile\nwith tarfile.open('/bak/'+sys.argv[1],'w') as t: t.add('/src', arcname='.')"
UNTAR_PY = "import sys,tarfile\ntarfile.open('/bak/'+sys.argv[1]).extractall('/dst', filter='fully_trusted')"
# The restored data root still holds the claim (password), the connected Vault and the captured file.
RESTORE_PY = """
import os, sys
from gateway import onboarding as o
assert o.claimed(), 'claim missing'
assert o.verify_password(os.environ['PVG_SMOKE_PASSWORD']) and not o.verify_password('wrong-password-0123456')
assert o.read_vault_state()['state'] == 'ready'
assert (o.vault_dir() / '.git').is_dir() and (o.vault_dir() / sys.argv[1]).is_file()
print('restored ok')
"""


def run(cmd, env=None, check=True, **kw):
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=kw.pop("timeout", 600), **kw)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, cmd[:4]))} failed ({r.returncode}): {r.stderr.strip()[-800:]}")
    return r


def skip_reason(prepare_only):
    """Why this run must be SKIPPED, or None. Compose v2 is needed in both modes; the daemon only for the full run."""
    if not shutil.which("docker"):
        return "Docker CLI not found"
    try:
        if run(["docker", "compose", "version"], check=False, timeout=60).returncode:
            return "Docker Compose v2 plugin (`docker compose`) not available"
        if not prepare_only and run(["docker", "info"], check=False, timeout=60).returncode:
            return "no running Docker daemon"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Docker CLI not usable ({type(exc).__name__})"
    return None


def wait(cond, what, timeout=90):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.5)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 303 is returned to the caller, like a browser we want to observe


class Browser:
    """A cookie-keeping HTTP client that does not follow redirects. Responses are (status, lower-case headers, text)."""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, form=None, bearer=None, body=None):
        headers, data = {}, None
        if form is not None:
            data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                status, head, raw = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            status, head, raw = err.code, err.headers, err.read()
        return status, {k.lower(): v for k, v in head.items()}, raw.decode("utf-8", "replace")

    def json(self, method, path, **kw):
        status, _, text = self.request(method, path, **kw)
        return status, json.loads(text or "{}")


class Journey:
    """The first-use path over HTTP. `target` provides py(code) -> stdout (run where the Gateway's env is set),
    remote_files() -> set of paths on the remote's main branch, and remote_commit() (a commit by another device)."""

    def __init__(self, base, target, password):
        self.base, self.target, self.password = base, target, password
        self.web = Browser(base)
        self.token = None

    def py(self, code):
        return self.target.py(code).strip()

    def csrf(self, path="/admin/vault"):
        status, _, text = self.web.request("GET", path)
        assert status == 200, (path, status)
        found = CSRF_RE.findall(text)
        assert found, f"no csrf token on {path}"
        return found[0]

    def live(self):
        try:
            return self.web.request("GET", "/healthz")[0] == 200
        except OSError:
            return False

    def pending(self):
        """Before the claim: alive, not ready, setup open, everything else closed."""
        assert self.web.request("GET", "/healthz")[0] == 200
        assert self.web.json("GET", "/readyz") == (503, {"status": "pending", "setup": "claim"})
        status, head, _ = self.web.request("GET", "/")
        assert (status, head["location"]) == (303, "/setup"), (status, head)
        status, _, text = self.web.request("GET", "/setup")
        assert status == 200 and "Set up PersonaVault" in text
        status, head, _ = self.web.request("GET", "/admin/vault")
        assert (status, head["location"]) == (303, "/admin/login"), status
        assert self.web.request("POST", "/gateway/v3/search", body={"query": QUERY})[0] in (401, 503)

    def claim(self, code):
        form = {"setup_code": code, "password": self.password, "confirm": self.password}
        assert self.web.request("POST", "/setup", form={**form, "setup_code": "not-the-setup-code-0123"})[0] == 403
        assert self.web.request("POST", "/setup", form={**form, "confirm": self.password + "x"})[0] == 400
        status, head, _ = self.web.request("POST", "/setup", form=form)
        assert (status, head["location"]) == (303, "/admin/vault"), status
        assert [c.name for c in self.web.jar] == ["pvg_admin"], "admin session cookie missing"
        status, head, _ = self.web.request("GET", "/setup")
        assert (status, head["location"]) == (303, "/"), "/setup must close after the claim"
        assert self.web.request("POST", "/setup", form=form)[0] == 303  # a second claim never reaches the claim logic
        assert self.web.json("GET", "/readyz") == (503, {"status": "pending", "setup": "vault"})

    def connect_vault(self, timeout=90):
        status, _, text = self.web.request("GET", "/admin/vault")
        assert status == 200 and "Connect your Vault" in text
        status, _, _ = self.web.request("POST", "/admin/vault", form={"csrf_token": self.csrf(), "repo_url": REPO_URL})
        assert status == 303, status
        status, _, text = self.web.request("GET", "/admin/vault")
        assert "ssh-ed25519 " in text and "settings/keys/new" in text and "PRIVATE KEY" not in text
        assert self.py(KEYMODE_PY) == "0o600 0o700", "deploy key or setup dir permissions"
        status, _, _ = self.web.request("POST", "/admin/vault/connect", form={"csrf_token": self.csrf()})
        assert status == 303, status

        def done():
            state = self.py(STATE_PY)
            assert state != "failed", f"clone failed: {self.py(ERROR_PY)}"
            return state == "ready"

        wait(done, "the Vault clone", timeout)

    def agent_token(self):
        self.token = self.py(TOKEN_PY)
        assert self.web.request("POST", "/gateway/v3/search", body={"query": QUERY})[0] == 401

    def search(self, token=None):
        return self.web.json("POST", "/gateway/v3/search", body={"query": QUERY, "limit": 5}, bearer=token or self.token)

    def use(self):
        """Read the cloned note, then write a capture. Returns (captured path, sha256)."""
        status, body = self.search()
        assert status == 200 and NOTE in [r["path"] for r in body["results"]], (status, body)
        note = {"kind": "note", "title": "pvg smoke", "body": "synthetic"}
        status, body = self.web.json("POST", "/gateway/v3/capture", body=note, bearer=self.token)
        assert status == 200 and body["path"].startswith("30_Conversations/raw/"), (status, body)
        path = body["path"]
        assert self.py(EXISTS_PY.format(path=path)) == "True"
        return path, self.py(DIGEST_PY.format(path=path))

    def sync_now(self):
        status, _, _ = self.web.request("POST", "/admin/vault/sync", form={"csrf_token": self.csrf()})
        assert status == 303, status

    def sync_roundtrip(self, captured, timeout=90):
        """The capture reaches the remote; a commit made elsewhere reaches the Vault."""
        self.sync_now()
        wait(lambda: captured in self.target.remote_files(), "the capture to be pushed", timeout)
        self.target.remote_commit()
        self.sync_now()
        wait(lambda: self.py(EXISTS_PY.format(path=REMOTE_NOTE)) == "True", "the remote commit to be pulled", timeout)
        assert {NOTE, captured, REMOTE_NOTE} <= self.target.remote_files()

    def after_restart(self, captured, digest, timeout=90):
        """A fresh process on the same data: the claim, the agent token and the Vault content persisted."""
        wait(self.live, "liveness after the restart", timeout)
        fresh = Browser(self.base)
        assert fresh.request("POST", "/admin/login", form={"password": self.password + "x"})[0] == 401
        assert fresh.request("POST", "/admin/login", form={"password": self.password})[0] == 303
        assert fresh.request("GET", "/admin/vault")[0] == 200
        status, head, _ = Browser(self.base).request("GET", "/setup")
        assert (status, head["location"]) == (303, "/"), "/setup must stay closed after a restart"
        wait(lambda: self.web.json("GET", "/readyz")[0] == 200, "readiness after the restart", timeout)
        status, body = self.search()
        assert status == 200 and NOTE in [r["path"] for r in body["results"]], (status, body)
        assert self.web.request("POST", "/gateway/v3/search", body={"query": QUERY})[0] == 401
        assert self.py(DIGEST_PY.format(path=captured)) == digest, "captured note changed or vanished"


class Smoke:
    """The Compose project: files, containers and volumes of one smoke run, all named after the project."""

    def __init__(self, mode="keyword"):
        assert mode in MODES
        self.mode = mode
        self.uid = secrets.token_hex(4)
        self.project = f"pvg-smoke-{mode[:3]}-{self.uid}"
        self.tag = f"{self.project}-gateway:smoke"  # never persona-vault-gateway:local
        self.container = f"{self.project}-gateway"
        self.seed_volume = f"{self.project}-seed"
        self.tmp = Path(tempfile.mkdtemp(prefix=f"{self.project}-"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(DROP)}
        self.setup_token = secrets.token_urlsafe(24) if mode == "keyword" else ""  # semantic reads the generated code
        self.password = f"{secrets.token_hex(12)}-Zq$7"
        self.started = False
        self.data_volume = None
        self.scratch_volumes = []

    # --- compose plumbing -------------------------------------------------------------------------------------
    def base(self):
        profile = ["--profile", "semantic"] if self.mode == "semantic" else []
        return ["docker", "compose", "-p", self.project, *profile, "-f", str(ROOT / "compose.yml"),
                "-f", str(ROOT / "compose.build.yml"), "-f", str(self.tmp / "override.yml"),
                "--env-file", str(self.tmp / "smoke.env")]

    def compose(self, *args, **kw):
        return run([*self.base(), *args], env=self.env, **kw)

    def py(self, code):
        return self.compose("exec", "-T", GATEWAY, "python", "-c", code).stdout

    def inspect(self, fmt):
        r = run(["docker", "inspect", "-f", fmt, self.container], env=self.env, check=False)
        return r.stdout.strip() if r.returncode == 0 else ""

    def logs(self):
        r = run(["docker", "logs", self.container], env=self.env, check=False)
        return r.stdout + r.stderr

    def image_run(self, mounts, code, *argv, env=()):
        """One-off container from the smoke image: no network, no Docker socket, only the named volumes."""
        flags = [f for m in mounts for f in ("-v", m)] + [f for e in env for f in ("-e", e)]
        return run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", *flags, self.tag,
                    "-c", code, *argv], env=self.env).stdout.strip()

    # --- Journey target ---------------------------------------------------------------------------------------
    def remote_files(self):
        return set(self.image_run([f"{self.seed_volume}:/seed:ro"], LS_PY.format(remote=SEED_REMOTE)).split())

    def remote_commit(self):
        self.image_run([f"{self.seed_volume}:/seed"], REMOTE_COMMIT_PY.format(remote=SEED_REMOTE, note=REMOTE_NOTE))

    # --- setup ------------------------------------------------------------------------------------------------
    def prepare(self):
        t, q = self.tmp, json.dumps
        (t / "py").mkdir()
        (t / "py/sitecustomize.py").write_text(SITECUSTOMIZE.format(remote="file://" + SEED_REMOTE))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        (t / "smoke.env").write_text("\n".join([
            f"PVG_SETUP_TOKEN={self.setup_token}", "VAULT_SYNC_INTERVAL_SECONDS=3600", "HOST_ID=pvg-smoke",
            f"EMBEDDING_PROVIDER={'hash' if self.mode == 'semantic' else 'none'}",  # no Cloudflare keys: optional
            "GATEWAY_BIND_ADDR=127.0.0.1", f"GATEWAY_HOST_PORT={self.port}",
            f"QDRANT_COLLECTION=pvg_smoke_{self.uid}", ""]))
        (t / "override.yml").write_text(f"""services:
  {GATEWAY}:
    image: {self.tag}
    container_name: {self.container}
    healthcheck:
      interval: 2s
      start_period: 2s
    environment:
      PYTHONPATH: /smoke/py
    volumes:
      - smoke-seed:/seed
      - {q(f"{t}/py:/smoke/py:ro")}
  qdrant:
    container_name: {self.project}-qdrant
volumes:
  smoke-seed:
    external: true
    name: {self.seed_volume}
""")
        cfg = json.loads(self.compose("config", "--format", "json").stdout)
        assert set(cfg["services"]) == ({GATEWAY, "qdrant"} if self.mode == "semantic" else {GATEWAY}), \
            "the old init and sync services must be gone, and Qdrant only runs with the semantic profile"
        for name, svc in cfg["services"].items():
            assert svc["container_name"].startswith(self.project), f"{name}: container_name not isolated"
            assert not any("docker.sock" in str(v) for v in svc.get("volumes", [])), f"{name}: Docker socket mounted"
        gw = cfg["services"][GATEWAY]
        assert gw["image"] == self.tag and gw["command"] == START
        assert "depends_on" not in gw, "the Gateway must start without waiting for anything"
        env = gw["environment"]
        for key, value in {"PORT": "8000", "PVG_DATA_DIR": "/data", "VAULT_DIR": "/data/vault",
                           "DB_PATH": "/data/gateway.db", "PVG_SETUP_TOKEN": self.setup_token,
                           "EMBEDDING_PROVIDER": "hash" if self.mode == "semantic" else "none",
                           "PYTHONPATH": "/smoke/py"}.items():
            assert env[key] == value, (key, env.get(key))
        assert not env["CLOUDFLARE_ACCOUNT_ID"] and not env["CLOUDFLARE_API_TOKEN"]
        assert {v["target"] for v in gw["volumes"]} == {"/data", "/seed", "/smoke/py"}
        assert next(v for v in gw["volumes"] if v["target"] == "/data")["source"] == "persona-vault-data"
        assert [(p["host_ip"], int(p["published"]), p["target"]) for p in gw["ports"]] == [("127.0.0.1", self.port, 8000)]
        assert all(v["name"].startswith(self.project) for v in cfg["volumes"].values()), "volume not isolated"
        assert "build" in gw and gw["build"]["context"] == str(ROOT), "compose.build.yml must add the source build"
        self.data_volume = cfg["volumes"]["persona-vault-data"]["name"]

    # --- scenario ---------------------------------------------------------------------------------------------
    def setup_code(self):
        if self.setup_token:
            assert self.setup_token not in self.logs(), "a configured setup token must never be logged"
            return self.setup_token
        found = CODE_RE.findall(self.logs())
        assert len(found) == 1, "the generated setup code must be printed exactly once"
        return found[0]

    def restart(self):
        before = self.inspect("{{.Id}}")
        self.compose("up", "-d", "--force-recreate", "--no-deps", GATEWAY)
        assert before and self.inspect("{{.Id}}") not in ("", before), "gateway was not recreated"

    def backup_restore(self, captured):
        """Stopped stack: tar /data into a scratch volume, extract into a fresh volume, compare, check the claim."""
        bak, restored = f"{self.project}-bak", f"{self.project}-restore-data"
        self.scratch_volumes = [bak, restored]
        self.compose("stop", "-t", "10")
        self.image_run([f"{self.data_volume}:/src:ro", f"{bak}:/bak"], TAR_PY, "data.tar")
        self.image_run([f"{bak}:/bak:ro", f"{restored}:/dst"], UNTAR_PY, "data.tar")
        same = self.image_run([f"{self.data_volume}:/a:ro", f"{restored}:/b:ro"], COMPARE_PY, "/a", "/b", "gateway.db")
        assert same.startswith("equal"), same
        out = self.image_run([f"{restored}:/data:ro"], RESTORE_PY, captured,
                             env=["PVG_DATA_DIR=/data", f"PVG_SMOKE_PASSWORD={self.password}"])
        assert out == "restored ok", out

    def scenario(self):
        self.compose("build")
        self.started = True
        run(["docker", "volume", "create", self.seed_volume], env=self.env)
        self.image_run([f"{self.seed_volume}:/seed"], SEED_PY.format(remote=SEED_REMOTE, note=NOTE, query=QUERY))
        self.compose("up", "-d")
        journey = Journey(f"http://127.0.0.1:{self.port}", self, self.password)
        wait(lambda: self.inspect("{{.State.Health.Status}}") == "healthy", "a healthy container before any setup")
        time.sleep(3)  # a few more probes: nothing may restart or crash while the Gateway waits for its owner
        assert self.inspect("{{.State.Running}} {{.RestartCount}}") == "true 0", "the pending Gateway restarted"
        journey.pending()
        if self.mode == "keyword":
            running = self.compose("ps", "-q").stdout.split()
            assert running == [self.inspect("{{.Id}}")], "only the Gateway container may exist (no sidecar, no Qdrant)"
        journey.claim(self.setup_code())
        journey.connect_vault()
        if self.mode == "keyword":  # semantic search is off, so the Gateway is ready without any vector store
            assert journey.web.json("GET", "/readyz")[1]["semantic"] == "disabled"
        journey.agent_token()
        if self.mode == "semantic":
            self.py(INDEX_PY)
            code, body = journey.web.json("GET", "/readyz")
            assert code == 200 and body["rag_indexed"] is True and body["chunks"] > 0, (code, body)
        captured, digest = journey.use()
        if self.mode == "keyword":
            journey.sync_roundtrip(captured)
        self.restart()
        journey.after_restart(captured, digest)
        assert not CODE_RE.search(self.logs()), "a claimed Gateway must not print a setup code again"
        if self.mode == "keyword":
            self.backup_restore(captured)

    def cleanup(self, failed):
        assert self.project.startswith("pvg-smoke-")
        if self.started:
            if failed:
                print(self.compose("logs", "--no-color", "--tail", "60", check=False).stdout[-4000:])
            self.compose("down", "-v", "--remove-orphans", "-t", "5", check=False)
            for name in (self.seed_volume, *self.scratch_volumes):
                assert name.startswith(self.project)
                run(["docker", "volume", "rm", "-f", name], env=self.env, check=False)
            run(["docker", "image", "rm", "-f", self.tag], env=self.env, check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)


def main():
    prepare_only = "--prepare-only" in sys.argv
    reason = skip_reason(prepare_only)
    if reason:
        print(f"SKIPPED: {reason}; this is not a pass")
        return 2
    for mode in MODES:
        smoke, failed = Smoke(mode), True
        try:
            smoke.prepare()
            if not prepare_only:
                smoke.scenario()
            failed = False
            print(f"{'config ok (prepare only)' if prepare_only else 'compose smoke passed'}: {smoke.project}")
        except (AssertionError, RuntimeError, subprocess.TimeoutExpired, OSError, KeyError) as exc:
            print(f"FAILED ({mode}): {exc!r}")
            return 1
        finally:
            smoke.cleanup(failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
