"""Managed onboarding tests: claim, deploy key, clone, in-process sync, readiness gating, legacy compatibility.

Temp directories and synthetic data only. No Docker, no cloud, no real Git host and no credentials: the SSH transport
is replaced by a synthetic local bare repository (only `remote_url` and the allowed Git protocol are patched), and
`ssh-keygen` is used for real only when it is installed. The Gateway runs on a temporary localhost port.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway.app as app_module
from gateway import bootstrap, onboarding, server
from gateway.app import ADMIN_COOKIE, app
from gateway.core import init_db, upsert_agent

URL = "git@github.com:synthetic-owner/synthetic-vault.git"
OTHER_URL = "git@github.com:synthetic-owner/other-vault.git"
SETUP_TOKEN = "synthetic-setup-token-0123456789"
PASSWORD = "pässwörd-synthetic-비밀번호-1"
HAS_KEYGEN = shutil.which("ssh-keygen") is not None
HAS_GIT = shutil.which("git") is not None


class Reply:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.text = body.decode("utf-8", "replace")

    def header(self, name: str) -> str | None:
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)

    def cookie(self) -> str | None:
        for key, value in self.headers:
            match = key.lower() == "set-cookie" and re.match(rf"{ADMIN_COOKIE}=([^;]+)", value)
            if match:
                return match.group(1)
        return None

    def json(self) -> object:
        return json.loads(self.text)


class Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.seen: list[str] = []  # every response body, to prove nothing secret was returned

    def send(self, method: str, path: str, *, form=None, raw=None, headers=None, cookie=None, origin=True,
             chunked=False, token=None) -> Reply:
        sent = dict(headers or {})
        body = raw
        if form is not None:
            body = urlencode(form).encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            sent["Cookie"] = f"{ADMIN_COOKIE}={cookie}"
        if token:
            sent["Authorization"] = f"Bearer {token}"
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        if method == "POST" and origin is True:
            sent["Origin"] = f"http://127.0.0.1:{self.port}"
        elif isinstance(origin, str):
            sent["Origin"] = origin
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=body, headers=sent, encode_chunked=chunked)
            response = connection.getresponse()
            reply = Reply(response.status, response.getheaders(), response.read())
        finally:
            connection.close()
        self.seen.append(reply.text)
        return reply

    def claim(self, code: str = SETUP_TOKEN, password: str = PASSWORD, confirm: str | None = None, **kwargs) -> Reply:
        return self.send("POST", "/setup", form={"setup_code": code, "password": password,
                                                  "confirm": password if confirm is None else confirm}, **kwargs)

    def csrf(self, cookie: str) -> str:
        page = self.send("GET", "/admin/vault", cookie=cookie)
        return re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)

    def post(self, cookie: str, path: str, form: dict | None = None, **kwargs) -> Reply:
        return self.send("POST", path, form={"csrf_token": self.csrf(cookie), **(form or {})}, cookie=cookie, **kwargs)


@contextlib.contextmanager
def running_gateway():
    out = io.StringIO()
    with socket.socket() as listener, patch("sys.stdout", out):
        listener.bind(("127.0.0.1", 0))
        server_ = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server_.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(300):
                if server_.started:
                    break
                time.sleep(0.02)
            assert server_.started, "temporary Gateway did not start"
            client = Client(listener.getsockname()[1])
            client.stdout = out
            yield client
        finally:
            server_.should_exit = True
            thread.join(timeout=20)
            assert not thread.is_alive(), "temporary Gateway did not stop"


def wait_for(predicate, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting for a condition")


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": tempfile.gettempdir(), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true",
           "GIT_AUTHOR_NAME": "Synthetic", "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
           "GIT_COMMITTER_NAME": "Synthetic", "GIT_COMMITTER_EMAIL": "synthetic@example.invalid"}
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result.stdout


def make_bare(root: Path, name: str = "remote.git", files: dict[str, str] | None = None) -> Path:
    """A synthetic bare repository; `files={}` leaves it empty (no commits)."""
    bare = root / name
    git("init", "--bare", "-b", "main", str(bare))
    files = {"README.md": "# synthetic vault\nline one\n"} if files is None else files
    if files:
        seed = root / f"{name}.seed"
        git("clone", str(bare), str(seed))
        for rel, text in files.items():
            (seed / rel).parent.mkdir(parents=True, exist_ok=True)
            (seed / rel).write_text(text)
        git("add", ".", cwd=seed)
        git("commit", "-m", "seed", cwd=seed)
        git("push", "origin", "HEAD:main", cwd=seed)
    return bare


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.data = self.tmp / "data"
        env = patch.dict(os.environ, {
            "PVG_ONBOARDING": "managed", "PVG_DATA_DIR": str(self.data), "VAULT_DIR": str(self.data / "vault"),
            "DB_PATH": str(self.data / "gateway.db"), "EMBEDDING_PROVIDER": "none", "PVG_SETUP_TOKEN": SETUP_TOKEN,
            "VAULT_SYNC_INTERVAL_SECONDS": "1", "HOST_ID": "synthetic-host",
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ADMIN_PASSWORD", None)
        app_module.admin_login_attempts.clear()
        self.addCleanup(onboarding.stop)
        offline = patch.object(urllib.request, "urlopen", side_effect=AssertionError("no network in tests"))
        offline.start()
        self.addCleanup(offline.stop)

    def point_remote_at(self, bare: Path | None) -> None:
        """Replace the SSH transport by a local bare repository; this is the only mocked external surface."""
        target = (lambda url: str(bare)) if bare else (lambda url: str(self.tmp / "missing.git"))
        for patcher in (patch.object(onboarding, "remote_url", target), patch.object(onboarding, "ALLOWED_PROTOCOLS", "file")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def claim_directly(self) -> None:
        onboarding.ensure_private_dir(onboarding.setup_dir())
        onboarding.claim(SETUP_TOKEN, PASSWORD, PASSWORD)

    def connect(self, url: str = URL) -> onboarding.Manager:
        mgr = onboarding.manager()
        mgr.configure(url)
        mgr.connect()
        wait_for(lambda: read_state() in ("ready", "failed"))
        return mgr


def read_state() -> str:
    return onboarding.read_vault_state()["state"]


class ServerTest(unittest.TestCase):
    def test_port_default_and_validation(self) -> None:
        self.assertEqual(server.port_from_env({}), 8000)
        self.assertEqual(server.port_from_env({"PORT": "9001"}), 9001)
        for bad in ("0", "65536", "abc", "-1", "80;id", "١٢٣"):
            with self.subTest(port=bad), self.assertRaises(ValueError):
                server.port_from_env({"PORT": bad})

    def test_main_launches_one_worker_in_managed_mode(self) -> None:
        run = MagicMock()
        with patch.dict(os.environ, {"PORT": "9123", "PVG_DATA_DIR": "/srv/pvg"}, clear=True):
            self.assertEqual(server.main(run=run), 0)
            run.assert_called_once_with("gateway.app:app", host="0.0.0.0", port=9123, workers=1)
            self.assertEqual(os.environ["PVG_ONBOARDING"], "managed")
            self.assertEqual(os.environ["VAULT_DIR"], "/srv/pvg/vault")
            self.assertEqual(os.environ["DB_PATH"], "/srv/pvg/gateway.db")
            self.assertEqual(os.environ["EMBEDDING_PROVIDER"], "none")

    def test_explicit_legacy_variables_win(self) -> None:
        env = {"VAULT_DIR": "/legacy/vault", "DB_PATH": "/legacy/db.sqlite", "EMBEDDING_PROVIDER": "cloudflare"}
        with patch.dict(os.environ, env, clear=True):
            server.main(run=MagicMock())
            self.assertEqual((os.environ["VAULT_DIR"], os.environ["DB_PATH"]), ("/legacy/vault", "/legacy/db.sqlite"))
            self.assertEqual(os.environ["EMBEDDING_PROVIDER"], "cloudflare")
            self.assertEqual(os.environ["PVG_DATA_DIR"], "/data")

    def test_invalid_port_does_not_start(self) -> None:
        run = MagicMock()
        with patch.dict(os.environ, {"PORT": "nope"}, clear=True), patch("sys.stderr", io.StringIO()):
            self.assertEqual(server.main(run=run), 2)
        run.assert_not_called()


class LegacyTest(Base):
    """Without the managed entrypoint nothing new is reachable and ADMIN_PASSWORD keeps working."""

    def test_legacy_mode_is_unchanged(self) -> None:
        os.environ.pop("PVG_ONBOARDING")
        os.environ["ADMIN_PASSWORD"] = "legacy-admin-password-1"
        with running_gateway() as client:
            for method, path in (("GET", "/"), ("GET", "/setup"), ("POST", "/setup"), ("GET", "/admin/vault"),
                                 ("POST", "/admin/vault"), ("POST", "/admin/vault/connect")):
                reply = client.send(method, path)
                self.assertEqual((reply.status, reply.json()), (404, {"detail": "Not Found"}), (method, path))
            self.assertEqual(client.send("GET", "/healthz").status, 200)
            self.assertEqual(client.send("GET", "/readyz").status, 200)
            self.assertTrue((self.data / "vault").is_dir())  # legacy startup still creates the Vault directory
            login = client.send("POST", "/admin/login", form={"password": "legacy-admin-password-1"})
            self.assertEqual((login.status, login.header("location")), (303, "/admin/tokens"))
            page = client.send("GET", "/admin/tokens", cookie=login.cookie())
            self.assertEqual(page.status, 200)
            self.assertNotIn("Vault sync", page.text)
            self.assertEqual(client.send("POST", "/admin/login", form={"password": "wrong"}).status, 401)

    def test_legacy_without_password_stays_disabled(self) -> None:
        os.environ.pop("PVG_ONBOARDING")
        with running_gateway() as client:
            self.assertEqual(client.send("GET", "/admin/login").status, 503)
            self.assertEqual(client.send("POST", "/admin/login", form={"password": "x"}).status, 503)

    def test_legacy_cookie_is_keyed_by_the_password(self) -> None:
        os.environ.pop("PVG_ONBOARDING")
        os.environ["ADMIN_PASSWORD"] = "legacy-admin-password-1"
        self.assertEqual(app_module.admin_signing_secret(), "legacy-admin-password-1")
        os.environ["PVG_ONBOARDING"] = "managed"
        self.assertIsNone(app_module.admin_signing_secret())  # the env password is not a managed credential


class ClaimTest(Base):
    def test_unclaimed_routes_and_health(self) -> None:
        self._unclaimed_routes_and_health()

    def test_malformed_claim_record_fails_closed(self) -> None:
        setup = self.data / "setup"
        setup.mkdir(parents=True)
        for garbage in ("not json", "{}", json.dumps({"password_hash": "scrypt$x", "signing_secret": "short"}), ""):
            (setup / "claim.json").write_text(garbage)
            self.assertTrue(onboarding.claimed())  # an existing record is never treated as "unclaimed"
            self.assertIsNone(onboarding.signing_secret())
            self.assertFalse(onboarding.verify_password(PASSWORD))
            with self.assertRaises(onboarding.AlreadyClaimed):
                onboarding.claim(SETUP_TOKEN, PASSWORD, PASSWORD)
        with running_gateway() as client:
            self.assertEqual(client.send("GET", "/admin/login").status, 503)
            self.assertEqual(client.send("POST", "/admin/login", form={"password": PASSWORD}).status, 503)
            self.assertEqual(client.send("GET", "/setup").header("location"), "/")
            self.assertEqual(client.claim().header("location"), "/")
        self.assertEqual((setup / "claim.json").read_text(), "")  # untouched, never reclaimed
        # A well-formed record with an unusable hash accepts no password at all.
        record = json.dumps({"password_hash": "scrypt$16384$8$1$zz$zz", "signing_secret": "a" * 64})
        (setup / "claim.json").write_text(record)
        self.assertFalse(onboarding.verify_password(PASSWORD))
        with running_gateway() as client:
            self.assertEqual(client.send("POST", "/admin/login", form={"password": PASSWORD}).status, 401)
            self.assertEqual(client.claim().header("location"), "/")
        self.assertEqual((setup / "claim.json").read_text(), record)

    def test_secure_cookie_can_be_forced_without_trusting_proxies(self) -> None:
        with running_gateway() as client:
            plain = client.claim()
            self.assertNotIn("Secure", plain.header("set-cookie"))
        os.environ["PVG_SECURE_COOKIES"] = "true"
        with running_gateway() as client:
            forced = client.send("POST", "/admin/login", form={"password": PASSWORD})
            self.assertIn("Secure", forced.header("set-cookie"))
            # A spoofed forwarding header from an untrusted peer changes nothing.
            spoofed = client.send("POST", "/admin/login", form={"password": PASSWORD}, headers={"X-Forwarded-Proto": "http"})
            self.assertIn("Secure", spoofed.header("set-cookie"))

    def _unclaimed_routes_and_health(self) -> None:
        with running_gateway() as client:
            for path in ("/", "/admin/login"):
                reply = client.send("GET", path)
                self.assertEqual((reply.status, reply.header("location")), (303, "/setup"), path)
            page = client.send("GET", "/setup")
            self.assertEqual(page.status, 200)
            self.assertIn('action="/setup"', page.text)
            self.assertEqual(client.send("GET", "/healthz").status, 200)
            ready = client.send("GET", "/readyz")
            self.assertEqual((ready.status, ready.json()), (503, {"status": "pending", "setup": "claim"}))
            self.assertFalse((self.data / "vault").exists())  # no Vault directory before a clone is published

    def test_denials_leak_nothing(self) -> None:
        records: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        collector = Collect()
        logging.getLogger().addHandler(collector)
        self.addCleanup(logging.getLogger().removeHandler, collector)
        with running_gateway() as client:
            wrong = client.claim(code="WRONG-code-synthetic-value-xyz")
            self.assertEqual(wrong.status, 403)
            self.assertIsNone(wrong.cookie())
            mismatch = client.claim(confirm="another-password-synthetic-2")
            short = client.claim(password="short", confirm="short")
            self.assertEqual((mismatch.status, short.status), (400, 400))
            query = client.send("GET", f"/setup?setup_code={SETUP_TOKEN}")
            self.assertNotIn(SETUP_TOKEN, query.text)
            self.assertFalse(onboarding.claimed())
            # Password-policy errors come before the code check, so they never confirm the code is right.
            self.assertEqual(client.claim(code="WRONG-code-synthetic-value-xyz", password="short", confirm="short").status, 400)
        db = (self.data / "gateway.db").read_bytes()
        for secret in (SETUP_TOKEN, PASSWORD, "WRONG-code-synthetic-value-xyz"):
            self.assertFalse(any(secret in text for text in client.seen + records), secret)
            self.assertNotIn(secret.encode(), db)

    def test_rate_limit_applies_before_the_code_is_compared(self) -> None:
        with running_gateway() as client:
            statuses = [client.claim(code=f"wrong-{n}").status for n in range(5)]
            self.assertEqual(statuses, [403] * 5)
            limited = client.claim()  # even the correct code is refused while limited
            self.assertEqual(limited.status, 429)
            self.assertTrue(limited.header("Retry-After"))
            self.assertFalse(onboarding.claimed())

    def test_cross_site_claims_are_rejected_and_not_counted(self) -> None:
        with running_gateway() as client:
            for _ in range(8):
                self.assertEqual(client.claim(origin="http://evil.example").status, 403)
            self.assertEqual(client.claim(headers={"Sec-Fetch-Site": "cross-site"}, origin=False).status, 403)
            self.assertFalse(onboarding.claimed())
            self.assertEqual(client.claim().status, 303)  # the real operator was not locked out

    def test_form_bounds(self) -> None:
        with running_gateway() as client:
            big = urlencode({"setup_code": "x" * 5000, "password": "y", "confirm": "y"}).encode()
            declared = client.send("POST", "/setup", raw=big, headers={"Content-Type": "application/x-www-form-urlencoded"})
            streamed = client.send("POST", "/setup", raw=iter([big[:3000], big[3000:]]), chunked=True,
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
            wrong_type = client.send("POST", "/setup", raw=b"{}", headers={"Content-Type": "application/json"})
            self.assertEqual((declared.status, streamed.status, wrong_type.status), (413, 413, 415))
            self.assertFalse(onboarding.claimed())

    def test_successful_claim_hashes_the_password_and_closes_setup(self) -> None:
        with running_gateway() as client:
            reply = client.claim()
            self.assertEqual((reply.status, reply.header("location")), (303, "/admin/vault"))
            cookie = reply.cookie()
            self.assertTrue(cookie)
            self.assertIn("httponly", reply.header("set-cookie").lower())
            record = json.loads((self.data / "setup" / "claim.json").read_text())
            self.assertTrue(record["password_hash"].startswith("scrypt$"))
            self.assertGreaterEqual(len(record["signing_secret"]), 64)
            self.assertNotIn(record["signing_secret"], record["password_hash"])
            for path in self.data.rglob("*"):
                if path.is_file():
                    self.assertNotIn(PASSWORD.encode(), path.read_bytes(), path.name)
            self.assertEqual(stat.S_IMODE((self.data / "setup" / "claim.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((self.data / "setup").stat().st_mode), 0o700)
            # Setup is closed: the same code claims nothing, and the password is not replaced.
            again = client.claim(password="a-different-password-1234")
            self.assertEqual((again.status, again.header("location")), (303, "/"))
            self.assertTrue(onboarding.verify_password(PASSWORD))
            self.assertFalse(onboarding.verify_password("a-different-password-1234"))
            self.assertEqual(client.send("GET", "/setup").header("location"), "/")
            # The session works; login works; the legacy ADMIN_PASSWORD env var is not a managed credential.
            self.assertEqual(client.send("GET", "/admin/vault", cookie=cookie).status, 200)
            os.environ["ADMIN_PASSWORD"] = "legacy-admin-password-1"
            self.assertEqual(client.send("POST", "/admin/login", form={"password": "legacy-admin-password-1"}).status, 401)
            self.assertEqual(client.send("POST", "/admin/login", form={"password": PASSWORD}).status, 303)
            self.assertEqual(client.send("GET", "/").header("location"), "/admin/login")
            # Until the Vault is connected the token manager redirects to the connect page.
            self.assertEqual(client.send("GET", "/admin/tokens", cookie=cookie).header("location"), "/admin/vault")
            self.assertFalse(any(PASSWORD in text for text in client.seen))

    def test_restart_cannot_reopen_the_claim(self) -> None:
        with running_gateway() as client:
            cookie = client.claim().cookie()
        with running_gateway() as client:
            self.assertEqual(client.send("GET", "/setup").header("location"), "/")
            self.assertEqual(client.claim(code=SETUP_TOKEN, password="a-different-password-1234").header("location"), "/")
            self.assertEqual(client.send("GET", "/admin/vault", cookie=cookie).status, 200)  # same signing secret
            self.assertTrue(onboarding.verify_password(PASSWORD))
            self.assertEqual(client.stdout.getvalue(), "")  # nothing is announced once claimed

    def test_first_claimant_wins_under_concurrency(self) -> None:
        onboarding.ensure_private_dir(onboarding.setup_dir())
        barrier = threading.Barrier(12)
        results: dict[int, object] = {}

        def attempt(n: int) -> None:
            barrier.wait()
            try:
                results[n] = onboarding.claim(SETUP_TOKEN, f"concurrent-password-{n:02d}-xyz", f"concurrent-password-{n:02d}-xyz")
            except onboarding.SetupError as exc:
                results[n] = exc

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [n for n, value in results.items() if isinstance(value, str)]
        self.assertEqual(len(winners), 1)
        self.assertTrue(all(isinstance(v, onboarding.AlreadyClaimed) for n, v in results.items() if n not in winners))
        self.assertEqual(results[winners[0]], onboarding.signing_secret())
        self.assertTrue(onboarding.verify_password(f"concurrent-password-{winners[0]:02d}-xyz"))
        self.assertEqual(sum(onboarding.verify_password(f"concurrent-password-{n:02d}-xyz") for n in range(12)), 1)

    def test_http_race_sets_one_session(self) -> None:
        with running_gateway() as client:
            with patch.object(app_module, "ADMIN_LOGIN_MAX_ATTEMPTS", 50):
                replies: list[Reply] = []
                threads = [threading.Thread(target=lambda n=n: replies.append(client.claim(password=f"race-password-synthetic-{n}")))
                           for n in range(6)]
                [t.start() for t in threads]
                [t.join() for t in threads]
            self.assertEqual(sum(reply.cookie() is not None for reply in replies), 1)
            self.assertTrue(all(reply.status in (303, 409) for reply in replies))

    def test_generated_code_is_printed_once_and_stored_privately(self) -> None:
        del os.environ["PVG_SETUP_TOKEN"]
        with running_gateway() as client:
            first = client.stdout.getvalue()
            code = re.search(r"\(shown once\): (\S+)", first).group(1)
            self.assertGreaterEqual(len(code), 32)
            stored = self.data / "setup" / "code" if False else self.data / "setup" / "setup_code"
            self.assertEqual(stored.read_text().strip(), code)
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            self.assertNotIn(code, "".join(client.seen))
        with running_gateway() as client:  # restart while still unclaimed: the code is not printed again
            self.assertNotIn(code, client.stdout.getvalue())
            self.assertIn("unclaimed", client.stdout.getvalue())
            self.assertEqual(client.claim(code=code).status, 303)
            self.assertFalse(stored.exists())  # the claim code is gone once used
        with running_gateway() as client:
            self.assertEqual(client.claim(code=code, password="a-different-password-1234").header("location"), "/")

    def test_env_token_is_never_printed_and_weak_tokens_are_ignored(self) -> None:
        with running_gateway() as client:
            self.assertNotIn(SETUP_TOKEN, client.stdout.getvalue())
            self.assertFalse((self.data / "setup" / "setup_code").exists())
        os.environ["PVG_SETUP_TOKEN"] = "short-token"
        with running_gateway() as client:
            output = client.stdout.getvalue()
            self.assertIn("ignored", output)
            self.assertNotIn("short-token", output)
            code = re.search(r"\(shown once\): (\S+)", output).group(1)
            self.assertEqual(client.claim(code="short-token").status, 403)
            self.assertEqual(client.claim(code=code).status, 303)


class GatingTest(Base):
    def test_vault_operations_are_unavailable_until_ready(self) -> None:
        self.claim_directly()
        init_db(self.data / "gateway.db")
        token = "pvg_synthetic_gating_token_value_0001"
        upsert_agent(self.data / "gateway.db", "codex-agent", token, ["conversation-log", "agent-memo", "vault-rag"], ["30_Conversations/raw"])
        note = {"kind": "note", "title": "t", "body": "b"}
        with running_gateway() as client:
            cookie = client.send("POST", "/admin/login", form={"password": PASSWORD}).cookie()
            self.assertEqual(client.send("GET", "/healthz").status, 200)
            self.assertEqual(client.send("GET", "/readyz").json(), {"status": "pending", "setup": "vault"})
            for method, path, body in (("POST", "/gateway/v3/search", {"query": "x"}), ("POST", "/gateway/v3/capture", note),
                                       ("GET", "/gateway/v3/working-agreement", None), ("GET", "/gateway/v3/health", None)):
                reply = client.send(method, path, raw=body, token=token)
                self.assertEqual(reply.status, 503, path)
            self.assertEqual(client.send("POST", "/gateway/v3/capture", raw=note).status, 401)  # auth still comes first
            self.assertEqual(client.send("GET", "/gateway/v3/capabilities", token=token).status, 200)  # no Vault access
            retired = client.send("GET", "/gateway/v2/capabilities", token=token)
            self.assertEqual(retired.status, 410)
            self.assertEqual(retired.json()["detail"]["code"], "client_upgrade_required")
            rebuild = client.post(cookie, "/admin/rag/rebuild")
            self.assertEqual(rebuild.status, 503)
            self.assertEqual(client.send("GET", "/admin/tokens", cookie=cookie).header("location"), "/admin/vault")
            self.assertFalse((self.data / "vault").exists())

    def test_embedding_retry_waits_for_the_vault(self) -> None:
        stop = app_module.embedding_retry_stop
        stop.clear()
        calls = MagicMock()
        thread = threading.Thread(target=app_module.embedding_retry_loop, args=(MagicMock(),), daemon=True)
        with patch.object(app_module, "EMBEDDING_RETRY_POLL_SECONDS", 0.02), patch.object(app_module, "retry_due_embeddings", calls), \
                patch.object(onboarding, "vault_ready", return_value=False):
            thread.start()
            time.sleep(0.3)
            calls.assert_not_called()
        with patch.object(app_module, "EMBEDDING_RETRY_POLL_SECONDS", 0.02), patch.object(app_module, "retry_due_embeddings", calls), \
                patch.object(onboarding, "vault_ready", return_value=True):
            wait_for(lambda: calls.called, 5)
        stop.set()
        thread.join(timeout=5)


@unittest.skipUnless(HAS_GIT and HAS_KEYGEN, "git and ssh-keygen are required")
class VaultTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.bare = make_bare(self.tmp)
        self.claim_directly()
        init_db(self.data / "gateway.db")

    def test_invalid_urls_never_reach_git_or_create_keys(self) -> None:
        mgr = onboarding.manager()
        bad = ("", "https://github.com/o/r.git", "git@gitlab.com:o/r.git", "-oProxyCommand=id", "git@github.com:o/r.git; id",
               "ssh://git@github.com:2222/o/r.git", "file:///etc", "ext::sh -c id", "git@github.com.evil.example:o/r.git", "/srv/repo")
        with patch.object(onboarding, "run_git", side_effect=AssertionError("git must not run")):
            for text in bad:
                with self.subTest(url=text), self.assertRaises(onboarding.SetupError):
                    mgr.configure(text)
        self.assertEqual(read_state(), "unconfigured")
        self.assertFalse((self.data / "setup" / "secrets").exists())
        with self.assertRaises(onboarding.SetupError):
            mgr.connect()  # nothing configured yet

    def test_deploy_key_pinned_hosts_and_exact_instructions(self) -> None:
        mgr = onboarding.manager()
        mgr.configure(URL)
        paths = bootstrap.Paths(self.data / "setup")
        self.assertEqual(stat.S_IMODE(paths.key.stat().st_mode), 0o600)
        self.assertEqual(paths.known_hosts.read_text(), bootstrap.github_known_hosts_line())
        status = mgr.status()
        self.assertTrue(status["public_key"].startswith("ssh-ed25519 "))
        self.assertEqual(status["registration_link"], "https://github.com/synthetic-owner/synthetic-vault/settings/keys/new")
        private_body = [line for line in paths.key.read_text().splitlines() if line and "-----" not in line]
        with running_gateway() as client:
            cookie = client.send("POST", "/admin/login", form={"password": PASSWORD}).cookie()
            page = client.send("GET", "/admin/vault", cookie=cookie).text
            self.assertIn(status["public_key"], page)
            self.assertIn('href="https://github.com/synthetic-owner/synthetic-vault/settings/keys/new"', page)
            self.assertIn("Allow write access", page)
            self.assertFalse(any(line in page for line in private_body))
            self.assertNotIn("PRIVATE KEY", page)
        env = onboarding.git_environment()
        ssh = env["GIT_SSH_COMMAND"]
        for pinned in ("StrictHostKeyChecking=yes", "IdentitiesOnly=yes", f"UserKnownHostsFile={paths.known_hosts}",
                       f"-i {paths.key}", "BatchMode=yes", "GlobalKnownHostsFile=/dev/null"):
            self.assertIn(pinned, ssh)
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file" if onboarding.ALLOWED_PROTOCOLS == "file" else "ssh")
        self.assertEqual((env["GIT_CONFIG_GLOBAL"], env["GIT_TERMINAL_PROMPT"]), ("/dev/null", "0"))

    def test_git_runs_without_shell_and_without_environment_leaks(self) -> None:
        fake = MagicMock()
        fake.return_value.communicate.return_value = ("", "")
        fake.return_value.returncode = 0
        os.environ["ADMIN_PASSWORD"] = "must-not-reach-git-1234"
        before = dict(os.environ)
        with patch.object(onboarding.subprocess, "Popen", fake):
            onboarding.git_in(self.tmp, "status")
            onboarding.run_git(["-C", str(self.tmp), "clone", "--", "x", "y"], timeout=5)
        self.assertEqual(dict(os.environ), before)  # no per-job mutation of the process environment
        for call in fake.call_args_list:
            argv, kwargs = call.args[0], call.kwargs
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[0], "git")
            self.assertFalse(kwargs.get("shell", False))
            self.assertIn("-C", argv)
            self.assertNotIn("ADMIN_PASSWORD", kwargs["env"])
            self.assertNotIn("PVG_SETUP_TOKEN", kwargs["env"])

    def test_clone_failure_is_visible_then_retry_succeeds(self) -> None:
        self.point_remote_at(None)
        token = "pvg_synthetic_clone_token_value_0001"
        upsert_agent(self.data / "gateway.db", "codex-agent", token, ["conversation-log", "agent-memo", "vault-rag"], ["30_Conversations/raw"])
        with running_gateway() as client:
            cookie = client.send("POST", "/admin/login", form={"password": PASSWORD}).cookie()
            self.assertEqual(client.post(cookie, "/admin/vault", {"repo_url": URL}).header("location"), "/admin/vault?notice=vault-saved")
            self.assertEqual(client.post(cookie, "/admin/vault/connect").status, 303)
            wait_for(lambda: read_state() == "failed")
            page = client.send("GET", "/admin/vault", cookie=cookie).text
            self.assertIn("The repository was not found", page)
            self.assertIn("Retry", page)
            self.assertNotIn(str(self.tmp), page)  # Git's own message (with paths) is classified, never rendered
            self.assertEqual(onboarding.read_vault_state()["repo_url"], URL)  # configured state is kept
            self.assertFalse((self.data / "vault").exists())
            self.assertFalse(list(self.data.glob(".vault.pvg-staging")))
            self.assertEqual(client.send("GET", "/readyz").status, 503)
            note = {"kind": "note", "title": "before clone", "body": "must not be captured"}
            self.assertEqual(client.send("POST", "/gateway/v3/capture", raw=note, token=token).status, 503)
            self.assertFalse((self.data / "vault").exists())
            self.point_remote_at(self.bare)  # the repository now exists; retry from the same page
            self.assertEqual(client.post(cookie, "/admin/vault/connect").status, 303)
            wait_for(lambda: read_state() == "ready")
            self.assertEqual(client.send("GET", "/readyz").status, 200)
            self.assertTrue((self.data / "vault" / "README.md").is_file())
            self.assertEqual(client.send("POST", "/gateway/v3/capture", raw=note, token=token).status, 200)
            self.assertEqual(client.send("GET", "/admin/tokens", cookie=cookie).status, 200)

    def test_empty_and_partial_clones_never_become_the_vault(self) -> None:
        self.point_remote_at(make_bare(self.tmp, "empty.git", files={}))
        mgr = self.connect()
        self.assertEqual((read_state(), onboarding.read_vault_state()["error"]), ("failed", "empty"))
        self.assertFalse((self.data / "vault").exists())
        self.assertFalse(onboarding.vault_ready())

        def crash_midway(args, *, timeout):
            staging = Path(args[args.index("--") + 2])
            (staging / ".git").mkdir(parents=True)
            (staging / "half.md").write_text("partial")
            return 128, "", "fatal: early EOF"

        with patch.object(onboarding, "run_git", crash_midway):
            mgr.connect()
            wait_for(lambda: read_state() == "failed" and not mgr.clone_running())
        self.assertFalse((self.data / "vault").exists())
        self.assertEqual(list(self.data.glob(".vault.pvg-*")), [])  # this attempt's own staging is cleaned up
        self.assertFalse(onboarding.vault_ready())

    def test_stop_during_clone_does_not_publish_or_sync(self) -> None:
        self.point_remote_at(self.bare)
        started, release = threading.Event(), threading.Event()
        real = onboarding.run_git

        def slow_clone(args, *, timeout):
            result = real(args, timeout=timeout)
            if "clone" in args:
                started.set()
                release.wait(10)  # the clone has finished; stop() arrives before publication
            return result

        mgr = onboarding.manager()
        mgr.configure(URL)
        with patch.object(onboarding, "run_git", slow_clone):
            mgr.connect()
            self.assertTrue(started.wait(10))
            stopper = threading.Thread(target=onboarding.stop)
            stopper.start()
            time.sleep(0.3)
            release.set()
            stopper.join(30)
        self.assertFalse(stopper.is_alive())
        self.assertFalse((self.data / "vault").exists())  # nothing was published
        self.assertEqual(list(self.data.glob(".vault.pvg-*")), [])
        self.assertEqual(read_state(), "cloning")  # resumable on the next start
        self.assertIsNone(mgr.sync_thread)
        mgr.start_sync()
        self.assertIsNone(mgr.sync_thread)  # a stopped manager never starts syncing
        with self.assertRaises(onboarding.SetupError):
            mgr.connect()
        fresh = onboarding.manager()
        self.assertIsNot(fresh, mgr)
        fresh.resume()  # restart: the interrupted clone runs again and now publishes
        wait_for(lambda: read_state() == "ready")

    def test_stop_ends_only_its_own_git_child(self) -> None:
        """A running Git command gets SIGTERM after its grace period; the call returns and the child is gone."""
        stop = threading.Event()
        pids: list[int] = []
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            pids.append(proc.pid)
            return proc

        outcome: list[object] = []

        def worker() -> None:
            onboarding._worker.stop, onboarding._worker.grace = stop, 0.3
            try:
                outcome.append(onboarding.run_git(["-c", "alias.slow=!sleep 30", "slow"], timeout=60))
            except onboarding.GitFailure as exc:
                outcome.append(exc.code)

        with patch.object(onboarding.subprocess, "Popen", spy):
            thread = threading.Thread(target=worker)
            thread.start()
            wait_for(lambda: pids)
            time.sleep(0.3)
            began = time.monotonic()
            stop.set()
            thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["stopped"])
        self.assertLess(time.monotonic() - began, 10)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pids[0], 0)  # its process group is gone

    def test_nonempty_and_symlinked_destinations_are_left_alone(self) -> None:
        self.point_remote_at(self.bare)
        vault = self.data / "vault"
        vault.mkdir(parents=True)
        (vault / "notes.txt").write_text("unrelated personal data")
        mgr = self.connect()
        self.assertEqual((read_state(), onboarding.read_vault_state()["error"]), ("failed", "destination"))
        self.assertEqual((vault / "notes.txt").read_text(), "unrelated personal data")
        self.assertFalse((vault / ".git").exists())
        shutil.rmtree(vault)
        target = self.tmp / "elsewhere"
        target.mkdir()
        vault.symlink_to(target, target_is_directory=True)
        mgr.connect()
        wait_for(lambda: read_state() == "failed" and not mgr.clone_running())
        self.assertEqual(onboarding.read_vault_state()["error"], "destination")
        self.assertTrue(vault.is_symlink())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(onboarding.vault_ready())
        vault.unlink()
        # Pre-existing paths that look like staging names are never deleted or followed (staging is random and new).
        squat_dir = self.data / ".vault.pvg-staging"
        squat_dir.mkdir()
        (squat_dir / "sentinel.txt").write_text("unrelated")
        squat_link = self.data / ".vault.pvg-link"
        squat_link.symlink_to(target, target_is_directory=True)
        mgr.connect()
        wait_for(lambda: read_state() == "ready")
        self.assertEqual((squat_dir / "sentinel.txt").read_text(), "unrelated")
        self.assertTrue(squat_link.is_symlink())
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.data.glob(".vault.pvg-*")), [".vault.pvg-link", ".vault.pvg-staging"])

    def test_existing_repository_is_adopted_not_overwritten(self) -> None:
        self.point_remote_at(self.bare)
        vault = self.data / "vault"
        git("clone", str(self.bare), str(vault))
        (vault / "local-note.md").write_text("uncommitted local work")
        (vault / ".git" / "pvg-marker").write_text("same repository")
        self.connect()
        self.assertEqual(read_state(), "ready")
        self.assertEqual((vault / "local-note.md").read_text(), "uncommitted local work")
        self.assertTrue((vault / ".git" / "pvg-marker").exists())  # not re-cloned
        onboarding.stop()
        # A repository whose origin is something else is unrelated: refused and untouched.
        other = make_bare(self.tmp, "other.git", files={"other.md": "x"})
        shutil.rmtree(self.data / "vault")
        git("clone", str(other), str(vault))
        onboarding.write_vault_state(state="key-ready")
        mgr = self.connect()
        self.assertEqual((read_state(), onboarding.read_vault_state()["error"]), ("failed", "destination"))
        self.assertTrue((vault / "other.md").exists())
        mgr.stop()

    def test_sync_commits_pulls_and_survives_restart(self) -> None:
        self.point_remote_at(self.bare)
        mgr = self.connect()
        self.assertEqual(read_state(), "ready")
        vault = self.data / "vault"
        wait_for(lambda: mgr.status()["sync"]["last_ok"])
        (vault / "30_Conversations" / "raw").mkdir(parents=True)
        (vault / "30_Conversations" / "raw" / "a.md").write_text("synthetic capture")
        mgr.sync_now()
        wait_for(lambda: "30_Conversations/raw/a.md" in git("--git-dir", str(self.bare), "ls-tree", "-r", "--name-only", "main"))
        self.assertIn("Sync vault", git("--git-dir", str(self.bare), "log", "-1", "--format=%s", "main"))
        other = self.tmp / "other-clone"
        git("clone", str(self.bare), str(other))
        (other / "from-remote.md").write_text("remote edit")
        git("add", ".", cwd=other)
        git("commit", "-m", "remote", cwd=other)
        git("push", "origin", "HEAD:main", cwd=other)
        mgr.sync_now()
        wait_for(lambda: (vault / "from-remote.md").exists())
        # Stop is non-destructive; a new manager resumes the configured repository from persisted state.
        onboarding.stop()
        self.assertEqual(git("status", "--porcelain", cwd=vault), "")
        self.assertEqual(read_state(), "ready")
        resumed = onboarding.manager()
        resumed.resume()
        self.assertTrue(resumed.status()["sync"]["running"])
        (vault / "after-restart.md").write_text("later")
        resumed.sync_now()
        wait_for(lambda: "after-restart.md" in git("--git-dir", str(self.bare), "ls-tree", "-r", "--name-only", "main"))

    def test_conflict_blocks_sync_without_force_or_autoresolve(self) -> None:
        self.point_remote_at(self.bare)
        mgr = self.connect()
        vault = self.data / "vault"
        wait_for(lambda: mgr.status()["sync"]["last_ok"])
        other = self.tmp / "other-clone"
        git("clone", str(self.bare), str(other))
        (other / "README.md").write_text("# synthetic vault\nREMOTE line\n")
        git("commit", "-am", "remote edit", cwd=other)
        git("push", "origin", "HEAD:main", cwd=other)
        remote_head = git("--git-dir", str(self.bare), "rev-parse", "main").strip()
        (vault / "README.md").write_text("# synthetic vault\nLOCAL line\n")
        mgr.sync_now()
        wait_for(lambda: mgr.status()["sync"]["state"] == "blocked")
        self.assertEqual(mgr.status()["sync"]["error"], "blocked")
        self.assertTrue(list((vault / ".git").glob("rebase-*")))
        self.assertIn("<<<<<<<", (vault / "README.md").read_text())  # Git's own markers, left for a human
        self.assertEqual(git("--git-dir", str(self.bare), "rev-parse", "main").strip(), remote_head)  # no push, no force
        time.sleep(2.5)  # several more cycles: still blocked, nothing changed
        self.assertEqual(mgr.status()["sync"]["state"], "blocked")
        self.assertTrue(list((vault / ".git").glob("rebase-*")))
        self.assertEqual(git("--git-dir", str(self.bare), "rev-parse", "main").strip(), remote_head)
        with running_gateway() as client:
            cookie = client.send("POST", "/admin/login", form={"password": PASSWORD}).cookie()
            self.assertIn("BLOCKED", client.send("GET", "/admin/vault", cookie=cookie).text)
        mgr = onboarding.manager()  # stopping the temporary Gateway also stopped the loop; startup resumes it
        mgr.resume()
        # A human resolves it; sync resumes on its own.
        (vault / "README.md").write_text("# synthetic vault\nRESOLVED line\n")
        git("add", "README.md", cwd=vault)
        git("rebase", "--continue", cwd=vault)
        wait_for(lambda: mgr.status()["sync"]["state"] == "ok")
        self.assertIn("RESOLVED", git("--git-dir", str(self.bare), "show", "main:README.md"))

    def test_push_failure_is_visible_and_backs_off(self) -> None:
        self.point_remote_at(self.bare)
        mgr = self.connect()
        wait_for(lambda: mgr.status()["sync"]["last_ok"])
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'rejected: synthetic secret-looking text' >&2\nexit 1\n")
        hook.chmod(0o755)
        (self.data / "vault" / "new.md").write_text("cannot be pushed")
        mgr.sync_now()
        sync = wait_for(lambda: (s := mgr.status()["sync"])["state"] == "error" and s)
        self.assertEqual(sync["error"], "push-rejected")
        wait_for(lambda: mgr.status()["sync"]["failures"] >= 2)
        self.assertGreater(mgr.status()["sync"]["next_delay"], onboarding.sync_interval())
        with running_gateway() as client:
            cookie = client.send("POST", "/admin/login", form={"password": PASSWORD}).cookie()
            page = client.send("GET", "/admin/vault", cookie=cookie).text
            self.assertIn("The push was rejected", page)
            self.assertNotIn("secret-looking", page)  # Git output is never rendered
        mgr = onboarding.manager()
        mgr.resume()
        hook.unlink()
        mgr.sync_now()
        wait_for(lambda: mgr.status()["sync"]["state"] == "ok" and mgr.status()["sync"]["failures"] == 0)
        self.assertIn("new.md", git("--git-dir", str(self.bare), "ls-tree", "-r", "--name-only", "main"))

    def test_backoff_is_bounded(self) -> None:
        delays = [onboarding.sync_delay(300, n) for n in range(0, 40)]
        self.assertEqual(delays[0], 300)
        self.assertEqual(delays[1], 600)
        self.assertTrue(all(a <= b for a, b in zip(delays, delays[1:])))
        self.assertEqual(max(delays), onboarding.SYNC_BACKOFF_MAX_SECONDS)
        self.assertEqual(onboarding.sync_delay(7200, 5), 7200)  # never shorter than the configured interval

    def test_browser_flow_from_first_start_to_token_manager(self) -> None:
        """End to end over localhost: claim, connect, clone, readiness, tokens, API use, restart."""
        shutil.rmtree(self.data)  # start from a pristine volume, so the first claim is a real first start
        self.point_remote_at(self.bare)
        with running_gateway() as client:
            self.assertEqual(client.send("GET", "/").header("location"), "/setup")
            cookie = client.claim().cookie()
            self.assertEqual(client.send("GET", "/", cookie=cookie).header("location"), "/admin/vault")
            bad = client.post(cookie, "/admin/vault", {"repo_url": "https://example.com/x.git"})
            self.assertEqual(bad.status, 400)
            self.assertNotIn("example.com", bad.text)
            client.post(cookie, "/admin/vault", {"repo_url": URL})
            self.assertEqual(client.send("POST", "/admin/vault/connect", form={"csrf_token": "forged"}, cookie=cookie).status, 403)
            client.post(cookie, "/admin/vault/connect")
            wait_for(lambda: "Vault connected" in client.send("GET", "/admin/vault", cookie=cookie).text)
            self.assertEqual(client.send("GET", "/readyz").status, 200)
            tokens = client.send("GET", "/admin/tokens", cookie=cookie)
            self.assertEqual(tokens.status, 200)
            self.assertIn("Agent tokens", tokens.text)
            self.assertIn('href="/admin/vault"', tokens.text)  # navigation between the two pages
            issued = client.post(cookie, "/admin/tokens", {"agent_id": "codex-agent", "permission": "read-write"})
            token = re.search(r'id="issued-token" tabindex="0">(pvg_[A-Za-z0-9_-]+)<', issued.text).group(1)
            self.assertEqual(client.send("GET", "/gateway/v3/capabilities", token=token).json()["api_version"], "v3")
            note = {"kind": "note", "title": "synthetic note", "body": "keyword alpha-bravo"}
            self.assertEqual(client.send("POST", "/gateway/v3/capture", raw=note, token=token).status, 200)
            found = client.send("POST", "/gateway/v3/search", raw={"query": "synthetic vault"}, token=token)
            self.assertEqual(found.status, 200)
            self.assertTrue(any(item["path"] == "README.md" for item in found.json()["results"]))
            self.assertEqual(client.send("GET", "/gateway/v2/capabilities", token=token).status, 410)
        with running_gateway() as client:  # restart: ready immediately, sync resumes, session and token still valid
            self.assertEqual(client.send("GET", "/readyz").status, 200)
            self.assertTrue(onboarding.manager().status()["sync"]["running"])
            self.assertEqual(client.send("GET", "/admin/tokens", cookie=cookie).status, 200)
            self.assertEqual(client.send("GET", "/gateway/v3/capabilities", token=token).status, 200)
            self.assertEqual(client.stdout.getvalue(), "")
        sync_row = sqlite3.connect(self.data / "gateway.db").execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(sync_row, 1)  # SQLite schema untouched


if __name__ == "__main__":
    unittest.main()
