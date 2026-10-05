"""Admin UI, retry-safety, and validation tests against a temporary localhost Gateway."""

from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import http.client
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch
from urllib.parse import urlencode

import uvicorn
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway.app as app_module
from gateway.app import (
    ADMIN_COOKIE,
    agent_table_html,
    app,
    embedding_retry_loop,
    make_admin_cookie,
    sanitize_error,
    valid_admin_cookie,
)
import gateway.core as core
from gateway.core import Settings, generate_token, init_db, list_agents, lookup_agent, upsert_agent

PASSWORD = "pässwörd-비밀번호-\U0001f511"
SAME_ORIGIN = object()


class Reply:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.text = body.decode("utf-8", "replace")

    def header(self, name: str) -> str | None:
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)

    def set_cookies(self) -> list[str]:
        return [value for key, value in self.headers if key.lower() == "set-cookie"]

    def json(self) -> object:
        return json.loads(self.text)


class Session:
    def __init__(self, cookie: str, csrf: str) -> None:
        self.cookie = cookie
        self.csrf = csrf


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def send(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, str] | None = None,
        raw: bytes | None = None,
        headers: dict[str, str | bytes] | None = None,
        cookie: str | bytes | None = None,
        origin: object = SAME_ORIGIN,
    ) -> Reply:
        sent: dict[str, str | bytes] = dict(headers or {})
        body = raw
        if form is not None:
            body = urlencode(form).encode("ascii")
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie is not None:
            prefix = f"{ADMIN_COOKIE}=".encode() if isinstance(cookie, bytes) else f"{ADMIN_COOKIE}="
            sent["Cookie"] = prefix + cookie
        if method == "POST" and origin is SAME_ORIGIN:
            sent["Origin"] = f"http://127.0.0.1:{self.port}"
        elif isinstance(origin, str):
            sent["Origin"] = origin
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return Reply(response.status, response.getheaders(), response.read())
        finally:
            connection.close()

    def login(self, password: str = PASSWORD) -> Session:
        reply = self.send("POST", "/admin/login", form={"password": password})
        assert reply.status == 303, reply.text
        cookie = re.match(rf"{ADMIN_COOKIE}=([^;]+)", reply.set_cookies()[0]).group(1)
        page = self.send("GET", "/admin/tokens", cookie=cookie)
        return Session(cookie, re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1))

    def post(self, session: Session, path: str, form: dict[str, str], *, token: bool = True, **kwargs: object) -> Reply:
        values = {"csrf_token": session.csrf, **form} if token else form
        return self.send("POST", path, form=values, cookie=session.cookie, **kwargs)


@contextlib.contextmanager
def running_gateway(**config: object):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", **config))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(0.02)
            assert server.started, "temporary Gateway did not start"
            yield Client(listener.getsockname()[1])
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "temporary Gateway did not stop"


def issued_token(reply: Reply) -> str:
    match = re.search(r'<pre id="issued-token" tabindex="0">(pvg_[A-Za-z0-9_-]+)</pre>', reply.text)
    assert match, reply.text
    return match.group(1)


class FormParser(HTMLParser):
    """Collects each form's successful controls (hidden/text inputs, selects) as a browser submits them."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self.form: dict | None = None
        self.select: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "form":
            self.form = {"action": values.get("action", ""), "method": values.get("method", "get").lower(), "fields": []}
            self.forms.append(self.form)
        elif self.form is None:
            return
        elif tag == "input" and values.get("name") and values.get("type", "text") not in ("submit", "button", "checkbox", "radio", "file"):
            self.form["fields"].append([values["name"], values.get("value", "")])
        elif tag == "select" and values.get("name"):
            self.select = {"field": [values["name"], None], "first": None}
            self.form["fields"].append(self.select["field"])
        elif tag == "option" and self.select is not None:
            value = values.get("value", "")
            self.select["first"] = value if self.select["first"] is None else self.select["first"]
            if "selected" in values:
                self.select["field"][1] = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self.select is not None:
            if self.select["field"][1] is None:
                self.select["field"][1] = self.select["first"] or ""
            self.select = None
        elif tag == "form":
            self.form = None


class Page:
    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        parser = FormParser()
        parser.feed(reply.text)
        self.forms = parser.forms

    def form(self, action: str, **match: str) -> dict:
        found = [
            form for form in self.forms
            if form["action"] == action and all(dict(map(tuple, form["fields"])).get(key) == value for key, value in match.items())
        ]
        assert len(found) == 1, f"expected one form for {action} {match}, found {len(found)}"
        return found[0]


def browser_origin(page: Reply, port: int) -> str:
    # Fetch spec, "append a request Origin header": a no-referrer policy makes the Origin of a
    # non-GET request serialize as "null"; any other policy sends the real origin.
    policy = (page.header("referrer-policy") or "").strip().lower()
    return "null" if policy == "no-referrer" else f"http://127.0.0.1:{port}"


class Browser:
    """Drives the Gateway's own HTML like a browser: parse the page, submit its form, derive Origin from its policy."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.cookie: str | None = None
        self.pages: list[Page] = []

    def load(self, path: str) -> Page:
        page = Page(self.client.send("GET", path, cookie=self.cookie))
        self.pages.append(page)
        return page

    def submit(self, page: Page, action: str, match: dict[str, str] | None = None, **typed: str) -> Page:
        form = page.form(action, **(match or {}))
        assert form["method"] == "post"
        fields = {name: value for name, value in form["fields"]}
        fields.update(typed)
        reply = self.client.send(
            "POST", action, form=fields, cookie=self.cookie,
            origin=browser_origin(page.reply, self.client.port), headers={"Sec-Fetch-Site": "same-origin"},
        )
        for cookie in reply.set_cookies():
            self.cookie = re.match(rf"{ADMIN_COOKIE}=([^;]*)", cookie).group(1).strip('"') or None
        result = Page(reply)
        self.pages.append(result)
        return result

    def follow(self, page: Page) -> Page:
        assert page.reply.status == 303, page.reply.text
        return self.load(page.reply.header("location"))


class AdminHttpTests(unittest.TestCase):
    index_calls: list[int]
    index_behavior = staticmethod(lambda: {})

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = TemporaryDirectory(prefix="pvg-admin-")
        root = Path(cls.temporary.name)
        cls.db = root / "gateway.db"
        environment = {
            "VAULT_DIR": str(root / "vault"),
            "DB_PATH": str(cls.db),
            "EMBEDDING_PROVIDER": "hash",
            "HOST_ID": "admin-test",
            "ADMIN_PASSWORD": PASSWORD,
            "QDRANT_URL": "http://127.0.0.1:1",
            "CLOUDFLARE_API_TOKEN": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
        }
        cls.patches = [
            patch.dict(os.environ, environment),
            patch.object(app_module, "retry_due_embeddings", lambda settings: False),
            patch.object(app_module, "index_vault", cls.fake_index),
        ]
        for item in cls.patches:
            item.start()
        init_db(cls.db)
        cls.index_calls = []
        cls.index_behavior = staticmethod(lambda: {})
        cls.stack = contextlib.ExitStack()
        cls.client = cls.stack.enter_context(running_gateway(proxy_headers=False))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stack.close()
        for item in reversed(cls.patches):
            item.stop()
        cls.temporary.cleanup()

    @staticmethod
    def fake_index(settings: Settings) -> dict:
        AdminHttpTests.index_calls.append(1)
        return AdminHttpTests.index_behavior()

    def setUp(self) -> None:
        # Every test shares one client address; the limiter is process-local state they must not inherit.
        with app_module.admin_login_lock:
            app_module.admin_login_attempts.clear()

    def agent(self, agent_id: str) -> dict | None:
        # last_used_at changes on every authenticated probe, so it is not part of the identity.
        found = next((item for item in list_agents(self.db) if item["agent_id"] == agent_id), None)
        return {key: value for key, value in found.items() if key != "last_used_at"} if found else None

    def bearer(self, token: str, path: str = "/gateway/v3/capabilities") -> Reply:
        return self.client.send("GET", path, headers={"Authorization": f"Bearer {token}"})

    def seed_agent(self, agent_id: str, scopes: list[str] | None = None) -> str:
        token = generate_token()
        upsert_agent(self.db, agent_id, token, scopes or ["vault-rag"], [])
        return token

    # -- login and cookies -------------------------------------------------------------------

    def test_non_ascii_password_logs_in_and_sets_plain_http_cookie(self) -> None:
        reply = self.client.send("POST", "/admin/login", form={"password": PASSWORD})
        self.assertEqual(reply.status, 303)
        self.assertEqual(reply.header("location"), "/admin/tokens")
        cookie = reply.set_cookies()[0]
        self.assertTrue(cookie.startswith(f"{ADMIN_COOKIE}="))
        self.assertIn("HttpOnly", cookie)
        self.assertRegex(cookie, r"(?i)samesite=lax")
        self.assertIn("Max-Age=43200", cookie)
        self.assertNotIn("Secure", cookie)

    def test_wrong_or_malformed_passwords_show_inline_error(self) -> None:
        bad_forms = [
            {"form": {"password": PASSWORD + "x"}},
            {"form": {"password": "pässwörd-비밀번호-\U0001f5dd"}},
            {"form": {"password": ""}},
            {"raw": b"password=%ff%fe", "headers": {"Content-Type": "application/x-www-form-urlencoded"}},
            {"raw": b"password=\xff\xfe\xc3", "headers": {"Content-Type": "application/x-www-form-urlencoded"}},
        ]
        for kwargs in bad_forms:
            with self.subTest(kwargs=kwargs):
                reply = self.client.send("POST", "/admin/login", **kwargs)
                self.assertEqual(reply.status, 401)
                self.assertEqual(reply.set_cookies(), [])
                self.assertIn('role="alert"', reply.text)
                self.assertIn("Incorrect password", reply.text)
                self.assertIn('<label for="password">', reply.text)
                self.assertIn('aria-describedby="login-error"', reply.text)

    def test_oversized_login_form_is_rejected_without_server_error(self) -> None:
        reply = self.client.send("POST", "/admin/login", raw=b"password=" + b"a" * 70_000, headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(reply.status, 413)

    # -- login rate limiting and origin ------------------------------------------------------

    def asgi_login(self, password: str, client: tuple[str, int] | None = ("203.0.113.9", 4000), forwarded: str | None = None):
        """POST /admin/login straight to the ASGI handler, so request.client is exactly what the server set."""
        body = urlencode({"password": password}).encode()

        async def receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        headers = [(b"host", b"testserver"), (b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", str(len(body)).encode())]
        if forwarded is not None:
            headers.append((b"x-forwarded-for", forwarded.encode()))
        scope = {"type": "http", "method": "POST", "scheme": "http", "path": "/admin/login", "query_string": b"",
                 "server": ("testserver", 80), "client": client, "headers": headers}
        return asyncio.run(app_module.admin_login(Request(scope, receive)))

    def test_login_cross_origin_is_rejected_without_counting_or_cookie(self) -> None:
        for kwargs in ({"origin": "http://evil.example"}, {"origin": "null"}, {"origin": None, "headers": {"Sec-Fetch-Site": "cross-site"}}):
            with self.subTest(kwargs=kwargs):
                reply = self.client.send("POST", "/admin/login", form={"password": PASSWORD}, **kwargs)
                self.assertEqual(reply.status, 403)
                self.assertEqual(reply.set_cookies(), [])
                self.assertIn("another site", reply.text)
                self.assertNotIn(PASSWORD, reply.text)
        # Rejected requests do not burn the real administrator's attempts.
        self.assertEqual(app_module.admin_login_attempts, {})
        for _ in range(app_module.ADMIN_LOGIN_MAX_ATTEMPTS - 1):
            self.assertEqual(self.client.send("POST", "/admin/login", form={"password": "wrong"}).status, 401)
        self.assertEqual(self.client.send("POST", "/admin/login", form={"password": PASSWORD}, origin=None).status, 303)

    def test_repeated_failures_return_429_with_retry_after_until_window_expires(self) -> None:
        for _ in range(app_module.ADMIN_LOGIN_MAX_ATTEMPTS):
            self.assertEqual(self.client.send("POST", "/admin/login", form={"password": PASSWORD + "x"}).status, 401)
        for password in (PASSWORD + "x", PASSWORD):
            blocked = self.client.send("POST", "/admin/login", form={"password": password})
            self.assertEqual(blocked.status, 429)
            self.assertEqual(blocked.set_cookies(), [])
            self.assertTrue(1 <= int(blocked.header("Retry-After")) <= app_module.ADMIN_LOGIN_WINDOW_SECONDS)
            self.assertIn("Too many login attempts", blocked.text)
            self.assertNotIn(password, blocked.text)
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            messages = [row[0] for row in con.execute("SELECT error_message FROM audit_logs WHERE action = 'admin-login'")]
        self.assertTrue(all(PASSWORD not in (message or "") for message in messages))
        # The same window rule through the handler: age the stored attempts instead of waiting.
        with app_module.admin_login_lock:
            for stamps in app_module.admin_login_attempts.values():
                stamps[:] = [stamp - app_module.ADMIN_LOGIN_WINDOW_SECONDS - 1 for stamp in stamps]
        self.assertEqual(self.client.send("POST", "/admin/login", form={"password": PASSWORD}).status, 303)
        self.assertEqual(app_module.admin_login_attempts, {})

    def test_successful_login_resets_the_failure_count(self) -> None:
        limit = app_module.ADMIN_LOGIN_MAX_ATTEMPTS
        for _ in range(2):
            for _ in range(limit - 1):
                self.assertEqual(self.client.send("POST", "/admin/login", form={"password": "wrong"}).status, 401)
            self.assertEqual(self.client.send("POST", "/admin/login", form={"password": PASSWORD}).status, 303)

    def test_spoofed_forwarded_for_does_not_bypass_or_poison_the_limit(self) -> None:
        for index in range(app_module.ADMIN_LOGIN_MAX_ATTEMPTS):
            self.assertEqual(self.asgi_login("wrong", forwarded=f"198.51.100.{index}").status_code, 401)
        # A fresh spoofed address, even one that looks like another client, stays in the real client's bucket.
        for forwarded in ("198.51.100.200", "203.0.113.77, 198.51.100.1", "", "not-an-address"):
            blocked = self.asgi_login(PASSWORD, forwarded=forwarded)
            self.assertEqual(blocked.status_code, 429, forwarded)
            self.assertIn("retry-after", blocked.headers)
        # Spoofing cannot lock out somebody else: a different real client address is unaffected.
        self.assertEqual(self.asgi_login(PASSWORD, client=("203.0.113.10", 4000), forwarded="203.0.113.9").status_code, 303)
        self.assertEqual(set(app_module.admin_login_attempts), {"203.0.113.9"})
        # Over a real socket (this server does not trust proxy headers) the header is equally ignored.
        app_module.admin_login_attempts.clear()
        statuses = [
            self.client.send("POST", "/admin/login", form={"password": "wrong"}, headers={"X-Forwarded-For": f"198.51.100.{i}"}).status
            for i in range(7)
        ]
        self.assertEqual(statuses, [401] * app_module.ADMIN_LOGIN_MAX_ATTEMPTS + [429] * 2)
        self.assertEqual(self.asgi_login("wrong", client=None).status_code, 401)
        self.assertIn("unknown", app_module.admin_login_attempts)

    def test_trusted_proxy_hops_key_the_limiter_on_the_forwarded_client(self) -> None:
        proxy, limit = ("10.0.0.1", 4000), app_module.ADMIN_LOGIN_MAX_ATTEMPTS
        with patch.dict(os.environ, {"PVG_TRUSTED_PROXY_HOPS": "1"}):
            for _ in range(limit):
                self.assertEqual(self.asgi_login("wrong", client=proxy, forwarded="203.0.113.1").status_code, 401)
            # Whatever the client prepends, the entry the proxy appended picks the bucket: still blocked.
            for forwarded in ("203.0.113.1", "198.51.100.7, 203.0.113.1", "::1,198.51.100.8,203.0.113.1"):
                self.assertEqual(self.asgi_login(PASSWORD, client=proxy, forwarded=forwarded).status_code, 429, forwarded)
            # Another visitor behind the same proxy has its own bucket, and a prefix naming the first one does not poison it.
            self.assertEqual(self.asgi_login(PASSWORD, client=proxy, forwarded="203.0.113.1, 203.0.113.2").status_code, 303)
            self.assertEqual(set(app_module.admin_login_attempts), {"203.0.113.1"})
            # Missing, empty or non-IP entries fall back to the connection address, never to a client-chosen bucket.
            app_module.admin_login_attempts.clear()
            for forwarded in (None, "", "not-an-address", "203.0.113.1, not-an-address", "203.0.113.1,"):
                self.assertEqual(self.asgi_login("wrong", client=proxy, forwarded=forwarded).status_code, 401, forwarded)
            self.assertEqual(set(app_module.admin_login_attempts), {"10.0.0.1"})
            # IPv6 clients are keyed on their /64, so rotating inside one prefix keeps the same attempts.
            app_module.admin_login_attempts.clear()
            for forwarded in ("2001:DB8:0::1", "2001:db8::dead:beef", "2001:db8:0:0:ffff:ffff:ffff:ffff"):
                self.asgi_login("wrong", client=proxy, forwarded=forwarded)
            self.assertEqual({k: len(v) for k, v in app_module.admin_login_attempts.items()}, {"2001:db8::/64": 3})
            for _ in range(limit - 3):
                self.asgi_login("wrong", client=proxy, forwarded="2001:db8::7")
            self.assertEqual(self.asgi_login(PASSWORD, client=proxy, forwarded="2001:db8::8").status_code, 429)
            self.assertEqual(self.asgi_login(PASSWORD, client=proxy, forwarded="2001:db8:0:1::1").status_code, 303)
            # IPv4-mapped addresses are IPv4 clients, not one shared ::/64 bucket.
            app_module.admin_login_attempts.clear()
            self.asgi_login("wrong", client=proxy, forwarded="::ffff:203.0.113.5")
            self.assertEqual(set(app_module.admin_login_attempts), {"203.0.113.5"})
        with patch.dict(os.environ, {"PVG_TRUSTED_PROXY_HOPS": "2"}):
            app_module.admin_login_attempts.clear()
            self.asgi_login("wrong", client=proxy, forwarded="198.51.100.9, 203.0.113.1, 10.1.1.1")
            self.asgi_login("wrong", client=proxy, forwarded="203.0.113.1")  # fewer entries than hops
            self.assertEqual(set(app_module.admin_login_attempts), {"203.0.113.1", "10.0.0.1"})
        # Unset, 0 and unusable values keep the plain behaviour: the header is ignored.
        for hops in ("", "0", "-1", "one", "1.5", "١"):
            with self.subTest(hops=hops), patch.dict(os.environ, {"PVG_TRUSTED_PROXY_HOPS": hops}):
                app_module.admin_login_attempts.clear()
                self.asgi_login("wrong", client=proxy, forwarded="203.0.113.1")
                self.assertEqual(set(app_module.admin_login_attempts), {"10.0.0.1"})

    def test_login_window_expiry_and_sliding_boundaries(self) -> None:
        attempt, window, limit = app_module.admin_login_attempt, app_module.ADMIN_LOGIN_WINDOW_SECONDS, app_module.ADMIN_LOGIN_MAX_ATTEMPTS
        for index in range(limit):
            self.assertEqual(attempt("client", float(index)), 0)
        self.assertEqual(attempt("client", 10.0), window - 10)
        self.assertEqual(attempt("client", window - 0.5), 1)
        self.assertEqual(attempt("other", 10.0), 0)
        self.assertEqual(attempt("client", float(window)), 0)  # the oldest attempt has just expired
        self.assertEqual(attempt("client", window + 0.5), 1)  # but the next-oldest has not
        self.assertEqual(attempt("client", 10.0 * window), 0)
        self.assertEqual(len(app_module.admin_login_attempts["client"]), 1)
        app_module.admin_login_succeeded("client")
        self.assertNotIn("client", app_module.admin_login_attempts)

    def test_login_attempt_storage_is_bounded(self) -> None:
        attempt, tracked = app_module.admin_login_attempt, app_module.ADMIN_LOGIN_MAX_TRACKED
        window, limit = app_module.ADMIN_LOGIN_WINDOW_SECONDS, app_module.ADMIN_LOGIN_MAX_ATTEMPTS
        for index in range(3 * tracked):  # a flood of distinct live addresses evicts the least recent
            attempt(f"client-{index}", 1000.0 + index / 1000)
        self.assertEqual(len(app_module.admin_login_attempts), tracked)
        for _ in range(10_000):  # one hammering client never grows past the limit
            attempt("hammer", 1200.0)
        self.assertEqual(len(app_module.admin_login_attempts["hammer"]), limit)
        self.assertEqual(len(app_module.admin_login_attempts), tracked)
        attempt("fresh", 1000.0 + window + 100)  # when full, expired entries are purged first
        self.assertEqual(set(app_module.admin_login_attempts), {"hammer", "fresh"})

    def test_concurrent_guesses_cannot_exceed_the_limit(self) -> None:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: app_module.admin_login_attempt("racing", 5.0), range(64)))
        self.assertEqual(results.count(0), app_module.ADMIN_LOGIN_MAX_ATTEMPTS)

    def test_malformed_cookies_are_rejected_not_500(self) -> None:
        valid = make_admin_cookie(PASSWORD)
        timestamp, signature = valid.split(".")
        cookies: list[str | bytes] = [
            "", "abc", ".", "123.", ".abc", "-5.sig", "1e3.abc", f"{timestamp}",
            f"{timestamp}.{signature[:-1]}", f"{timestamp}.{signature}0", f"{timestamp}.{signature.upper()}",
            "9" * 400 + ".abc", "0" * 20 + "." + signature,
            make_admin_cookie(PASSWORD, now=1),
            make_admin_cookie(PASSWORD, now=int(time.time()) + 3600),
            make_admin_cookie("another-password"),
            "٣.abc".encode("utf-8"), "².00".encode("utf-8"),
            f"{timestamp}.éé".encode("utf-8"), b"\xff\xfe.\x80",
        ]
        for cookie in cookies:
            with self.subTest(cookie=cookie):
                reply = self.client.send("GET", "/admin/tokens", cookie=cookie)
                self.assertEqual(reply.status, 303)
                self.assertEqual(reply.header("location"), "/admin/login")
                reply = self.client.send("POST", "/admin/tokens", form={"agent_id": "x", "permission": "read"}, cookie=cookie)
                self.assertEqual(reply.status, 303)
        for cookie in ("٣.abc", "².00", f"{timestamp}.é", "\ud800.\ud800", "١" * 5 + "." + signature):
            self.assertFalse(valid_admin_cookie(cookie, PASSWORD))
        self.assertTrue(valid_admin_cookie(valid, PASSWORD))

    def test_secure_cookie_only_for_trusted_https(self) -> None:
        def request(scheme: str) -> Request:
            return Request({"type": "http", "scheme": scheme, "server": ("example.test", 443), "headers": [], "method": "POST", "path": "/", "query_string": b""})

        self.assertTrue(app_module.admin_cookie_attributes(request("https"))["secure"])
        self.assertFalse(app_module.admin_cookie_attributes(request("http"))["secure"])

        forwarded = {"X-Forwarded-Proto": "https"}
        untrusted = self.client.send("POST", "/admin/login", form={"password": PASSWORD}, headers=forwarded)
        self.assertNotIn("Secure", untrusted.set_cookies()[0])
        with running_gateway(proxy_headers=True, forwarded_allow_ips="127.0.0.1") as trusted_proxy:
            reply = trusted_proxy.send("POST", "/admin/login", form={"password": PASSWORD}, headers=forwarded)
            self.assertIn("Secure", reply.set_cookies()[0])
            plain = trusted_proxy.send("POST", "/admin/login", form={"password": PASSWORD})
            self.assertNotIn("Secure", plain.set_cookies()[0])

    # -- CSRF and origin ---------------------------------------------------------------------

    def test_mutations_require_session_bound_csrf_token(self) -> None:
        session = self.client.login()
        other = Session(make_admin_cookie(PASSWORD, now=int(time.time()) - 30), session.csrf)
        self.assertNotEqual(self.client.send("GET", "/admin/tokens", cookie=other.cookie).status, 500)
        self.seed_agent("csrf-target")
        before = self.agent("csrf-target")
        calls = len(self.index_calls)
        form = {"agent_id": "csrf-target", "permission": "read", "confirm": "yes"}
        attempts = [
            ("missing token", session, {}, False),
            ("wrong token", session, {"csrf_token": "0" * 64}, False),
            ("empty token", session, {"csrf_token": ""}, False),
            ("token from another session", other, {"csrf_token": session.csrf}, False),
        ]
        for label, who, extra, use_token in attempts:
            for path in ("/admin/tokens", "/admin/tokens/rotate", "/admin/tokens/disable", "/admin/rag/rebuild", "/admin/logout"):
                with self.subTest(label=label, path=path):
                    reply = self.client.send("POST", path, form={**form, **extra}, cookie=who.cookie)
                    self.assertEqual(reply.status, 403)
                    self.assertIn("Request blocked", reply.text)
                    self.assertEqual(reply.set_cookies(), [])
        self.assertEqual(self.agent("csrf-target"), before)
        self.assertEqual(len(self.index_calls), calls)
        self.assertIsNone(self.agent("csrf-new"))

    def test_cross_origin_requests_are_rejected_even_with_valid_token(self) -> None:
        session = self.client.login()
        token = self.seed_agent("origin-target")
        before = self.agent("origin-target")
        calls = len(self.index_calls)
        scenarios = [
            {"origin": "http://evil.example"},
            {"origin": f"http://127.0.0.1.evil.example:{self.client.port}"},
            {"origin": "null"},
            {"origin": "not a url ["},
            {"origin": None, "headers": {"Sec-Fetch-Site": "cross-site"}},
            {"origin": None, "headers": {"Sec-Fetch-Site": "same-site"}},
        ]
        for scenario in scenarios:
            for path in ("/admin/tokens", "/admin/tokens/rotate", "/admin/tokens/disable", "/admin/rag/rebuild", "/admin/logout"):
                with self.subTest(scenario=scenario, path=path):
                    reply = self.client.post(session, path, {"agent_id": "origin-target", "permission": "read", "confirm": "yes"}, **scenario)
                    self.assertEqual(reply.status, 403)
                    self.assertEqual(reply.set_cookies(), [])
        self.assertEqual(self.agent("origin-target"), before)
        self.assertEqual(self.bearer(token).status, 200)
        self.assertEqual(len(self.index_calls), calls)

        no_origin = self.client.post(session, "/admin/tokens", {"agent_id": "origin-ok", "permission": "read"}, origin=None)
        self.assertEqual(no_origin.status, 200, "non-browser clients without Origin still work with the token")
        fetch_metadata = self.client.post(session, "/admin/tokens", {"agent_id": "origin-ok2", "permission": "read"}, origin=None, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(fetch_metadata.status, 200)

    def test_unauthenticated_posts_redirect_to_login(self) -> None:
        for path in ("/admin/tokens", "/admin/tokens/rotate", "/admin/tokens/disable", "/admin/rag/rebuild"):
            reply = self.client.send("POST", path, form={"agent_id": "x"})
            self.assertEqual((reply.status, reply.header("location")), (303, "/admin/login"))

    def test_forms_work_with_the_origin_a_real_browser_sends(self) -> None:
        """Regression for 403s on a real click: every form is submitted from the page that served it.

        The Origin header is derived from that page's Referrer-Policy, so a policy that makes browsers
        send "Origin: null" (no-referrer) fails here instead of being masked by a hand-written header.
        """
        type(self).index_behavior = staticmethod(lambda: {"files": 1, "chunks": 2})
        browser = Browser(self.client)
        login = browser.submit(browser.load("/admin/login"), "/admin/login", password=PASSWORD)
        self.assertEqual(login.reply.status, 303)
        browser.follow(login)
        tokens = browser.load("/admin/tokens")
        created = browser.submit(tokens, "/admin/tokens", agent_id="browser-flow")
        self.assertEqual(created.reply.status, 200, created.reply.text)
        first_token = issued_token(created.reply)
        self.assertEqual(self.agent("browser-flow")["scopes"], ["conversation-log", "agent-memo", "vault-rag"])

        # Same ID again goes through the confirmation page, whose own form then completes the rotation.
        prompt = browser.submit(tokens, "/admin/tokens", agent_id="browser-flow", permission="write")
        self.assertIn("Replace existing token?", prompt.reply.text)
        self.assertEqual(self.bearer(first_token).status, 200)
        replaced = browser.submit(prompt, "/admin/tokens")
        self.assertEqual(replaced.reply.status, 200, replaced.reply.text)
        second_token = issued_token(replaced.reply)
        self.assertEqual(self.bearer(first_token).status, 401)
        self.assertEqual(self.agent("browser-flow")["scopes"], ["conversation-log", "agent-memo"])

        tokens = browser.load("/admin/tokens")
        rotated = browser.submit(tokens, "/admin/tokens/rotate", {"agent_id": "browser-flow"})
        self.assertEqual(rotated.reply.status, 200, rotated.reply.text)
        third_token = issued_token(rotated.reply)
        self.assertEqual(self.bearer(second_token).status, 401)

        rebuilt = browser.submit(tokens, "/admin/rag/rebuild")
        self.assertEqual(rebuilt.reply.status, 200, rebuilt.reply.text)
        self.assertIn("RAG index updated", rebuilt.reply.text)

        disabled = browser.submit(tokens, "/admin/tokens/disable", {"agent_id": "browser-flow"})
        self.assertEqual(disabled.reply.status, 303, disabled.reply.text)
        self.assertIn("Agent token disabled", browser.follow(disabled).reply.text)
        self.assertEqual(self.bearer(third_token).status, 401)

        # The logout form lives in the header of every authenticated page, including one-time token pages.
        again = browser.submit(browser.load("/admin/tokens"), "/admin/tokens", agent_id="browser-flow-2")
        logout = browser.submit(again, "/admin/logout")
        self.assertEqual((logout.reply.status, logout.reply.header("location")), (303, "/admin/login?notice=logged-out"))
        self.assertIsNone(browser.cookie)

        html_pages = [page for page in browser.pages if (page.reply.header("content-type") or "").startswith("text/html")]
        self.assertGreaterEqual(len(html_pages), 8)
        for page in html_pages:
            self.assertEqual(page.reply.header("referrer-policy"), "same-origin")
            self.assertNotIn('name="referrer"', page.reply.text)

    def test_browser_model_catches_a_policy_that_sends_null_origin(self) -> None:
        browser = Browser(self.client)
        browser.submit(browser.load("/admin/login"), "/admin/login", password=PASSWORD)
        tokens = browser.load("/admin/tokens")
        self.assertEqual(browser_origin(tokens.reply, self.client.port), f"http://127.0.0.1:{self.client.port}")
        regressed = Page(Reply(200, [("Referrer-Policy", "no-referrer")], tokens.reply.body))
        self.assertEqual(browser_origin(regressed.reply, self.client.port), "null")
        blocked = browser.submit(regressed, "/admin/tokens", agent_id="null-origin")
        self.assertEqual(blocked.reply.status, 403)
        self.assertIsNone(self.agent("null-origin"))

    # -- explicit confirmation ---------------------------------------------------------------

    def test_create_with_existing_id_does_not_rotate_until_confirmed(self) -> None:
        session = self.client.login()
        first = self.client.post(session, "/admin/tokens", {"agent_id": "confirm-create", "permission": "read"})
        self.assertEqual(first.status, 200)
        old_token = issued_token(first)
        before = self.agent("confirm-create")
        self.assertEqual(before["scopes"], ["vault-rag"])

        attempt = self.client.post(session, "/admin/tokens", {"agent_id": "confirm-create", "permission": "read-write"})
        self.assertEqual(attempt.status, 200)
        self.assertIn("Replace existing token?", attempt.text)
        self.assertIn('name="confirm" value="yes"', attempt.text)
        self.assertIn('name="csrf_token"', attempt.text)
        self.assertNotIn("issued-token", attempt.text)
        self.assertNotIn(old_token, attempt.text)
        self.assertEqual(self.agent("confirm-create"), before)
        self.assertEqual(self.bearer(old_token).status, 200)

        for bogus in ("", "no", "YES", "true"):
            reply = self.client.post(session, "/admin/tokens", {"agent_id": "confirm-create", "permission": "read-write", "confirm": bogus})
            self.assertIn("Replace existing token?", reply.text)
        self.assertEqual(self.agent("confirm-create"), before)

        confirmed = self.client.post(session, "/admin/tokens", {"agent_id": "confirm-create", "permission": "read-write", "confirm": "yes"})
        new_token = issued_token(confirmed)
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(self.bearer(old_token).status, 401)
        self.assertEqual(self.bearer(new_token).status, 200)
        self.assertEqual(self.agent("confirm-create")["scopes"], ["conversation-log", "agent-memo", "vault-rag"])

    def test_create_validation_errors_do_not_issue_tokens(self) -> None:
        session = self.client.login()
        for form in ({"agent_id": "Bad ID", "permission": "read"}, {"agent_id": "ok-id", "permission": "admin"}, {"agent_id": "", "permission": "read"}):
            with self.subTest(form=form):
                reply = self.client.post(session, "/admin/tokens", form)
                self.assertEqual(reply.status, 400)
                self.assertNotIn("issued-token", reply.text)
        self.assertIsNone(self.agent("ok-id"))
        self.assertIsNone(self.agent("bad id"))

    def test_rotate_requires_confirmation_then_replaces_token(self) -> None:
        session = self.client.login()
        old_token = self.seed_agent("rotate-me", ["vault-rag"])
        before = self.agent("rotate-me")

        prompt = self.client.post(session, "/admin/tokens/rotate", {"agent_id": "rotate-me"})
        self.assertEqual(prompt.status, 200)
        self.assertIn("Rotate this token?", prompt.text)
        self.assertNotIn("issued-token", prompt.text)
        self.assertEqual(self.agent("rotate-me"), before)
        self.assertEqual(self.bearer(old_token).status, 200)

        rotated = self.client.post(session, "/admin/tokens/rotate", {"agent_id": "rotate-me", "confirm": "yes"})
        self.assertEqual(rotated.status, 200)
        self.assertIn("Agent token rotated", rotated.text)
        new_token = issued_token(rotated)
        self.assertEqual(self.bearer(old_token).status, 401)
        self.assertEqual(self.bearer(new_token).status, 200)
        self.assertEqual(self.agent("rotate-me")["scopes"], ["vault-rag"])

        missing = self.client.post(session, "/admin/tokens/rotate", {"agent_id": "nobody", "confirm": "yes"})
        self.assertEqual(missing.status, 404)

    def test_disable_requires_confirmation_then_disables(self) -> None:
        session = self.client.login()
        token = self.seed_agent("disable-me")

        prompt = self.client.post(session, "/admin/tokens/disable", {"agent_id": "disable-me"})
        self.assertEqual(prompt.status, 200)
        self.assertIn("Disable this token?", prompt.text)
        self.assertTrue(self.agent("disable-me")["enabled"])
        self.assertEqual(self.bearer(token).status, 200)

        done = self.client.post(session, "/admin/tokens/disable", {"agent_id": "disable-me", "confirm": "yes"})
        self.assertEqual((done.status, done.header("location")), (303, "/admin/tokens?notice=disabled"))
        self.assertFalse(self.agent("disable-me")["enabled"])
        self.assertEqual(self.bearer(token).status, 401)
        page = self.client.send("GET", done.header("location"), cookie=session.cookie)
        self.assertIn("Agent token disabled", page.text)
        self.assertIn("disabled", page.text)

        self.assertEqual(self.client.post(session, "/admin/tokens/disable", {"agent_id": "nobody", "confirm": "yes"}).status, 404)
        reflected = self.client.send("GET", "/admin/tokens?notice=<script>alert(1)</script>", cookie=session.cookie)
        self.assertNotIn("alert(1)", reflected.text)

    def test_logout_clears_cookie_with_valid_token(self) -> None:
        session = self.client.login()
        reply = self.client.post(session, "/admin/logout", {})
        self.assertEqual((reply.status, reply.header("location")), (303, "/admin/login?notice=logged-out"))
        cleared = reply.set_cookies()[0]
        self.assertTrue(cleared.startswith(f'{ADMIN_COOKIE}="";') or cleared.startswith(f"{ADMIN_COOKIE}=;"), cleared)
        self.assertRegex(cleared, r"(?i)max-age=0")
        page = self.client.send("GET", reply.header("location"))
        self.assertIn("You have been logged out.", page.text)
        # Unauthenticated logout is harmless and needs no token.
        self.assertEqual(self.client.send("POST", "/admin/logout").status, 303)

    # -- secrets and rendering ---------------------------------------------------------------

    def test_token_is_shown_once_and_never_stored_in_urls_or_audit(self) -> None:
        session = self.client.login()
        reply = self.client.post(session, "/admin/tokens", {"agent_id": "one-time", "permission": "read-write"})
        self.assertEqual(reply.status, 200)
        token = issued_token(reply)
        self.assertEqual(reply.text.count(token), 1)
        self.assertIn("shown only once", reply.text)
        self.assertEqual(reply.header("cache-control"), "no-store")
        # same-origin: the browser still sends its real Origin to this site; no other site gets a Referer.
        self.assertEqual(reply.header("referrer-policy"), "same-origin")
        self.assertIsNone(reply.header("location"))
        self.assertIn('<a href="/admin/tokens">Back to tokens</a>', reply.text)
        self.assertNotIn("/admin/login", reply.text)
        self.assertIn('id="copy-token" hidden', reply.text)
        self.assertIn("press Ctrl/Cmd+C", reply.text)

        listing = self.client.send("GET", "/admin/tokens", cookie=session.cookie)
        self.assertNotIn(token, listing.text)
        self.assertIn(f"{token[:12]}...", listing.text)
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            dump = json.dumps([tuple(row) for row in con.execute("SELECT * FROM audit_logs")])
        self.assertNotIn(token, dump)
        self.assertIn("admin-token", dump)

    def test_login_and_token_pages_have_headings_labels_and_state(self) -> None:
        login = self.client.send("GET", "/admin/login")
        self.assertEqual(login.status, 200)
        for fragment in ('<html lang="en">', "<h1>PersonaVault Admin</h1>", '<label for="password">', 'id="password"', 'autocomplete="current-password"', 'class="skip"', "<main"):
            self.assertIn(fragment, login.text)

        session = self.client.login()
        self.seed_agent("listed-agent", ["conversation-log", "agent-memo", "vault-rag"])
        page = self.client.send("GET", "/admin/tokens", cookie=session.cookie)
        self.assertEqual(page.status, 200)
        for fragment in (
            "<h1>Agent tokens</h1>", "<h2 id=\"issue-heading\">Issue a token</h2>", "<h2 id=\"agents-heading\">Agents</h2>", "<h2 id=\"rag-heading\">RAG index</h2>",
            '<label for="agent_id">Agent ID</label>', 'id="agent_id"', '<label for="permission">Permission</label>', '<select name="permission" id="permission"',
            '<option value="read">Read</option>', '<option value="write">Write</option>', '<option value="read-write" selected>Read + Write</option>',
            "<code>conversation-log, agent-memo, vault-rag</code>", "Last token use", "does not mean a capture completed",
            'data-label="Scopes"',
            'action="/admin/tokens/rotate"', 'action="/admin/tokens/disable"', "Confirm rotate", "Confirm disable",
            'action="/admin/logout"', 'action="/admin/rag/rebuild"', "waits for it to finish",
        ):
            self.assertIn(fragment, page.text)
        # Behavior, not appearance: stacked rows need a label on every cell, and CSS must not hide focus.
        table = re.search(r"<tbody.*?</tbody>", page.text, re.S).group(0)
        self.assertEqual(table.count("<td"), table.count('<td role="cell" data-label='))
        self.assertNotRegex(re.search(r"<style>.*?</style>", page.text, re.S).group(0), r"outline\s*:\s*(none|0)\b")
        self.assertIn('<th role="columnheader" scope="col">', page.text)
        self.assertEqual(len(set(re.findall(r'name="csrf_token" value="([0-9a-f]+)"', page.text))), 1)
        self.assertNotIn("<script", page.text)
        self.assertNotIn("http://fonts", page.text.lower())
        self.assertNotIn("https://", re.sub(r"<a [^>]*>", "", page.text))

    def test_empty_agent_list_has_empty_state(self) -> None:
        self.assertIn("No agent tokens yet", agent_table_html([]))
        self.assertNotIn("<table", agent_table_html([]))

    # -- index update ------------------------------------------------------------------------

    def test_rag_rebuild_shows_labeled_counts(self) -> None:
        session = self.client.login()
        type(self).index_behavior = staticmethod(lambda: {"files": 7, "chunks": 31, "updated": 3, "payload_updated": 2, "deleted": 1, "model": "hash-test"})
        reply = self.client.post(session, "/admin/rag/rebuild", {})
        self.assertEqual(reply.status, 200)
        for label, value in (("Markdown files scanned", "7"), ("Chunks in index", "31"), ("Chunks newly embedded", "3"), ("Chunks with metadata-only update", "2"), ("Stale chunks deleted", "1"), ("Embedding model", "hash-test")):
            self.assertIn(f"<dt>{label}</dt><dd>{value}</dd>", reply.text)
        self.assertNotIn("progress", reply.text.lower())
        self.assertIn('<a href="/admin/tokens">Back to tokens</a>', reply.text)

    def test_keyword_only_mode_explains_index_state_and_never_contacts_semantic_services(self) -> None:
        session = self.client.login()
        calls: list[str] = []

        def blocked(name: str):
            def fail(*args: object, **kwargs: object) -> None:
                calls.append(name)
                raise AssertionError(f"{name} must not run when EMBEDDING_PROVIDER=none")

            return fail

        poisoned = [
            patch.object(owner, name, blocked(name))
            for owner, name in (
                (core, "qdrant_json"), (core, "cloudflare_embedding_request"), (core, "cloudflare_embeddings"),
                (core, "embed_documents"), (core, "embed_query"), (urllib.request, "urlopen"),
            )
        ]
        indexed_before = len(self.index_calls)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"EMBEDDING_PROVIDER": "none"}))
            stack.enter_context(patch.object(app_module, "index_vault", core.index_vault))
            for item in poisoned:
                stack.enter_context(item)
            page = self.client.send("GET", "/admin/tokens", cookie=session.cookie)
            self.assertEqual(page.status, 200)
            self.assertIn('<h2 id="rag-heading">RAG index</h2>', page.text)
            self.assertIn("Semantic search is disabled", page.text)
            self.assertIn("EMBEDDING_PROVIDER=none", page.text)
            self.assertNotIn("/admin/rag/rebuild", page.text)
            self.assertNotIn("Update RAG index", page.text)
            reply = self.client.post(session, "/admin/rag/rebuild", {})
            self.assertEqual(reply.status, 200)
            self.assertIn("<h1>Semantic search is disabled</h1>", reply.text)
            self.assertIn("Nothing was updated", reply.text)
            self.assertNotIn("RAG index updated", reply.text)
            self.assertNotIn("<dt>", reply.text)
            ready = self.client.send("GET", "/readyz")
            self.assertEqual(ready.status, 200)
            self.assertEqual(
                ready.json(),
                {"status": "ok", "db": "ok", "qdrant": "disabled", "semantic": "disabled", "rag_indexed": False, "chunks": 0, "provider": "none"},
            )
        self.assertEqual(calls, [])
        self.assertEqual(len(self.index_calls), indexed_before)
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            statuses = [row[0] for row in con.execute("SELECT status FROM audit_logs WHERE action = 'admin-rag-rebuild' ORDER BY id DESC LIMIT 1")]
        self.assertEqual(statuses, ["disabled"])
        enabled = self.client.send("GET", "/admin/tokens", cookie=session.cookie)
        self.assertIn('action="/admin/rag/rebuild"', enabled.text)
        self.assertNotIn("Semantic search is disabled", enabled.text)

    def test_rag_rebuild_failure_is_sanitized_with_next_action(self) -> None:
        session = self.client.login()
        secret = "Qdrant failed at http://user:hunter2@qdrant:6333 token=abc123 Bearer zzz999 pvg_abcdefghijklmnop " + "x" * 5000

        def failing() -> dict:
            raise RuntimeError(secret)

        type(self).index_behavior = staticmethod(failing)
        with self.assertLogs("gateway.app", logging.ERROR) as logs:
            reply = self.client.post(session, "/admin/rag/rebuild", {})
        self.assertEqual(reply.status, 503)
        self.assertIn("RAG index update failed", reply.text)
        self.assertIn("Check that Qdrant", reply.text)
        for leaked in ("hunter2", "abc123", "zzz999", "pvg_abcdefghijklmnop", "x" * 300):
            self.assertNotIn(leaked, reply.text)
            self.assertNotIn(leaked, "\n".join(logs.output))
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            messages = [row[0] for row in con.execute("SELECT error_message FROM audit_logs WHERE action = 'admin-rag-rebuild' AND status = 'error'")]
        self.assertTrue(messages)
        self.assertTrue(all("hunter2" not in (message or "") and len(message or "") <= 200 for message in messages))

    def test_sanitize_error_removes_configured_secrets_and_bounds_text(self) -> None:
        settings = Settings(vault_dir=Path("v"), db_path=Path("d.db"), host_id="h", cloudflare_api_token="cf-literal-secret", qdrant_api_key="qd-literal-secret")
        text = sanitize_error(RuntimeError("bad cf-literal-secret and qd-literal-secret\nline\x00two " + PASSWORD), settings)
        for leaked in ("cf-literal-secret", "qd-literal-secret", PASSWORD, "\n", "\x00"):
            self.assertNotIn(leaked, text)
        self.assertEqual(sanitize_error(ValueError("")), "ValueError")
        self.assertLessEqual(len(sanitize_error(ValueError("y" * 10_000))), 200)

    # -- API validation and contracts --------------------------------------------------------

    def test_surrogate_validation_failures_become_bounded_422(self) -> None:
        token = self.seed_agent("api-agent", ["vault-rag", "conversation-log", "agent-memo"])
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        bodies = [
            (b'{"query":"\\ud800"}', ""),
            (b'{"query":"ok\\ud800LEAKMARK","limit":99}', "LEAKMARK"),
            (b'{"query":"\\udfffLEAKMARK","view":"\\ud800LEAKMARK"}', "LEAKMARK"),
            (b'{"query":"q","context":{"\\ud800LEAKMARK":1},"limit":0}', "LEAKMARK"),
        ]
        for body, marker in bodies:
            with self.subTest(body=body):
                reply = self.client.send("POST", "/gateway/v3/search", raw=body, headers=headers)
                self.assertEqual(reply.status, 422)
                self.assertIn("application/json", reply.header("content-type"))
                detail = reply.json()["detail"]
                self.assertTrue(detail and all(set(item) == {"type", "loc", "msg"} for item in detail))
                self.assertLess(len(reply.body), 4_000)
                self.assertNotIn("ud800", reply.text.lower())
                if marker:
                    self.assertNotIn(marker, reply.text)
        note = self.client.send("POST", "/gateway/v3/capture", raw=b'{"kind":"note","title":"t\\ud800LEAKMARK","body":"b"}', headers=headers)
        self.assertEqual(note.status, 422)
        self.assertNotIn("LEAKMARK", note.text)
        junk = self.client.send("POST", "/gateway/v3/search", raw=b"\xff\xfe\xc3(", headers=headers)
        self.assertEqual(junk.status, 422)

    def test_validation_rules_still_reject_and_name_the_field(self) -> None:
        token = self.seed_agent("api-agent2", ["vault-rag"])
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        reply = self.client.send("POST", "/gateway/v3/search", raw=b'{"query":"hello","limit":99}', headers=headers)
        self.assertEqual(reply.status, 422)
        detail = reply.json()["detail"]
        self.assertEqual(detail[0]["loc"], ["body", "limit"])
        self.assertEqual(detail[0]["type"], "less_than_equal")
        self.assertNotIn("99", json.dumps(detail[0]["msg"]).replace("20", ""))
        self.assertEqual(self.client.send("POST", "/gateway/v3/search", raw=b'{"query":"hello"}').status, 401)

    def test_gateway_v3_contract_and_upgrade_required_are_unchanged(self) -> None:
        token = self.seed_agent("contract-agent", ["vault-rag"])
        capabilities = self.bearer(token)
        self.assertEqual(capabilities.status, 200)
        self.assertEqual(capabilities.json()["api_version"], "v3")
        self.assertIn("conversation-merge-v1", capabilities.json()["features"])
        retired = self.bearer(token, "/gateway/v1/capabilities")
        self.assertEqual(retired.status, 410)
        detail = retired.json()["detail"]
        self.assertEqual((detail["code"], detail["reason"], detail["requested_api_version"]), ("client_upgrade_required", "api_version_retired", "v1"))
        self.assertTrue(detail["retryable_after_update"] and detail["preserve_request_body"])
        self.assertIn("successor-version", retired.header("link"))
        self.assertEqual(self.client.send("GET", "/gateway/v3/capabilities").status, 401)
        self.assertIsNotNone(lookup_agent(self.db, token))


class RetryLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(vault_dir=Path("vault"), db_path=Path("gateway.db"), host_id="retry-test")
        self.stop = threading.Event()
        self.patches = [
            patch.object(app_module, "embedding_retry_stop", self.stop),
            patch.object(app_module, "EMBEDDING_RETRY_POLL_SECONDS", 0.001),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def test_loop_survives_transient_exceptions_and_logs_sanitized_errors(self) -> None:
        calls: list[int] = []
        finished = threading.Event()
        failures = [RuntimeError("boom pvg_secrettokenvalue123 token=abc"), ValueError("bad"), OSError("disk"), KeyError("k")]

        def flaky(settings: Settings) -> bool:
            calls.append(1)
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
            finished.set()
            return False

        crashed: list[BaseException] = []

        def run() -> None:
            try:
                embedding_retry_loop(self.settings)
            except BaseException as exc:  # pragma: no cover - only reached on regression
                crashed.append(exc)

        with patch.object(app_module, "retry_due_embeddings", flaky), self.assertLogs("gateway.app", logging.ERROR) as logs:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            self.assertTrue(finished.wait(10), "retry loop died after a transient exception")
            self.assertTrue(thread.is_alive())
            self.stop.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(crashed, [])
        output = "\n".join(logs.output)
        self.assertEqual(len(logs.records), len(failures))
        self.assertIn("RuntimeError", output)
        self.assertNotIn("secrettokenvalue123", output)
        self.assertNotIn("abc", output)

    def test_loop_does_not_swallow_shutdown_exceptions(self) -> None:
        for exception in (KeyboardInterrupt(), SystemExit(1)):
            with self.subTest(exception=type(exception).__name__):
                def raising(settings: Settings, exception: BaseException = exception) -> bool:
                    raise exception

                with patch.object(app_module, "retry_due_embeddings", raising):
                    with self.assertRaises(type(exception)):
                        embedding_retry_loop(self.settings)

    def test_loop_exits_promptly_when_stopped(self) -> None:
        self.stop.set()
        with patch.object(app_module, "retry_due_embeddings", side_effect=AssertionError("must not run")):
            embedding_retry_loop(self.settings)


if __name__ == "__main__":
    unittest.main()
