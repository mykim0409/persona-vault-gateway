"""Fresh Docker Compose smoke test (stdlib only; needs the Docker CLI + Compose v2, and a running daemon for the full run).

Runs the real compose.yml (+ compose.build.yml, so the image is built from source) with a minimal override under a
unique `pvg-smoke-*` project: own container names, own gateway image tag, loopback ephemeral port, fresh volumes, a
temp bare Git repo (one synthetic Markdown commit) mounted only into sync, dummy secret files, explicit --env-file.
Two projects run one after the other: `keyword` is the base deployment (EMBEDDING_PROVIDER=none, no Cloudflare keys,
no Qdrant) and `semantic` enables the `semantic` profile (real pinned Qdrant, hash embeddings).
Cleanup removes only that project's containers/volumes, its image tag and its temp dir.
The keyword project also runs the first-run initializer (`persona-vault-init`) from the already built image against
an EMPTY temp deployment directory (a copy of the base compose.yml; never the repository, no Docker socket, no
network credentials), then force-recreates the Gateway to prove capture and auth persist in the volumes, then
stops the stack and copies the synthetic Vault and SQLite volumes with the same image (tarfile) into new volumes.
Exit 0 = passed, 1 = failed, 2 = skipped (no Docker CLI, no Compose v2, or no running daemon for the full run); a skip
is not a pass. `--prepare-only` still needs the Docker CLI and Compose v2 (`docker compose config`) but no daemon.
"""
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "/tmp/pv-sync-clone-ready"
NOTE = "00_Inbox/pvg-smoke-note.md"
QUERY = "quartz heron canary"
# Variables compose.yml interpolates; never inherit them from the caller's shell (shell beats --env-file).
DROP = ("COMPOSE_", "PVG_", "FORWARDED_", "VAULT_", "GATEWAY_", "EMBEDDING_", "QDRANT_", "CLOUDFLARE_", "ADMIN_PASSWORD",
        "HOST_ID")
MODES = ("keyword", "semantic")
SHIM = '#!/bin/sh\n[ "$1" = clone ] && until [ -e /smoke/gate/release ]; do sleep 0.2; done\nPATH="${PATH#/smoke/bin:}"\nexec git "$@"\n'
INIT_URL = "git@github.com:pvg-smoke/synthetic-vault.git"  # never contacted: init without --check makes no network call
# Run inside the Gateway image: same file contents in two trees, and the copied SQLite file opens and is intact.
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


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("pvg_bootstrap", ROOT / "gateway/bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(path):
    """Relative path -> (bytes, mode) for every regular file below `path`."""
    return {str(p.relative_to(path)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in sorted(path.rglob("*")) if p.is_file()}


def verify_init(deploy, output, url, password):
    """What a successful first run must have produced in `deploy` and (not) printed."""
    bs, mode = load_bootstrap(), lambda p: stat.S_IMODE(p.stat().st_mode)
    secrets_dir, env = deploy / "secrets", deploy / ".env"
    assert sorted(p.name for p in deploy.rglob("*") if p.name != "compose.yml") == [
        ".env", "github_known_hosts", "persona_vault_sync", "persona_vault_sync.pub", "secrets"], "unexpected files"
    assert not any(p.is_symlink() for p in deploy.rglob("*"))
    assert (mode(env), mode(secrets_dir), mode(secrets_dir / "persona_vault_sync")) == (0o600, 0o700, 0o600)
    owner = deploy.stat().st_uid
    assert all(p.stat().st_uid == owner for p in deploy.rglob("*")), "files not handed back to the directory owner"
    pub = (secrets_dir / "persona_vault_sync.pub").read_text().strip()
    assert re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+ persona-vault-sync", pub) and pub in output
    assert (secrets_dir / "github_known_hosts").read_text() == bs.github_known_hosts_line()
    wanted = {"VAULT_REPO_SSH_URL": url, "ADMIN_PASSWORD": password, "GATEWAY_BIND_ADDR": "127.0.0.1",
              "EMBEDDING_PROVIDER": "none", "CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_API_TOKEN": ""}
    assert {k: bs.read_env_value(env, k) for k in wanted} == wanted
    assert not any(w in env.read_text().lower() for w in ("compose_file", "public_ip", "https", "caddy"))
    private = (secrets_dir / "persona_vault_sync").read_text()
    assert "OPENSSH PRIVATE KEY" in private and private.splitlines()[1] not in output and password not in output


class Smoke:
    def __init__(self, mode="keyword"):
        assert mode in MODES
        self.mode = mode
        self.uid = secrets.token_hex(4)
        self.project = f"pvg-smoke-{mode[:3]}-{self.uid}"
        self.tag = f"{self.project}-gateway:smoke"  # never persona-vault-gateway:local
        self.tmp = Path(tempfile.mkdtemp(prefix=f"{self.project}-"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(DROP)}
        self.proc = None
        self.started = False
        self.deploy = self.tmp / "deploy"  # EMPTY deployment dir for the initializer (plus a copy of compose.yml)
        self.init_started = False
        self.vols = {}
        self.scratch_volumes = []

    def base(self):
        profile = ["--profile", "semantic"] if self.mode == "semantic" else []
        return ["docker", "compose", "-p", self.project, *profile, "-f", str(ROOT / "compose.yml"),
                "-f", str(ROOT / "compose.build.yml"), "-f", str(self.tmp / "override.yml"),
                "--env-file", str(self.tmp / "smoke.env")]

    def compose(self, *args, **kw):
        return run([*self.base(), *args], env=self.env, **kw)

    def init_compose(self, *args, **kw):
        """Base compose copied into the empty deployment dir, tools profile, the already built smoke image."""
        return run(["docker", "compose", "-p", f"{self.project}-init", "--profile", "tools",
                    "-f", str(self.deploy / "compose.yml"), "--env-file", str(self.tmp / "init.env"), *args],
                   env=self.env, **kw)

    def prepare(self):
        t, q = self.tmp, json.dumps
        for d in ("seed", "work", "gitcfg", "bin", "gate", "secrets", "home"):
            (t / d).mkdir()
        self.deploy.mkdir()
        shutil.copy(ROOT / "compose.yml", self.deploy / "compose.yml")
        (t / "init.env").write_text(f"PVG_IMAGE={self.tag}\n")
        genv = {"PATH": os.environ["PATH"], "HOME": str(t / "home"), "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "smoke", "GIT_AUTHOR_EMAIL": "s@localhost",
                "GIT_COMMITTER_NAME": "smoke", "GIT_COMMITTER_EMAIL": "s@localhost"}
        git = lambda *a, cwd=t: run(["git", *a], env=genv, cwd=cwd)
        git("init", "--bare", "-b", "main", str(t / "seed/vault.git"))
        git("clone", str(t / "seed/vault.git"), str(t / "work/v"))
        (t / "work/v/00_Inbox").mkdir()
        (t / "work/v" / NOTE).write_text(f"# PVG smoke note\n\nSynthetic {QUERY} sentence for the compose smoke test.\n")
        git("add", ".", cwd=t / "work/v")
        git("commit", "-m", "synthetic smoke note", cwd=t / "work/v")
        git("push", "origin", "HEAD:main", cwd=t / "work/v")
        (t / "gitcfg/config").write_text("[safe]\n\tdirectory = *\n")  # file:// clone of a host-owned repo
        (t / "bin/git").write_text(SHIM)  # holds only `git clone` until gate/release exists
        (t / "bin/git").chmod(0o755)
        (t / "secrets/key").write_text("dummy-not-a-key\n")
        (t / "secrets/hosts").write_text("dummy\n")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        (t / "smoke.env").write_text("\n".join([
            "VAULT_REPO_SSH_URL=file:///seed/vault.git", "VAULT_SYNC_INTERVAL_SECONDS=3600", "HOST_ID=pvg-smoke",
            f"ADMIN_PASSWORD={secrets.token_hex(16)}",  # no Cloudflare keys: they are optional
            f"EMBEDDING_PROVIDER={'hash' if self.mode == 'semantic' else 'none'}",
            "GATEWAY_BIND_ADDR=127.0.0.1", f"GATEWAY_HOST_PORT={self.port}",
            f"QDRANT_COLLECTION=pvg_smoke_{self.uid}", ""]))
        mounts = "\n".join(f"      - {q(m)}" for m in (
            f"{t}/seed:/seed:ro", f"{t}/gitcfg:/smoke/gitcfg", f"{t}/bin:/smoke/bin:ro", f"{t}/gate:/smoke/gate:ro"))
        (t / "override.yml").write_text(f"""services:
  persona-vault-gateway:
    image: {self.tag}
    container_name: {self.project}-gateway
  qdrant:
    container_name: {self.project}-qdrant
  persona-vault-init:
    image: {self.tag}
  persona-vault-sync:
    container_name: {self.project}-sync
    environment:
      GIT_CONFIG_GLOBAL: /smoke/gitcfg/config
      PATH: /smoke/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    volumes:
{mounts}
secrets:
  persona_vault_sync_key: {{file: {q(str(t / 'secrets/key'))}}}
  github_known_hosts: {{file: {q(str(t / 'secrets/hosts'))}}}
""")
        cfg = json.loads(self.compose("config", "--format", "json").stdout)
        for name, svc in cfg["services"].items():
            if name != "persona-vault-init":  # a one-off `run` container, never named
                assert svc["container_name"].startswith(self.project), f"{name}: container_name not isolated"
            assert not any("docker.sock" in str(v) for v in svc.get("volumes", [])), f"{name}: Docker socket mounted"
        gw = cfg["services"]["persona-vault-gateway"]
        assert gw["image"] == self.tag
        assert all(v["name"].startswith(self.project) for v in cfg["volumes"].values()), "volume not isolated"
        assert gw["depends_on"] == {"persona-vault-sync": {"condition": "service_healthy", "required": True}}, \
            "gateway must wait only for the initial clone, not for Qdrant"
        assert ("qdrant" in cfg["services"]) == (self.mode == "semantic"), "Qdrant runs only with the semantic profile"
        assert gw["environment"]["EMBEDDING_PROVIDER"] == ("hash" if self.mode == "semantic" else "none")
        assert [(p["host_ip"], int(p["published"])) for p in gw["ports"]] == [("127.0.0.1", self.port)]
        assert "build" in gw and gw["build"]["context"] == str(ROOT), "compose.build.yml must add the source build"
        # The tools-profile init service: same image, python -m gateway.bootstrap, project dir at /setup, tty/stdin.
        init = json.loads(self.compose("--profile", "tools", "config", "--format", "json").stdout)["services"]["persona-vault-init"]
        assert init["image"] == self.tag and init["entrypoint"] == ["python", "-m", "gateway.bootstrap"]
        assert init["stdin_open"] and init["tty"]
        assert [(v["source"], v["target"]) for v in init["volumes"]] == [(str(ROOT), "/setup")]
        # The same init service from the copied base compose, before any .env or secret file exists.
        copied = json.loads(self.init_compose("config", "--format", "json").stdout)["services"]["persona-vault-init"]
        assert copied["image"] == self.tag and "build" not in copied
        assert [(v["source"], v["target"]) for v in copied["volumes"]] == [(str(self.deploy), "/setup")]
        assert not any(p.name in (".env", "secrets") for p in self.deploy.iterdir())
        self.vols = {k: v["name"] for k, v in cfg["volumes"].items()}

    def initializer(self):
        """Run the real first-run initializer in an empty directory, then re-run it."""
        password = f"{secrets.token_hex(12)}-Zq$"
        self.init_started = True
        first = self.init_compose("run", "--rm", "-T", "persona-vault-init",
                                  input=f"{INIT_URL}\n{password}\n{password}\n").stdout
        verify_init(self.deploy, first, INIT_URL, password)
        before = tree(self.deploy)
        again = self.init_compose("run", "--rm", "-T", "persona-vault-init", input="").stdout  # a prompt would fail
        assert tree(self.deploy) == before, "re-run changed existing files"
        assert "kept existing .env" in again and password not in again
        (self.deploy / "secrets/persona_vault_sync.pub").unlink()
        recovered = self.init_compose("run", "--rm", "-T", "persona-vault-init", input="").stdout
        assert tree(self.deploy) == before, "public key was not re-derived identically"
        assert before["secrets/persona_vault_sync.pub"][0].decode().strip() in recovered

    def state(self, svc, fmt="{{.State.Running}}"):
        r = run(["docker", "inspect", "-f", fmt, f"{self.project}-{svc}"], env=self.env, check=False)
        return r.stdout.strip() if r.returncode == 0 else ""

    def marker(self):
        r = run(["docker", "exec", f"{self.project}-sync", "test", "-f", MARKER], env=self.env, check=False)
        return r.returncode == 0

    def http(self, method, path, body=None, token=None):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def http_ok(self, path):
        try:
            return self.http("GET", path)[0] == 200
        except OSError:
            return False

    def py(self, code):
        return self.compose("exec", "-T", "persona-vault-gateway", "python", "-c", code).stdout.strip()

    def digest(self, path):
        return self.py(f"import hashlib;print(hashlib.sha256(open('/vault/{path}','rb').read()).hexdigest())")

    def volume_run(self, mounts, code, *argv):
        """One-off container from the smoke image: no network, no Docker socket, only the named volumes."""
        flags = [f for m in mounts for f in ("-v", m)]
        return run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", *flags, self.tag,
                    "-c", code, *argv], env=self.env).stdout.strip()

    def recreate_persists(self, token, captured, digest):
        """The container is replaced, the volumes are not: auth (SQLite) and Vault content survive."""
        before = self.state("gateway", "{{.Id}}")
        self.compose("up", "-d", "--force-recreate", "--no-deps", "persona-vault-gateway")
        assert before and self.state("gateway", "{{.Id}}") not in ("", before), "gateway was not recreated"
        wait(lambda: self.http_ok("/healthz"), "liveness after force-recreate")
        code, body = self.http("POST", "/gateway/v3/search", {"query": QUERY, "limit": 5}, token)
        assert code == 200 and NOTE in [r["path"] for r in body["results"]], (code, body)
        assert self.http("POST", "/gateway/v3/search", {"query": QUERY})[0] == 401
        assert self.digest(captured) == digest, "captured note changed or vanished after recreate"

    def backup_restore(self, captured, digest):
        """Stopped stack: tar both volumes into a scratch volume, extract into fresh volumes, compare."""
        vault, data = self.vols["persona-vault"], self.vols["persona-vault-gateway-data"]
        bak, rv, rd = (f"{self.project}-{n}" for n in ("bak", "restore-vault", "restore-data"))
        self.scratch_volumes = [bak, rv, rd]
        self.compose("stop", "-t", "10")
        for src, name in ((vault, "vault.tar"), (data, "data.tar")):
            self.volume_run([f"{src}:/src:ro", f"{bak}:/bak"], TAR_PY, name)
        for dst, name in ((rv, "vault.tar"), (rd, "data.tar")):
            self.volume_run([f"{bak}:/bak:ro", f"{dst}:/dst"], UNTAR_PY, name)
        assert self.volume_run([f"{vault}:/a:ro", f"{rv}:/b:ro"], COMPARE_PY, "/a", "/b").startswith("equal")
        assert self.volume_run([f"{data}:/a:ro", f"{rd}:/b:ro"], COMPARE_PY, "/a", "/b", "gateway.db").startswith("equal")
        sha = "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())"
        assert self.volume_run([f"{rv}:/vault:ro"], sha, f"/vault/{captured}") == digest

    def scenario(self):
        self.compose("build")
        self.started = True
        if self.mode == "keyword":
            self.initializer()
        self.proc = subprocess.Popen([*self.base(), "up", "-d"], env=self.env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        wait(lambda: self.state("sync") == "true", "sync container")
        for _ in range(10):  # clone is held at the gate: no marker, sync not healthy, gateway not started
            assert self.proc.poll() is None, "compose up returned before clone was ready"
            assert not self.marker() and self.state("sync", "{{.State.Health.Status}}") != "healthy"
            assert self.state("gateway") != "true", "gateway started before initial clone completed"
            time.sleep(0.5)
        (self.tmp / "gate/release").touch()
        _, err = self.proc.communicate(timeout=300)
        assert self.proc.returncode == 0, err[-800:]
        assert self.marker() and self.state("gateway") == "true"
        wait(lambda: self.http_ok("/healthz"), "liveness")
        code, body = self.http("GET", "/readyz")
        if self.mode == "keyword":  # semantic search is off, so the Gateway is ready without any vector store
            assert code == 200 and body["semantic"] == "disabled" and body["rag_indexed"] is False, (code, body)
            assert self.state("qdrant") == "", "Qdrant container must not exist in the base deployment"
        else:  # no collection yet: 503, or 200 with rag_indexed=false
            assert code in (200, 503) and body.get("rag_indexed") is not True, (code, body)
        token = self.py("from gateway.core import Settings as S, init_db, generate_token as g, upsert_agent as u;"
                        "s=S.from_env();init_db(s.db_path);t=g();"
                        "u(s.db_path,'pvg-smoke',t,['conversation-log','agent-memo','vault-rag'],"
                        "['30_Conversations/raw']);print(t)")
        assert self.http("POST", "/gateway/v3/search", {"query": QUERY})[0] == 401
        if self.mode == "semantic":
            self.py("from gateway.core import Settings as S, index_vault as i;print(i(S.from_env()))")
            code, body = self.http("GET", "/readyz")
            assert code == 200 and body["rag_indexed"] is True and body["chunks"] > 0, (code, body)
        code, body = self.http("POST", "/gateway/v3/search", {"query": QUERY, "limit": 5}, token)
        assert code == 200 and NOTE in [r["path"] for r in body["results"]], (code, body)
        note = {"kind": "note", "title": "pvg smoke", "body": "synthetic"}
        code, body = self.http("POST", "/gateway/v3/capture", note, token)
        assert code == 200 and body["path"].startswith("30_Conversations/raw/"), (code, body)
        self.compose("exec", "-T", "persona-vault-gateway", "test", "-f", f"/vault/{body['path']}")
        digest = self.digest(body["path"])
        self.recreate_persists(token, body["path"], digest)
        if self.mode == "keyword":
            self.backup_restore(body["path"], digest)

    def cleanup(self, failed):
        assert self.project.startswith("pvg-smoke-")
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        if self.started:
            if failed:
                print(self.compose("logs", "--no-color", "--tail", "40", check=False).stdout[-3000:])
            self.compose("down", "-v", "--remove-orphans", "-t", "5", check=False)
            for name in self.scratch_volumes:
                assert name.startswith(self.project)
                run(["docker", "volume", "rm", "-f", name], env=self.env, check=False)
            run(["docker", "image", "rm", "-f", self.tag], env=self.env, check=False)
        if self.init_started:
            self.init_compose("down", "-v", "--remove-orphans", "-t", "5", check=False)
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
        except (AssertionError, RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            print(f"FAILED ({mode}): {exc}")
            return 1
        finally:
            smoke.cleanup(failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
