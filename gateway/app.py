from __future__ import annotations

import hmac
import html
import logging
import math
import os
import re
import time
from hashlib import sha256
from threading import Event, Lock, Thread
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import onboarding
from .core import (
    AGENT_ID_RE,
    ConversationConflictError,
    Settings,
    audit,
    disable_agent,
    generate_token,
    index_vault,
    init_db,
    list_agents,
    load_working_agreement,
    lookup_agent,
    rag_readiness,
    retry_due_embeddings,
    save_agent_note,
    save_conversation,
    search_vault,
    upsert_agent,
    wiki_health,
)


app = FastAPI(title="PersonaVault Gateway")
logger = logging.getLogger(__name__)
ADMIN_COOKIE = "pvg_admin"
ADMIN_SESSION_TTL_SECONDS = 12 * 60 * 60
DEFAULT_AGENT_ID = "codex-agent"
TOKEN_PERMISSION_SCOPES = {
    "read": ("vault-rag",),
    "write": ("conversation-log", "agent-memo"),
    "read-write": ("conversation-log", "agent-memo", "vault-rag"),
}
API_VERSION = "v3"
RETIRED_API_VERSIONS = {"v1", "v2"}
PLUGIN_NAME = "persona-vault"
PLUGIN_MIN_VERSION = "0.7.0"
PLUGIN_MARKETPLACE = "mykim0409/persona-vault-gateway"
PLUGIN_URL = "https://github.com/mykim0409/persona-vault-gateway"
EMBEDDING_RETRY_POLL_SECONDS = 60
embedding_retry_stop = Event()
embedding_retry_thread: Thread | None = None
CSRF_FIELD = "csrf_token"
ADMIN_FORM_MAX_BYTES = 16 * 1024
SETUP_FORM_MAX_BYTES = 4 * 1024
ADMIN_LOGIN_MAX_ATTEMPTS = 5
ADMIN_LOGIN_WINDOW_SECONDS = 5 * 60
ADMIN_LOGIN_MAX_TRACKED = 1024
# ponytail: process-local state is enough for the single Gateway process. Counters reset on restart and are
# not shared between workers or replicas; a database-backed limiter is only needed if that ever changes.
admin_login_attempts: dict[str, list[float]] = {}
admin_login_lock = Lock()
ADMIN_NOTICES = {
    "disabled": "Agent token disabled. Requests using it are now rejected.",
    "logged-out": "You have been logged out.",
    "vault-saved": "Repository saved and deploy key ready. Register the key, then connect.",
    "vault-connecting": "Connecting. The repository is being cloned in the background.",
    "vault-syncing": "Sync requested. The status below updates when it finishes.",
}
ERROR_TEXT_LIMIT = 200
VALIDATION_ERROR_LIMIT = 20
VALIDATION_LOC_LIMIT = 8


class CaptureMessage(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=64_000)
    timestamp: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    turn_id: str | None = Field(default=None, max_length=200)
    agent_id: str | None = Field(default=None, max_length=200)
    agent_type: str | None = Field(default=None, max_length=120)
    request: str | None = Field(default=None, max_length=64_000)


class ConversationCapture(BaseModel):
    kind: Literal["conversation"]
    mode: Literal["snapshot", "merge"] = "snapshot"
    session_id: str = Field(min_length=1, max_length=200)
    project: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=160)
    started_at: str = Field(min_length=1, max_length=64)
    ended_at: str = Field(min_length=1, max_length=64)
    messages: list[CaptureMessage] = Field(min_length=1, max_length=500)
    context: dict[str, Any]
    tags: list[str] = Field(max_length=32)
    privacy: str = Field(min_length=1, max_length=32)


class AgentNoteCapture(BaseModel):
    kind: Literal["note"]
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=64_000)
    project: str | None = Field(default=None, max_length=120)
    tags: list[str] = Field(default_factory=list)
    note_type: Literal["observation", "proposal", "handoff"] = "observation"
    note_kind: str | None = Field(default=None, max_length=80)
    outcome: Literal["success", "failure", "mixed", "unknown", "not_applicable"] = "unknown"
    session_id: str | None = Field(default=None, max_length=120)
    observed_at: str | None = None
    subject_id: str | None = Field(default=None, max_length=160)
    subject_aliases: list[str] = Field(default_factory=list)
    error_signatures: list[str] = Field(default_factory=list)
    applicability: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    relations: dict[str, list[str]] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    repository_sources: list[dict[str, Any]] = Field(default_factory=list)
    privacy: str = "normal"


CapturePayload = Annotated[ConversationCapture | AgentNoteCapture, Field(discriminator="kind")]


class VaultSearch(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)
    refresh: bool = False
    view: Literal["all", "current", "evidence", "history", "conflicts"] = "all"
    context: dict[str, Any] = Field(default_factory=dict)


SEARCH_RESULT_FIELDS = (
    "document_id",
    "path",
    "title",
    "snippet",
    "score",
    "match",
    "outcome",
    "review_state",
    "temporal_state",
    "conflict_state",
)


def search_response(result: dict[str, Any], view: str) -> dict[str, Any]:
    response = {
        key: result[key]
        for key in (
            "query",
            "embedding_model",
            "index",
            "context",
            "answer_state",
        )
        if key in result
    }
    response["view"] = view
    response["results"] = [
        {
            **{key: item[key] for key in SEARCH_RESULT_FIELDS if key in item},
            "role": search_result_role(item),
        }
        for item in result.get("results", [])
    ]
    return response


def search_result_role(item: dict[str, Any]) -> str:
    path = str(item.get("path") or "")
    if path.startswith("30_Conversations/raw/") or path.startswith("40_Agents/"):
        return "evidence"
    if item.get("temporal_state") in {"historical", "superseded"}:
        return "history"
    return "current"


def admin_password() -> str | None:
    return os.getenv("ADMIN_PASSWORD")


def admin_signing_secret() -> str | None:
    """The key behind admin cookies and CSRF tokens: ADMIN_PASSWORD (legacy) or the managed claim's own secret."""
    return onboarding.signing_secret() if onboarding.managed() else admin_password()


def admin_password_matches(candidate: str) -> bool:
    if onboarding.managed():
        return onboarding.verify_password(candidate)
    return hmac.compare_digest(secret_bytes(candidate), secret_bytes(admin_password() or ""))


def secret_bytes(value: str) -> bytes:
    # Environment values may carry surrogate-escaped bytes; compare raw bytes, never str.
    return value.encode("utf-8", "surrogateescape")


def cookie_signature(password: str, timestamp: str) -> str:
    return hmac.new(secret_bytes(password), timestamp.encode("ascii"), sha256).hexdigest()


def make_admin_cookie(password: str, now: int | None = None) -> str:
    timestamp = str(now if now is not None else int(time.time()))
    return f"{timestamp}.{cookie_signature(password, timestamp)}"


def valid_admin_cookie(cookie: str | None, password: str, now: int | None = None) -> bool:
    if not cookie:
        return False
    timestamp, separator, signature = cookie.partition(".")
    # isdigit() accepts non-ASCII digits that int() or compare_digest() would reject with an exception.
    if not separator or not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
        return False
    if not signature.isascii():
        return False
    age = (now if now is not None else int(time.time())) - int(timestamp)
    if age < 0 or age > ADMIN_SESSION_TTL_SECONDS:
        return False
    return hmac.compare_digest(signature.encode("ascii"), cookie_signature(password, timestamp).encode("ascii"))


def csrf_token_for(password: str, cookie: str) -> str:
    # Bound to this session cookie; the cookie is HttpOnly so pages cannot be forged without the password.
    return hmac.new(secret_bytes(password), b"pvg-csrf\0" + cookie.encode("utf-8"), sha256).hexdigest()


def admin_session(request: Request) -> str | None:
    password = admin_signing_secret()
    cookie = request.cookies.get(ADMIN_COOKIE)
    return cookie if password and valid_admin_cookie(cookie, password) else None


def admin_ok(request: Request) -> bool:
    return admin_session(request) is not None


def admin_csrf_token(request: Request) -> str:
    password = admin_signing_secret()
    cookie = admin_session(request)
    return csrf_token_for(password, cookie) if password and cookie else ""


def valid_csrf(request: Request, form: dict[str, str]) -> bool:
    expected = admin_csrf_token(request)
    supplied = form.get(CSRF_FIELD, "")
    return bool(expected) and hmac.compare_digest(supplied.encode("utf-8", "replace"), expected.encode("utf-8"))


def same_origin_request(request: Request) -> bool:
    """Supplement to the CSRF token: reject browser requests that declare another origin."""
    origin = request.headers.get("origin")
    if origin is not None:
        try:
            return urlsplit(origin).netloc.lower() == request.url.netloc.lower() != ""
        except ValueError:
            return False
    return request.headers.get("sec-fetch-site") in (None, "same-origin", "none")


def admin_cookie_attributes(request: Request) -> dict[str, Any]:
    # request.url.scheme already honours forwarded headers only when the server trusts the proxy.
    # PVG_SECURE_COOKIES=true forces Secure behind an HTTPS proxy that is not trusted for forwarded headers.
    forced = os.getenv("PVG_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes")
    return {"httponly": True, "samesite": "lax", "secure": forced or request.url.scheme == "https"}


def admin_login_client(request: Request) -> str:
    # Only the address the ASGI server resolved: uvicorn rewrites it from X-Forwarded-For solely for trusted
    # proxies, so a raw header sent by a client never selects (or escapes) its own bucket here.
    return request.client.host if request.client else "unknown"


def admin_login_attempt(client: str, now: float | None = None) -> int:
    """Count a password attempt; returns 0 if allowed, else the seconds until the client may try again.

    The attempt is counted before the password is compared, so concurrent guesses cannot exceed the limit.
    A blocked attempt is not counted, which keeps each client's state at ADMIN_LOGIN_MAX_ATTEMPTS timestamps.
    """
    now = time.monotonic() if now is None else now
    with admin_login_lock:
        recent = [stamp for stamp in admin_login_attempts.get(client, ()) if now - stamp < ADMIN_LOGIN_WINDOW_SECONDS]
        if len(recent) >= ADMIN_LOGIN_MAX_ATTEMPTS:
            admin_login_attempts[client] = recent
            return max(1, math.ceil(recent[0] + ADMIN_LOGIN_WINDOW_SECONDS - now))
        if client not in admin_login_attempts and len(admin_login_attempts) >= ADMIN_LOGIN_MAX_TRACKED:
            for key in [key for key, stamps in admin_login_attempts.items() if now - stamps[-1] >= ADMIN_LOGIN_WINDOW_SECONDS]:
                del admin_login_attempts[key]
            if len(admin_login_attempts) >= ADMIN_LOGIN_MAX_TRACKED:
                del admin_login_attempts[min(admin_login_attempts, key=lambda key: admin_login_attempts[key][-1])]
        admin_login_attempts[client] = [*recent, now]
        return 0


def admin_login_succeeded(client: str) -> None:
    with admin_login_lock:
        admin_login_attempts.pop(client, None)


SECRET_PATTERNS = (
    re.compile(r"pvg_[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(?<=://)[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)[^\s,;]+"),
)


def sanitize_error(exc: BaseException, settings: Settings | None = None) -> str:
    """Bounded, single-line error text with known secrets removed, safe to log, audit, or render."""
    try:
        text = str(exc)
    except Exception:
        text = ""
    secrets = [admin_password()]
    if settings:
        secrets += [settings.cloudflare_api_token, settings.qdrant_api_key]
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}[redacted]" if match.re.groups else "[redacted]", text)
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    if not text:
        text = type(exc).__name__
    return text if len(text) <= ERROR_TEXT_LIMIT else text[: ERROR_TEXT_LIMIT - 1] + "…"


def bounded_text(value: Any, limit: int) -> str:
    # Lone surrogates cannot be encoded as UTF-8 JSON; replace them rather than failing the response.
    text = str(value).encode("utf-8", "replace").decode("utf-8")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def html_page(title: str, body: str, status_code: int = 200, csrf: str | None = None, nav: str = "") -> HTMLResponse:
    logout = (
        f"""<form method="post" action="/admin/logout">
        <input type="hidden" name="{CSRF_FIELD}" value="{html.escape(csrf)}">
        <button type="submit" class="quiet">Log out</button>
      </form>"""
        if csrf
        else ""
    )
    nav_html = f"{nav}\n    " if nav and csrf else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --paper: #faf6ee; --ink: #1f1b16; --muted: #5c5447; --rule: #d8cfbd; --field: #fffdf8;
      --accent: #0f766e; --danger: #9b1c1c;
      --serif: Georgia, "Noto Serif KR", "Nanum Myeongjo", "AppleMyungjo", "Batang", serif;
      --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif;
      --mono: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.6 var(--sans); }}
    .wrap {{ max-width: 960px; margin: 0 auto; padding: 0 20px; }}
    .skip {{ position: absolute; left: -9999px; top: 8px; background: var(--ink); color: var(--paper); padding: 8px 12px; }}
    .skip:focus {{ left: 8px; }}
    .site {{ border-bottom: 2px solid var(--ink); }}
    .bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-top: 14px; padding-bottom: 14px; }}
    .bar form {{ margin: 0; }}
    .brand {{ margin: 0; font: 700 1.15rem var(--serif); }}
    .brand span {{ font: 400 .85rem var(--sans); color: var(--muted); letter-spacing: .08em; text-transform: uppercase; margin-left: 8px; }}
    .bar nav {{ display: flex; gap: 16px; }}
    main.wrap {{ padding-top: 32px; padding-bottom: 64px; }}
    h1, h2 {{ font-family: var(--serif); line-height: 1.2; margin: 0 0 12px; }}
    h1 {{ font-size: 2rem; }}
    h2 {{ font-size: 1.35rem; }}
    section {{ border-top: 1px solid var(--rule); margin-top: 32px; padding-top: 24px; }}
    p {{ margin: 0 0 12px; max-width: 68ch; }}
    a {{ color: var(--accent); }}
    code, pre, .mono {{ font-family: var(--mono); font-size: .9rem; }}
    code {{ overflow-wrap: anywhere; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; margin: 0 0 12px; padding: 14px; background: var(--field); border: 1px solid var(--ink); }}
    label {{ display: block; font-weight: 600; margin-top: 16px; }}
    input, select {{ display: block; width: 100%; max-width: 420px; margin: 6px 0 4px; padding: 10px; font: inherit; color: var(--ink); background: var(--field); border: 1px solid var(--ink); border-radius: 2px; }}
    input[type=hidden] {{ display: none; }}
    button, .button {{ display: inline-block; margin-top: 16px; padding: 10px 16px; font: 600 1rem var(--sans); color: #fff; background: var(--accent); border: 1px solid var(--accent); border-radius: 2px; cursor: pointer; text-decoration: none; }}
    button.quiet {{ margin-top: 0; color: var(--ink); background: transparent; border-color: var(--ink); }}
    button.danger {{ margin-top: 8px; background: var(--danger); border-color: var(--danger); }}
    :focus-visible {{ outline: 3px solid var(--accent); outline-offset: 2px; }}
    .hint {{ color: var(--muted); font-size: .9rem; }}
    .error {{ color: var(--danger); font-weight: 600; }}
    .notice {{ padding: 8px 12px; border-left: 4px solid var(--accent); background: var(--field); }}
    .cancel {{ margin-left: 16px; }}
    .facts {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 20px; margin: 0 0 16px; }}
    .facts dt {{ color: var(--muted); }}
    .facts dd {{ margin: 0; }}
    .sr {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid var(--rule); padding: 10px 8px 10px 0; text-align: left; vertical-align: top; }}
    th {{ font-size: .8rem; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); }}
    details {{ margin-bottom: 6px; }}
    summary {{ cursor: pointer; color: var(--accent); font-weight: 600; }}
    details form {{ margin: 8px 0 12px; }}
    @media (max-width: 720px) {{
      .agents, .agents tbody, .agents tr, .agents td {{ display: block; width: 100%; }}
      .agents thead {{ position: absolute; left: -9999px; }}
      .agents tr {{ padding: 14px 0; border-bottom: 1px solid var(--ink); }}
      .agents td {{ border: 0; padding: 4px 0; }}
      .agents td::before {{ content: attr(data-label); display: block; font-size: .75rem; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); }}
      .facts {{ grid-template-columns: 1fr; gap: 0; }}
      .facts dd {{ margin-bottom: 8px; }}
      input, select {{ max-width: none; }}
    }}
  </style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="site">
  <div class="wrap bar">
    <p class="brand">PersonaVault<span>Admin</span></p>
    {nav_html}{logout}
  </div>
</header>
<main id="main" class="wrap">
{body}
</main>
</body>
</html>""",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            # Not "no-referrer": browsers then serialize Origin as "null" on form POSTs, which the
            # same-origin guard must reject. "same-origin" sends the real Origin to this site only
            # and sends no Referer to any other site.
            "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
        },
    )


def admin_page(request: Request, title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return html_page(title, body, status_code, csrf=admin_csrf_token(request) or None, nav=admin_nav_html())


def admin_nav_html() -> str:
    if not onboarding.managed():
        return ""
    return '<nav aria-label="Admin"><a href="/admin/tokens">Agent tokens</a><a href="/admin/vault">Vault sync</a></nav>'


def notice_html(key: str | None) -> str:
    # Only fixed strings keyed by an allow-list: query input is never reflected.
    message = ADMIN_NOTICES.get(key or "")
    return f'<p class="notice" role="status">{html.escape(message)}</p>' if message else ""


def admin_disabled_html() -> str:
    return "<h1>Admin disabled</h1><p>Set <code>ADMIN_PASSWORD</code> and restart the Gateway to enable the admin pages.</p>"


def login_form_html(error: str | None = None, notice: str | None = None) -> str:
    error_html = f'<p class="error" id="login-error" role="alert">{html.escape(error)}</p>' if error else ""
    invalid = ' aria-invalid="true" aria-describedby="login-error"' if error else ""
    return f"""<h1>PersonaVault Admin</h1>
<p>Sign in to manage agent tokens and the search index.</p>
{notice_html(notice)}
<form method="post" action="/admin/login">
  {error_html}
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" autofocus required{invalid}>
  <button type="submit">Log in</button>
</form>"""


def hidden_inputs_html(csrf: str, **fields: str) -> str:
    values = {CSRF_FIELD: csrf, **fields}
    return "\n  ".join(
        f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">' for name, value in values.items()
    )


def agent_table_html(agents: list[dict[str, Any]], csrf: str = "") -> str:
    if not agents:
        return """<h2 id="agents-heading">Agents</h2>
<p>No agent tokens yet. Issue the first one with the form above.</p>"""
    rows = []
    for agent in agents:
        agent_id = html.escape(agent["agent_id"])
        scopes = html.escape(", ".join(agent["scopes"]))
        roots = "<br>".join(f"<code>{html.escape(root)}</code>" for root in agent["allowed_roots"]) or "none"
        status = "enabled" if agent["enabled"] else "disabled"
        if agent["enabled"]:
            action = f"""<details>
  <summary>Rotate<span class="sr"> {agent_id}</span></summary>
  <form method="post" action="/admin/tokens/rotate">
    {hidden_inputs_html(csrf, agent_id=agent["agent_id"], confirm="yes")}
    <p>Rotate <code>{agent_id}</code>? A new token is issued and the current one stops working immediately.</p>
    <button type="submit">Confirm rotate</button>
  </form>
</details>
<details>
  <summary>Disable<span class="sr"> {agent_id}</span></summary>
  <form method="post" action="/admin/tokens/disable">
    {hidden_inputs_html(csrf, agent_id=agent["agent_id"], confirm="yes")}
    <p>Disable <code>{agent_id}</code>? Requests with its token are rejected until you issue a new token for this ID.</p>
    <button type="submit" class="danger">Confirm disable</button>
  </form>
</details>"""
        else:
            action = '<span class="hint">Issue a token with this ID to re-enable it.</span>'
        rows.append(
            f"""<tr role="row">
  <td role="cell" data-label="Agent ID"><code>{agent_id}</code></td>
  <td role="cell" data-label="Status">{status}</td>
  <td role="cell" data-label="Token"><code>{html.escape(agent["token_prefix"])}...</code></td>
  <td role="cell" data-label="Scopes"><code>{scopes}</code></td>
  <td role="cell" data-label="Allowed roots">{roots}</td>
  <td role="cell" data-label="Last token use">{html.escape(agent["last_used_at"] or "never")}</td>
  <td role="cell" data-label="Rotated">{html.escape(agent["rotated_at"] or "-")}</td>
  <td role="cell" data-label="Actions">{action}</td>
</tr>"""
        )
    return f"""<h2 id="agents-heading">Agents</h2>
<table class="agents" role="table" aria-labelledby="agents-heading">
  <thead role="rowgroup"><tr role="row"><th role="columnheader" scope="col">Agent ID</th><th role="columnheader" scope="col">Status</th><th role="columnheader" scope="col">Token</th><th role="columnheader" scope="col">Scopes</th><th role="columnheader" scope="col">Allowed roots</th><th role="columnheader" scope="col">Last token use</th><th role="columnheader" scope="col">Rotated</th><th role="columnheader" scope="col">Actions</th></tr></thead>
  <tbody role="rowgroup">{"".join(rows)}</tbody>
</table>
<p class="hint">Last token use is when the token last authenticated any Gateway request, including searches and health checks. It does not mean a capture completed or that anything was saved.</p>"""


def rag_section_html(csrf: str, semantic_enabled: bool) -> str:
    if not semantic_enabled:
        return """<section aria-labelledby="rag-heading">
  <h2 id="rag-heading">RAG index</h2>
  <p><strong>Semantic search is disabled</strong> (<code>EMBEDDING_PROVIDER=none</code>). Search uses keyword matching over the vault only, and no embedding provider or vector index is contacted. There is nothing to update here, and any existing vector data is left untouched.</p>
</section>"""
    return f"""<section aria-labelledby="rag-heading">
  <h2 id="rag-heading">RAG index</h2>
  <p>Updates the search index from the vault now and waits for it to finish, so it can take a while. Keep this page open; the result shows the counts from the finished run.</p>
  <form method="post" action="/admin/rag/rebuild">
    {hidden_inputs_html(csrf)}
    <button type="submit">Update RAG index</button>
  </form>
</section>"""


def token_form_html(
    agents: list[dict[str, Any]], csrf: str = "", notice: str | None = None, semantic_enabled: bool = True
) -> str:
    return f"""<h1>Agent tokens</h1>
<p>Bearer tokens let plugins and agents call this Gateway. Only a token's hash is stored; the raw token is shown once when it is issued.</p>
{notice_html(notice)}
<section aria-labelledby="issue-heading">
  <h2 id="issue-heading">Issue a token</h2>
  <form method="post" action="/admin/tokens">
    {hidden_inputs_html(csrf)}
    <label for="agent_id">Agent ID</label>
    <input id="agent_id" name="agent_id" class="mono" value="{DEFAULT_AGENT_ID}" pattern="[a-z0-9][a-z0-9_-]{{0,63}}" autocomplete="off" spellcheck="false" aria-describedby="agent-id-hint" required>
    <p class="hint" id="agent-id-hint">Lowercase letters, numbers, <code>-</code> and <code>_</code>. An ID that already has a token asks for confirmation before the token is replaced.</p>
    <label for="permission">Permission</label>
    <select name="permission" id="permission" aria-describedby="permission-hint" required>
      <option value="read">Read</option>
      <option value="write">Write</option>
      <option value="read-write" selected>Read + Write</option>
    </select>
    <p class="hint" id="permission-hint">Read (<code>vault-rag</code>) searches the whole Markdown vault except <code>.git</code>, <code>.obsidian</code>, and <code>.tmp</code>. Write (<code>conversation-log</code>, <code>agent-memo</code>) is restricted to <code>30_Conversations/raw</code>.</p>
    <button type="submit">Issue token</button>
  </form>
</section>
<section aria-labelledby="agents-heading">
{agent_table_html(agents, csrf)}
</section>
{rag_section_html(csrf, semantic_enabled)}"""


def token_created_html(token: str, agent_id: str | None = None, rotated: bool = False) -> str:
    who = f"<p>Agent ID: <code>{html.escape(agent_id)}</code></p>" if agent_id else ""
    return f"""<h1>Agent token {"rotated" if rotated else "created"}</h1>
{who}
<p>Store this token now. It is shown only once and only its hash is saved, so reloading or leaving this page will not show it again.</p>
<pre id="issued-token" tabindex="0">{html.escape(token)}</pre>
<p><button type="button" id="copy-token" hidden>Copy token</button> <span id="copy-status" role="status"></span></p>
<p><a href="/admin/tokens">Back to tokens</a></p>
<script>
(function () {{
  var button = document.getElementById("copy-token");
  var token = document.getElementById("issued-token");
  var status = document.getElementById("copy-status");
  function select() {{
    var range = document.createRange();
    range.selectNodeContents(token);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }}
  function fallback(message) {{
    select();
    status.textContent = message + " The token is selected; press Ctrl/Cmd+C to copy it.";
  }}
  button.hidden = false;
  button.addEventListener("click", function () {{
    try {{
      if (!(navigator.clipboard && window.isSecureContext)) {{ return fallback("Clipboard unavailable."); }}
      navigator.clipboard.writeText(token.textContent).then(
        function () {{ select(); status.textContent = "Copied."; }},
        function () {{ fallback("Copy failed."); }}
      );
    }} catch (error) {{ fallback("Copy failed."); }}
  }});
}})();
</script>"""


def agent_facts_html(agent: dict[str, Any]) -> str:
    return f"""<dl class="facts">
  <dt>Agent ID</dt><dd><code>{html.escape(agent["agent_id"])}</code></dd>
  <dt>Status</dt><dd>{"enabled" if agent["enabled"] else "disabled"}</dd>
  <dt>Current token</dt><dd><code>{html.escape(agent["token_prefix"])}...</code></dd>
  <dt>Current scopes</dt><dd><code>{html.escape(", ".join(agent["scopes"]))}</code></dd>
</dl>"""


def confirm_page_html(
    heading: str, message_html: str, action: str, csrf: str, fields: dict[str, str], button: str, danger: bool = False
) -> str:
    return f"""<h1>{html.escape(heading)}</h1>
{message_html}
<form method="post" action="{html.escape(action)}">
  {hidden_inputs_html(csrf, **fields, confirm="yes")}
  <button type="submit"{' class="danger"' if danger else ""}>{html.escape(button)}</button>
  <a class="cancel" href="/admin/tokens">Cancel</a>
</form>"""


def rag_rebuilt_html(result: dict[str, Any]) -> str:
    rows = (
        ("Markdown files scanned", result.get("files", 0)),
        ("Chunks in index", result.get("chunks", 0)),
        ("Chunks newly embedded", result.get("updated", 0)),
        ("Chunks with metadata-only update", result.get("payload_updated", 0)),
        ("Stale chunks deleted", result.get("deleted", 0)),
        ("Embedding model", result.get("model", "-")),
    )
    facts = "\n  ".join(f"<dt>{label}</dt><dd>{html.escape(str(value))}</dd>" for label, value in rows)
    return f"""<h1>RAG index updated</h1>
<p>The update finished. These are the counts from this run.</p>
<dl class="facts">
  {facts}
</dl>
<p><a href="/admin/tokens">Back to tokens</a></p>"""


def rag_disabled_html() -> str:
    return """<h1>Semantic search is disabled</h1>
<p>Nothing was updated. <code>EMBEDDING_PROVIDER=none</code> keeps search keyword-only, so no embedding provider or vector index was contacted. Existing vector data is left untouched.</p>
<p><a href="/admin/tokens">Back to tokens</a></p>"""


def rag_failed_html(message: str) -> str:
    return f"""<h1>RAG index update failed</h1>
<p class="error" role="alert">{html.escape(message)}</p>
<p>The index may be partly updated, so search results can be incomplete until an update succeeds. Check that Qdrant and the embedding provider are reachable and that the vault is not changing, then update again. If another update is already running, wait for it to finish. The Gateway log has details.</p>
<p><a href="/admin/tokens">Back to tokens</a></p>"""


def error_page_html(heading: str, message: str) -> str:
    return f'<h1>{html.escape(heading)}</h1>\n<p class="error" role="alert">{html.escape(message)}</p>\n<p><a href="/admin/tokens">Back to tokens</a></p>'


def setup_form_html(error: str | None = None) -> str:
    error_html = f'<p class="error" id="setup-error" role="alert">{html.escape(error)}</p>' if error else ""
    return f"""<h1>Set up PersonaVault</h1>
<p>Claim this Gateway by choosing the administrator password. The setup code is printed once in the server's startup output (or is the value of <code>PVG_SETUP_TOKEN</code>). It is never shown in the browser.</p>
<form method="post" action="/setup">
  {error_html}
  <label for="setup_code">Setup code</label>
  <input id="setup_code" name="setup_code" type="password" autocomplete="off" spellcheck="false" maxlength="200" autofocus required>
  <label for="password">Admin password</label>
  <input id="password" name="password" type="password" autocomplete="new-password" minlength="16" maxlength="128" aria-describedby="password-hint" required>
  <p class="hint" id="password-hint">16-128 characters. Only a salted hash is stored.</p>
  <label for="confirm">Confirm password</label>
  <input id="confirm" name="confirm" type="password" autocomplete="new-password" minlength="16" maxlength="128" required>
  <button type="submit">Claim this Gateway</button>
</form>"""


def vault_error_html(code: str | None) -> str:
    message = onboarding.ERROR_TEXT.get(code or "", onboarding.ERROR_TEXT["failed"])
    return f'<p class="error" role="alert">{html.escape(message)}</p>'


def vault_url_form_html(csrf: str, current: str | None, button: str) -> str:
    return f"""<form method="post" action="/admin/vault">
  {hidden_inputs_html(csrf)}
  <label for="repo_url">Repository SSH URL</label>
  <input id="repo_url" name="repo_url" class="mono" value="{html.escape(current or "")}" placeholder="git@github.com:OWNER/REPO.git" maxlength="200" autocomplete="off" spellcheck="false" aria-describedby="repo-url-hint" required>
  <p class="hint" id="repo-url-hint">GitHub SSH URLs only. The repository needs at least one commit.</p>
  <button type="submit">{html.escape(button)}</button>
</form>"""


def vault_page_html(status: dict[str, Any], csrf: str, notice: str | None = None) -> str:
    state, sync, url = status["state"], status["sync"], status["repo_url"]
    reload_script = "<script>setTimeout(function () { location.reload(); }, 3000);</script>"
    if state == "ready":
        labels = {"idle": "starting", "running": "running now", "ok": "ok", "error": "failing", "blocked": "BLOCKED"}
        problem = vault_error_html(sync["error"]) if sync["error"] else ""
        retry = f"{sync['next_delay']} s after the last attempt" if sync["next_delay"] else "-"
        return f"""<h1>Vault connected</h1>
{notice_html(notice)}
<dl class="facts">
  <dt>Repository</dt><dd><code>{html.escape(url or "")}</code></dd>
  <dt>Sync</dt><dd>{html.escape(labels.get(sync["state"], sync["state"]))}</dd>
  <dt>Last attempt</dt><dd>{html.escape(sync["last_attempt"] or "-")}</dd>
  <dt>Last success</dt><dd>{html.escape(sync["last_ok"] or "never")}</dd>
  <dt>Next attempt</dt><dd>{html.escape(retry)}</dd>
  <dt>Consecutive failures</dt><dd>{int(sync["failures"])}</dd>
</dl>
{problem}
<p class="hint">Changes are committed, pulled with rebase and pushed from this server. Push access is only proven by a successful sync; a rejected push shows up here.</p>
<form method="post" action="/admin/vault/sync">
  {hidden_inputs_html(csrf)}
  <button type="submit">Sync now</button>
</form>
<p><a href="/admin/tokens">Manage agent tokens</a></p>
{reload_script if sync["state"] == "running" or not sync["last_attempt"] else ""}"""
    if state in ("unconfigured", None):
        return f"""<h1>Connect your Vault</h1>
{notice_html(notice)}
<p>Enter the Git repository that stores your Vault. The Gateway generates a private deploy key on this server and shows only its public half. Until the Vault is connected, agent tokens and Vault access stay unavailable.</p>
{vault_url_form_html(csrf, None, "Generate deploy key")}"""
    cloning = state == "cloning" or status["cloning"]
    link = status["registration_link"]
    link_html = (
        f'<p><a href="{html.escape(link)}" target="_blank" rel="noopener noreferrer">Open the deploy key page for this repository</a>. '
        "Use the title <code>persona-vault-sync</code>, paste the key and tick <strong>Allow write access</strong>.</p>"
        if link
        else ""
    )
    key_html = f'<pre id="public-key" tabindex="0">{html.escape(status["public_key"])}</pre>' if status["public_key"] else ""
    connect = (
        '<p role="status">Cloning in the background. This page refreshes on its own.</p>'
        if cloning
        else f"""<form method="post" action="/admin/vault/connect">
  {hidden_inputs_html(csrf)}
  <button type="submit">{"Retry" if state == "failed" else "Connect and clone"}</button>
</form>"""
    )
    problem = vault_error_html(status["error"]) if state == "failed" and not cloning else ""
    return f"""<h1>Connect your Vault</h1>
{notice_html(notice)}
<dl class="facts">
  <dt>Repository</dt><dd><code>{html.escape(url or "")}</code></dd>
  <dt>Status</dt><dd>{"cloning" if cloning else "failed" if state == "failed" else "waiting for the deploy key"}</dd>
</dl>
{problem}
<section aria-labelledby="key-heading">
  <h2 id="key-heading">1. Register the deploy key</h2>
  <p>This is the public key only; the private key stays on this server and is never displayed.</p>
  {key_html}
  {link_html}
</section>
<section aria-labelledby="connect-heading">
  <h2 id="connect-heading">2. Connect</h2>
  <p>The repository is cloned into a staging area and published only when the clone is complete. This step cannot confirm write access; if pushes are rejected later, the sync status says so.</p>
  {connect}
</section>
<section aria-labelledby="change-heading">
  <details>
    <summary id="change-heading">Use a different repository</summary>
    {vault_url_form_html(csrf, url, "Save repository")}
  </details>
</section>
{reload_script if cloning else ""}"""


async def bounded_form(request: Request, limit: int) -> dict[str, str]:
    """Parse a small urlencoded form, aborting while streaming once it exceeds the limit (unauthenticated callers)."""
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="unsupported content type")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="form too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="form too large")
    values = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: items[-1] for key, items in values.items()}


async def form_values(request: Request) -> dict[str, str]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > ADMIN_FORM_MAX_BYTES:
        raise HTTPException(status_code=413, detail="form too large")
    body = await request.body()
    if len(body) > ADMIN_FORM_MAX_BYTES:
        raise HTTPException(status_code=413, detail="form too large")
    text = body.decode("utf-8", "replace")
    return {key: values[-1] for key, values in parse_qs(text, keep_blank_values=True).items()}


def admin_settings() -> Settings:
    settings = Settings.from_env()
    init_db(settings.db_path)
    return settings


async def admin_mutation(request: Request) -> tuple[dict[str, str], Response | None]:
    """Authenticate an admin form POST and enforce the CSRF token; returns (form, early response)."""
    if not admin_ok(request):
        return {}, RedirectResponse("/admin/login", status_code=303)
    form = await form_values(request)
    if not same_origin_request(request) or not valid_csrf(request, form):
        settings = admin_settings()
        audit(
            settings.db_path,
            action="admin-csrf",
            route=request.url.path,
            status="denied",
            error_message="origin or csrf token check failed",
            **request_meta(request),
        )
        return form, admin_page(
            request,
            "Request blocked",
            error_page_html(
                "Request blocked",
                "The security check for this form failed, so nothing was changed. Reload the Admin page and submit the form again.",
            ),
            403,
        )
    return form, None


def find_agent(settings: Settings, agent_id: str) -> dict[str, Any] | None:
    return next((agent for agent in list_agents(settings.db_path) if agent["agent_id"] == agent_id), None)


def scopes_for_permission(permission: str) -> list[str]:
    scopes = TOKEN_PERMISSION_SCOPES.get(permission)
    if not scopes:
        raise ValueError("permission must be read, write, or read-write")
    return list(scopes)


def allowed_roots_for_agent(agent_id: str, scopes: list[str] | None = None) -> list[str]:
    selected = set(scopes) if scopes is not None else {"conversation-log", "agent-memo"}
    return ["30_Conversations/raw"] if selected & {"conversation-log", "agent-memo"} else []


@app.on_event("startup")
def startup() -> None:
    global embedding_retry_thread
    if onboarding.managed():
        onboarding.start()  # layout defaults, operator banner, resume a configured Vault
    settings = Settings.from_env()
    if not onboarding.managed():
        settings.vault_dir.mkdir(parents=True, exist_ok=True)  # managed mode publishes the Vault only after a clone
    init_db(settings.db_path)
    embedding_retry_stop.clear()
    if settings.embedding_provider == "none":
        return  # Keyword-only mode has nothing to retry and must not schedule indexing.
    if not embedding_retry_thread or not embedding_retry_thread.is_alive():
        # ponytail: process-local scheduling is enough for the single Gateway replica.
        embedding_retry_thread = Thread(
            target=embedding_retry_loop,
            args=(settings,),
            daemon=True,
            name="pvg-embedding-retry",
        )
        embedding_retry_thread.start()


def embedding_retry_loop(settings: Settings) -> None:
    while not embedding_retry_stop.wait(EMBEDDING_RETRY_POLL_SECONDS):
        if not onboarding.vault_ready():
            continue  # no Vault access, including indexing, before the Vault is connected
        try:
            retry_due_embeddings(settings)
        except Exception as exc:  # BaseException (shutdown, interrupts) still ends the thread
            logger.error(
                "Automatic embedding retry failed (%s): %s", type(exc).__name__, sanitize_error(exc, settings)
            )


@app.on_event("shutdown")
def shutdown() -> None:
    embedding_retry_stop.set()
    onboarding.stop()
    if embedding_retry_thread:
        embedding_retry_thread.join(timeout=1)


def request_meta(request: Request) -> dict[str, str | None]:
    return {
        "remote_addr": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": request.headers.get("x-request-id"),
    }


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # The default handler echoes the rejected input, which crashes on lone surrogates and leaks payloads.
    errors = [
        {
            "type": bounded_text(error.get("type", "invalid"), 80),
            "loc": [
                bounded_text(part, 64) if isinstance(part, str) else part
                for part in list(error.get("loc", ()))[:VALIDATION_LOC_LIMIT]
            ],
            "msg": bounded_text(error.get("msg", "Invalid value"), 200),
        }
        for error in exc.errors()[:VALIDATION_ERROR_LIMIT]
    ]
    return JSONResponse({"detail": errors}, status_code=422)


def admin_unavailable_response() -> Response:
    # Managed mode before the claim: the browser setup page replaces the "set ADMIN_PASSWORD" message.
    if onboarding.managed() and not onboarding.claimed():
        return RedirectResponse("/setup", status_code=303)
    if onboarding.managed():  # claimed, but the credential record is unreadable: fail closed
        return html_page(
            "Admin unavailable",
            "<h1>Admin unavailable</h1><p>The administrator credential record is unreadable, so login is disabled. Restore the data volume's <code>setup</code> directory.</p>",
            503,
        )
    return html_page("Admin disabled", admin_disabled_html(), 503)


@app.get("/admin/login")
def admin_login_page(request: Request, notice: str | None = None):
    if admin_ok(request):
        return RedirectResponse("/admin/tokens", status_code=303)
    if not admin_signing_secret():
        return admin_unavailable_response()
    return html_page("Admin login", login_form_html(notice=notice))


@app.post("/admin/login")
async def admin_login(request: Request):
    secret = admin_signing_secret()
    if not secret:
        return admin_unavailable_response()

    settings = admin_settings()
    # A cross-site form must neither log in nor burn the real administrator's attempts.
    if not same_origin_request(request):
        audit(
            settings.db_path,
            action="admin-login",
            route=request.url.path,
            status="denied",
            error_message="cross-origin login rejected",
            **request_meta(request),
        )
        return html_page("Admin login", login_form_html(error="This login request came from another site and was blocked."), 403)
    client = admin_login_client(request)
    retry_after = admin_login_attempt(client)
    if retry_after:
        response = html_page(
            "Admin login",
            login_form_html(error=f"Too many login attempts. Try again in {math.ceil(retry_after / 60)} minute(s)."),
            429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    form = await form_values(request)
    if not await run_in_threadpool(admin_password_matches, form.get("password", "")):
        audit(
            settings.db_path,
            action="admin-login",
            route=request.url.path,
            status="denied",
            error_message="invalid password",
            **request_meta(request),
        )
        return html_page("Admin login", login_form_html(error="Incorrect password. Try again."), 401)

    audit(settings.db_path, action="admin-login", route=request.url.path, status="ok", **request_meta(request))
    admin_login_succeeded(client)
    response = RedirectResponse("/admin/tokens", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE,
        make_admin_cookie(secret),
        max_age=ADMIN_SESSION_TTL_SECONDS,
        **admin_cookie_attributes(request),
    )
    return response


@app.post("/admin/logout")
async def admin_logout(request: Request):
    if admin_ok(request):
        _, rejected = await admin_mutation(request)
        if rejected:
            return rejected
    response = RedirectResponse("/admin/login?notice=logged-out", status_code=303)
    response.delete_cookie(ADMIN_COOKIE, **admin_cookie_attributes(request))
    return response


@app.get("/admin/tokens")
def admin_tokens_page(request: Request, notice: str | None = None):
    if not admin_ok(request):
        return RedirectResponse("/admin/login", status_code=303)
    if not onboarding.vault_ready():  # managed mode: finish connecting the Vault first
        return RedirectResponse("/admin/vault", status_code=303)
    settings = admin_settings()
    return admin_page(
        request,
        "Agent tokens",
        token_form_html(
            list_agents(settings.db_path),
            admin_csrf_token(request),
            notice,
            semantic_enabled=settings.embedding_provider != "none",
        ),
    )


@app.post("/admin/tokens")
async def admin_tokens_create(request: Request):
    form, rejected = await admin_mutation(request)
    if rejected:
        return rejected

    settings = admin_settings()
    agent_id = form.get("agent_id", "")
    permission = form.get("permission", "")
    # No await below: the existence check and upsert run as one step on the event loop.
    try:
        scopes = scopes_for_permission(permission)
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise ValueError("agent_id must be lowercase letters, numbers, '-' or '_'")
        existing = find_agent(settings, agent_id)
        if existing and form.get("confirm") != "yes":
            state = (
                "is disabled. Continuing re-enables it with a new token."
                if not existing["enabled"]
                else "already has a token. Continuing replaces it, and the current token stops working immediately."
            )
            return admin_page(
                request,
                "Confirm token replacement",
                confirm_page_html(
                    "Replace existing token?",
                    f"<p>Agent ID <code>{html.escape(agent_id)}</code> {state} Nothing has changed yet.</p>\n"
                    f"{agent_facts_html(existing)}\n<p>New scopes: <code>{html.escape(', '.join(scopes))}</code></p>",
                    "/admin/tokens",
                    admin_csrf_token(request),
                    {"agent_id": agent_id, "permission": permission},
                    "Replace token",
                    danger=True,
                ),
            )
        token = generate_token()
        upsert_agent(
            settings.db_path,
            agent_id,
            token,
            scopes,
            allowed_roots_for_agent(agent_id, scopes),
        )
    except ValueError as exc:
        audit(
            settings.db_path,
            action="admin-token",
            route=request.url.path,
            status="error",
            agent_id=agent_id,
            error_message=sanitize_error(exc, settings),
            **request_meta(request),
        )
        return admin_page(request, "Issue token", error_page_html("Token not issued", str(exc)), 400)

    audit(settings.db_path, action="admin-token", route=request.url.path, status="ok", agent_id=agent_id, **request_meta(request))
    return admin_page(request, "Agent token created", token_created_html(token, agent_id))


@app.post("/admin/tokens/disable")
async def admin_tokens_disable(request: Request):
    form, rejected = await admin_mutation(request)
    if rejected:
        return rejected

    settings = admin_settings()
    agent_id = form.get("agent_id", "")
    agent = find_agent(settings, agent_id)
    if not agent:
        audit(
            settings.db_path,
            action="admin-token-disable",
            route=request.url.path,
            status="error",
            agent_id=agent_id,
            error_message="agent not found",
            **request_meta(request),
        )
        return admin_page(request, "Disable agent token", error_page_html("Agent not found", "No agent has that ID."), 404)
    if form.get("confirm") != "yes":
        return admin_page(
            request,
            "Confirm disable",
            confirm_page_html(
                "Disable this token?",
                f"<p>Requests using this agent's token will be rejected. Nothing has changed yet.</p>\n{agent_facts_html(agent)}",
                "/admin/tokens/disable",
                admin_csrf_token(request),
                {"agent_id": agent_id},
                "Disable token",
                danger=True,
            ),
        )

    status = "ok" if disable_agent(settings.db_path, agent_id) else "error"
    audit(
        settings.db_path,
        action="admin-token-disable",
        route=request.url.path,
        status=status,
        agent_id=agent_id,
        **request_meta(request),
    )
    return RedirectResponse("/admin/tokens?notice=disabled", status_code=303)


@app.post("/admin/tokens/rotate")
async def admin_tokens_rotate(request: Request):
    form, rejected = await admin_mutation(request)
    if rejected:
        return rejected

    settings = admin_settings()
    agent_id = form.get("agent_id", "")
    agent = find_agent(settings, agent_id)
    if not agent or not agent["enabled"]:
        audit(
            settings.db_path,
            action="admin-token-rotate",
            route=request.url.path,
            status="error",
            agent_id=agent_id,
            error_message="enabled agent not found",
            **request_meta(request),
        )
        return admin_page(request, "Rotate agent token", error_page_html("Agent not found", "No enabled agent has that ID."), 404)
    if form.get("confirm") != "yes":
        return admin_page(
            request,
            "Confirm rotate",
            confirm_page_html(
                "Rotate this token?",
                f"<p>A new token is issued and the current one stops working immediately. Nothing has changed yet.</p>\n{agent_facts_html(agent)}",
                "/admin/tokens/rotate",
                admin_csrf_token(request),
                {"agent_id": agent_id},
                "Rotate token",
                danger=True,
            ),
        )

    token = generate_token()
    upsert_agent(settings.db_path, agent_id, token, agent["scopes"], agent["allowed_roots"])
    audit(settings.db_path, action="admin-token-rotate", route=request.url.path, status="ok", agent_id=agent_id, **request_meta(request))
    return admin_page(request, "Agent token rotated", token_created_html(token, agent_id, rotated=True))


@app.post("/admin/rag/rebuild")
async def admin_rag_rebuild(request: Request):
    _, rejected = await admin_mutation(request)
    if rejected:
        return rejected

    if not onboarding.vault_ready():
        return admin_page(
            request, "Vault not ready", error_page_html("Vault not ready", "Connect the Vault before updating the index."), 503
        )
    settings = admin_settings()
    try:
        result = await run_in_threadpool(index_vault, settings)
    except (RuntimeError, ValueError) as exc:
        message = sanitize_error(exc, settings)
        logger.error("Admin RAG index update failed (%s): %s", type(exc).__name__, message)
        audit(
            settings.db_path,
            action="admin-rag-rebuild",
            route=request.url.path,
            status="error",
            error_message=message,
            **request_meta(request),
        )
        return admin_page(request, "RAG index update failed", rag_failed_html(message), 503)

    if result.get("semantic") == "disabled":
        audit(settings.db_path, action="admin-rag-rebuild", route=request.url.path, status="disabled", **request_meta(request))
        return admin_page(request, "Semantic search is disabled", rag_disabled_html())
    audit(settings.db_path, action="admin-rag-rebuild", route=request.url.path, status="ok", **request_meta(request))
    return admin_page(request, "RAG index updated", rag_rebuilt_html(result))


def require_managed() -> None:
    # Outside managed mode these routes do not exist: same 404 body as any unknown path.
    if not onboarding.managed():
        raise HTTPException(status_code=404, detail="Not Found")


def require_vault_ready() -> None:
    if not onboarding.vault_ready():
        raise HTTPException(status_code=503, detail="Vault setup is not complete")


@app.get("/", include_in_schema=False)
def root_redirect(request: Request):
    require_managed()
    if not onboarding.claimed():
        return RedirectResponse("/setup", status_code=303)
    if not admin_ok(request):
        return RedirectResponse("/admin/login", status_code=303)
    return RedirectResponse("/admin/tokens" if onboarding.vault_ready() else "/admin/vault", status_code=303)


@app.get("/setup", include_in_schema=False)
def setup_page():
    require_managed()  # nothing from the query string is read or reflected: the setup code is POST-only
    if onboarding.claimed():
        return RedirectResponse("/", status_code=303)
    return html_page("Set up PersonaVault", setup_form_html())


@app.post("/setup", include_in_schema=False)
async def setup_claim(request: Request):
    require_managed()
    if onboarding.claimed():
        return RedirectResponse("/", status_code=303)
    settings = admin_settings()
    if not same_origin_request(request):
        audit(settings.db_path, action="setup-claim", route=request.url.path, status="denied",
              error_message="cross-origin claim rejected", **request_meta(request))
        return html_page("Set up PersonaVault", setup_form_html("This request came from another site and was blocked."), 403)
    client = f"setup:{admin_login_client(request)}"
    retry_after = admin_login_attempt(client)  # counted before the code is compared
    if retry_after:
        response = html_page(
            "Set up PersonaVault",
            setup_form_html(f"Too many attempts. Try again in {math.ceil(retry_after / 60)} minute(s)."),
            429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    form = await bounded_form(request, SETUP_FORM_MAX_BYTES)
    try:
        secret = await run_in_threadpool(
            onboarding.claim, form.get("setup_code", ""), form.get("password", ""), form.get("confirm", "")
        )
    except onboarding.SetupError as exc:
        audit(settings.db_path, action="setup-claim", route=request.url.path, status="denied",
              error_message=type(exc).__name__, **request_meta(request))
        return html_page("Set up PersonaVault", setup_form_html(str(exc)), exc.status)
    except OSError:
        logger.error("Setup claim could not be stored")
        return html_page("Set up PersonaVault", setup_form_html("Setup state could not be stored."), 500)
    admin_login_succeeded(client)
    audit(settings.db_path, action="setup-claim", route=request.url.path, status="ok", **request_meta(request))
    response = RedirectResponse("/admin/vault", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE, make_admin_cookie(secret), max_age=ADMIN_SESSION_TTL_SECONDS, **admin_cookie_attributes(request)
    )
    return response


@app.get("/admin/vault", include_in_schema=False)
def admin_vault_page(request: Request, notice: str | None = None):
    require_managed()
    if not admin_ok(request):
        return RedirectResponse("/admin/login", status_code=303)
    return admin_page(
        request, "Vault sync", vault_page_html(onboarding.manager().status(), admin_csrf_token(request), notice)
    )


async def admin_vault_action(request: Request, action: str, notice: str):
    """Shared shape of the vault form POSTs: authenticate, run one manager call, redirect to the status page."""
    require_managed()
    form, rejected = await admin_mutation(request)
    if rejected:
        return rejected
    settings = admin_settings()
    try:
        await run_in_threadpool(action, form)
    except onboarding.SetupError as exc:
        audit(settings.db_path, action="admin-vault", route=request.url.path, status="error",
              error_message=type(exc).__name__, **request_meta(request))
        return admin_page(request, "Vault sync", error_page_html("Vault not changed", str(exc)), exc.status)
    audit(settings.db_path, action="admin-vault", route=request.url.path, status="ok", **request_meta(request))
    return RedirectResponse(f"/admin/vault?notice={notice}", status_code=303)


@app.post("/admin/vault", include_in_schema=False)
async def admin_vault_save(request: Request):
    return await admin_vault_action(
        request, lambda form: onboarding.manager().configure(form.get("repo_url", "")), "vault-saved"
    )


@app.post("/admin/vault/connect", include_in_schema=False)
async def admin_vault_connect(request: Request):
    return await admin_vault_action(request, lambda form: onboarding.manager().connect(), "vault-connecting")


@app.post("/admin/vault/sync", include_in_schema=False)
async def admin_vault_sync(request: Request):
    return await admin_vault_action(request, lambda form: onboarding.manager().sync_now(), "vault-syncing")


def current_agent(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = Settings.from_env()
    init_db(settings.db_path)
    if not authorization or not authorization.startswith("Bearer "):
        audit(
            settings.db_path,
            action="auth",
            route=request.url.path,
            status="denied",
            error_message="missing bearer token",
            **request_meta(request),
        )
        raise HTTPException(status_code=401, detail="invalid token")
    agent = lookup_agent(settings.db_path, authorization.removeprefix("Bearer ").strip())
    if not agent:
        audit(
            settings.db_path,
            action="auth",
            route=request.url.path,
            status="denied",
            error_message="invalid bearer token",
            **request_meta(request),
        )
        raise HTTPException(status_code=401, detail="invalid token")
    return agent


def capabilities() -> dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "features": [
            "raw-evidence-v1",
            "capture-union-v1",
            "search-views-v1",
            "wiki-analysis-v1",
            "rag-health-v1",
            "conversation-upsert-v3",
            "conversation-merge-v1",
            "working-agreement-v1",
        ],
        "plugin": {
            "name": PLUGIN_NAME,
            "min_version": PLUGIN_MIN_VERSION,
            "marketplace": PLUGIN_MARKETPLACE,
            "url": PLUGIN_URL,
        },
    }


def retire_api(request: Request, agent: dict[str, Any]) -> None:
    settings = Settings.from_env()
    requested_version = next(
        (part for part in request.url.path.split("/") if part in RETIRED_API_VERSIONS),
        "unknown",
    )
    audit(
        settings.db_path,
        action="api-version",
        route=request.url.path,
        status="denied",
        agent_id=agent["agent_id"],
        error_message="client_upgrade_required",
        **request_meta(request),
    )
    raise HTTPException(
        status_code=410,
        detail={
            "code": "client_upgrade_required",
            "reason": "api_version_retired",
            "message": "Update the PersonaVault client and retry the unchanged request.",
            "requested_api_version": requested_version,
            "current_api_version": API_VERSION,
            "current_api_base": f"/gateway/{API_VERSION}",
            "plugin": capabilities()["plugin"],
            "retryable_after_update": True,
            "preserve_request_body": True,
        },
        headers={
            "Link": (
                f'</gateway/{API_VERSION}/capabilities>; rel="successor-version", '
                f'<{PLUGIN_URL}>; rel="latest-version"'
            )
        },
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    if not onboarding.vault_ready():
        return JSONResponse({"status": "pending", "setup": onboarding.setup_phase()}, status_code=503)
    result = rag_readiness(Settings.from_env())
    return JSONResponse(result, status_code=200 if result.get("status") == "ok" else 503)


@app.get("/gateway/v1/capabilities", include_in_schema=False)
@app.get("/gateway/v1/working-agreement", include_in_schema=False)
@app.post("/gateway/v1/conversation-log", include_in_schema=False)
@app.post("/gateway/v1/agent-memo", include_in_schema=False)
@app.post("/gateway/v1/rag-search", include_in_schema=False)
@app.get("/gateway/v1/rag-health", include_in_schema=False)
@app.get("/gateway/v2/capabilities", include_in_schema=False)
@app.get("/gateway/v2/working-agreement", include_in_schema=False)
@app.post("/gateway/v2/conversation-log", include_in_schema=False)
@app.post("/gateway/v2/agent-memo", include_in_schema=False)
@app.post("/gateway/v2/rag-search", include_in_schema=False)
@app.get("/gateway/v2/rag-health", include_in_schema=False)
def retired_api(request: Request, agent: dict[str, Any] = Depends(current_agent)) -> None:
    retire_api(request, agent)


@app.get("/gateway/v3/capabilities")
def gateway_capabilities(agent: dict[str, Any] = Depends(current_agent)) -> dict[str, Any]:
    return capabilities()


@app.get("/gateway/v3/working-agreement")
def working_agreement(
    request: Request,
    agent: dict[str, Any] = Depends(current_agent),
    _ready: None = Depends(require_vault_ready),
) -> dict[str, Any]:
    settings = Settings.from_env()
    try:
        result = load_working_agreement(settings, agent)
        audit(
            settings.db_path,
            action="working-agreement",
            route=request.url.path,
            status="ok",
            agent_id=agent["agent_id"],
            target_path=result["path"],
            **request_meta(request),
        )
        return result
    except PermissionError as exc:
        audit(
            settings.db_path,
            action="working-agreement",
            route=request.url.path,
            status="denied",
            agent_id=agent["agent_id"],
            error_message=str(exc),
            **request_meta(request),
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        audit(
            settings.db_path,
            action="working-agreement",
            route=request.url.path,
            status="error",
            agent_id=agent["agent_id"],
            error_message=str(exc),
            **request_meta(request),
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/gateway/v3/capture")
def capture(
    payload: CapturePayload,
    request: Request,
    agent: dict[str, Any] = Depends(current_agent),
    _ready: None = Depends(require_vault_ready),
) -> dict[str, Any]:
    settings = Settings.from_env()
    capture_kind = payload.kind
    try:
        values = payload.model_dump(exclude={"kind"})
        if isinstance(payload, ConversationCapture):
            values["capture_kind"] = "conversation"
            result = save_conversation(settings, agent, values)
        else:
            result = save_agent_note(settings, agent, values)
        audit(
            settings.db_path,
            action=f"capture-{capture_kind}",
            route=request.url.path,
            status="ok",
            agent_id=agent["agent_id"],
            target_path=result["path"],
            **request_meta(request),
        )
        return result
    except PermissionError as exc:
        audit(
            settings.db_path,
            action=f"capture-{capture_kind}",
            route=request.url.path,
            status="denied",
            agent_id=agent["agent_id"],
            error_message=str(exc),
            **request_meta(request),
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, FileExistsError) as exc:
        audit(
            settings.db_path,
            action=f"capture-{capture_kind}",
            route=request.url.path,
            status="error",
            agent_id=agent["agent_id"],
            error_message=str(exc),
            **request_meta(request),
        )
        raise HTTPException(
            status_code=409 if isinstance(exc, ConversationConflictError) else 400,
            detail=str(exc),
        ) from exc


@app.post("/gateway/v3/search")
def vault_search(
    payload: VaultSearch,
    request: Request,
    agent: dict[str, Any] = Depends(current_agent),
    _ready: None = Depends(require_vault_ready),
) -> dict[str, Any]:
    settings = Settings.from_env()
    bundle = {
        "all": "auto",
        "current": "current",
        "evidence": "evidence",
        "history": "history",
        "conflicts": "conflicts",
    }[payload.view]
    try:
        result = search_vault(
            settings,
            agent,
            payload.query,
            payload.limit,
            payload.refresh,
            bundle,
            payload.context,
        )
        audit(
            settings.db_path,
            action="search",
            route=request.url.path,
            status="ok",
            agent_id=agent["agent_id"],
            **request_meta(request),
        )
        return search_response(result, payload.view)
    except PermissionError as exc:
        audit(
            settings.db_path,
            action="search",
            route=request.url.path,
            status="denied",
            agent_id=agent["agent_id"],
            error_message=str(exc),
            **request_meta(request),
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        audit(
            settings.db_path,
            action="search",
            route=request.url.path,
            status="error",
            agent_id=agent["agent_id"],
            error_message=str(exc),
            **request_meta(request),
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/gateway/v3/health")
def gateway_health(
    request: Request,
    agent: dict[str, Any] = Depends(current_agent),
    _ready: None = Depends(require_vault_ready),
) -> dict[str, Any]:
    settings = Settings.from_env()
    try:
        result = wiki_health(settings, agent)
        audit(
            settings.db_path,
            action="health",
            route=request.url.path,
            status="ok",
            agent_id=agent["agent_id"],
            **request_meta(request),
        )
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
