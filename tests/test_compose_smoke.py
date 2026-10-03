"""Fresh Docker Compose smoke test (stdlib only; needs the Docker CLI + Compose v2, and a running daemon for the full run).

Runs the real compose.yml with a minimal override under a unique `pvg-smoke-*` project: own container names,
own gateway image tag, loopback ephemeral port, fresh volumes, real pinned Qdrant, hash embeddings, a temp bare
Git repo (one synthetic Markdown commit) mounted only into sync, dummy secret files, explicit --env-file.
Cleanup removes only that project's containers/volumes, its image tag and its temp dir.
Exit 0 = passed, 1 = failed, 2 = skipped (no Docker CLI, no Compose v2, or no running daemon for the full run); a skip
is not a pass. `--prepare-only` still needs the Docker CLI and Compose v2 (`docker compose config`) but no daemon.
"""
import json
import os
import secrets
import shutil
import socket
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
DROP = ("COMPOSE_", "VAULT_", "GATEWAY_", "EMBEDDING_", "QDRANT_", "CLOUDFLARE_", "ADMIN_PASSWORD", "HOST_ID")
SHIM = '#!/bin/sh\n[ "$1" = clone ] && until [ -e /smoke/gate/release ]; do sleep 0.2; done\nPATH="${PATH#/smoke/bin:}"\nexec git "$@"\n'


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


class Smoke:
    def __init__(self):
        self.uid = secrets.token_hex(4)
        self.project = f"pvg-smoke-{self.uid}"
        self.tag = f"{self.project}-gateway:smoke"  # never persona-vault-gateway:local
        self.tmp = Path(tempfile.mkdtemp(prefix=f"{self.project}-"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(DROP)}
        self.proc = None
        self.started = False

    def base(self):
        return ["docker", "compose", "-p", self.project, "-f", str(ROOT / "compose.yml"),
                "-f", str(self.tmp / "override.yml"), "--env-file", str(self.tmp / "smoke.env")]

    def compose(self, *args, **kw):
        return run([*self.base(), *args], env=self.env, **kw)

    def prepare(self):
        t, q = self.tmp, json.dumps
        for d in ("seed", "work", "gitcfg", "bin", "gate", "secrets", "home"):
            (t / d).mkdir()
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
            f"ADMIN_PASSWORD={secrets.token_hex(16)}", "CLOUDFLARE_ACCOUNT_ID=dummy", "CLOUDFLARE_API_TOKEN=dummy",
            "EMBEDDING_PROVIDER=hash", "GATEWAY_BIND_ADDR=127.0.0.1", f"GATEWAY_HOST_PORT={self.port}",
            f"QDRANT_COLLECTION=pvg_smoke_{self.uid}", ""]))
        mounts = "\n".join(f"      - {q(m)}" for m in (
            f"{t}/seed:/seed:ro", f"{t}/gitcfg:/smoke/gitcfg", f"{t}/bin:/smoke/bin:ro", f"{t}/gate:/smoke/gate:ro"))
        (t / "override.yml").write_text(f"""services:
  persona-vault-gateway:
    image: {self.tag}
    container_name: {self.project}-gateway
  qdrant:
    container_name: {self.project}-qdrant
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
            assert svc["container_name"].startswith(self.project), f"{name}: container_name not isolated"
        gw = cfg["services"]["persona-vault-gateway"]
        assert gw["image"] == self.tag
        assert all(v["name"].startswith(self.project) for v in cfg["volumes"].values()), "volume not isolated"
        assert gw["depends_on"]["persona-vault-sync"]["condition"] == "service_healthy"
        assert [(p["host_ip"], int(p["published"])) for p in gw["ports"]] == [("127.0.0.1", self.port)]

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

    def scenario(self):
        self.compose("build")
        self.started = True
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
        code, body = self.http("GET", "/readyz")  # no collection yet: 503, or 200 with rag_indexed=false
        assert code in (200, 503) and body.get("rag_indexed") is not True, (code, body)
        token = self.py("from gateway.core import Settings as S, init_db, generate_token as g, upsert_agent as u;"
                        "s=S.from_env();init_db(s.db_path);t=g();"
                        "u(s.db_path,'pvg-smoke',t,['conversation-log','agent-memo','vault-rag'],"
                        "['30_Conversations/raw']);print(t)")
        assert self.http("POST", "/gateway/v3/search", {"query": QUERY})[0] == 401
        self.py("from gateway.core import Settings as S, index_vault as i;print(i(S.from_env()))")
        code, body = self.http("GET", "/readyz")
        assert code == 200 and body["rag_indexed"] is True and body["chunks"] > 0, (code, body)
        code, body = self.http("POST", "/gateway/v3/search", {"query": QUERY, "limit": 5}, token)
        assert code == 200 and NOTE in [r["path"] for r in body["results"]], (code, body)
        note = {"kind": "note", "title": "pvg smoke", "body": "synthetic"}
        code, body = self.http("POST", "/gateway/v3/capture", note, token)
        assert code == 200 and body["path"].startswith("30_Conversations/raw/"), (code, body)
        self.compose("exec", "-T", "persona-vault-gateway", "test", "-f", f"/vault/{body['path']}")

    def cleanup(self, failed):
        assert self.project.startswith("pvg-smoke-")
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        if self.started:
            if failed:
                print(self.compose("logs", "--no-color", "--tail", "40", check=False).stdout[-3000:])
            self.compose("down", "-v", "--remove-orphans", "-t", "5", check=False)
            run(["docker", "image", "rm", "-f", self.tag], env=self.env, check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)


def main():
    prepare_only = "--prepare-only" in sys.argv
    reason = skip_reason(prepare_only)
    if reason:
        print(f"SKIPPED: {reason}; this is not a pass")
        return 2
    smoke, failed = Smoke(), True
    try:
        smoke.prepare()
        if not prepare_only:
            smoke.scenario()
        failed = False
        print(f"{'config ok (prepare only)' if prepare_only else 'compose smoke passed'}: {smoke.project}")
        return 0
    except (AssertionError, RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        smoke.cleanup(failed)


if __name__ == "__main__":
    sys.exit(main())
