from __future__ import annotations

import hmac
import html
import logging
import os
import re
import time
from hashlib import sha256
from threading import Event, Thread
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

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
ADMIN_NOTICES = {
    "disabled": "Agent token disabled. Requests using it are now rejected.",
    "logged-out": "You have been logged out.",
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
    password = admin_password()
    cookie = request.cookies.get(ADMIN_COOKIE)
    return cookie if password and valid_admin_cookie(cookie, password) else None


def admin_ok(request: Request) -> bool:
    return admin_session(request) is not None


def admin_csrf_token(request: Request) -> str:
    password = admin_password()
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
    return {"httponly": True, "samesite": "lax", "secure": request.url.scheme == "https"}


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


def html_page(title: str, body: str, status_code: int = 200, csrf: str | None = None) -> HTMLResponse:
    logout = (
        f"""<form method="post" action="/admin/logout">
        <input type="hidden" name="{CSRF_FIELD}" value="{html.escape(csrf)}">
        <button type="submit" class="quiet">Log out</button>
      </form>"""
        if csrf
        else ""
    )
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
    {logout}
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
    return html_page(title, body, status_code, csrf=admin_csrf_token(request) or None)


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


def token_form_html(agents: list[dict[str, Any]], csrf: str = "", notice: str | None = None) -> str:
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
<section aria-labelledby="rag-heading">
  <h2 id="rag-heading">RAG index</h2>
  <p>Updates the search index from the vault now and waits for it to finish, so it can take a while. Keep this page open; the result shows the counts from the finished run.</p>
  <form method="post" action="/admin/rag/rebuild">
    {hidden_inputs_html(csrf)}
    <button type="submit">Update RAG index</button>
  </form>
</section>"""


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


def rag_failed_html(message: str) -> str:
    return f"""<h1>RAG index update failed</h1>
<p class="error" role="alert">{html.escape(message)}</p>
<p>The index may be partly updated, so search results can be incomplete until an update succeeds. Check that Qdrant and the embedding provider are reachable and that the vault is not changing, then update again. If another update is already running, wait for it to finish. The Gateway log has details.</p>
<p><a href="/admin/tokens">Back to tokens</a></p>"""


def error_page_html(heading: str, message: str) -> str:
    return f'<h1>{html.escape(heading)}</h1>\n<p class="error" role="alert">{html.escape(message)}</p>\n<p><a href="/admin/tokens">Back to tokens</a></p>'


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
    settings = Settings.from_env()
    settings.vault_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)
    embedding_retry_stop.clear()
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
        try:
            retry_due_embeddings(settings)
        except Exception as exc:  # BaseException (shutdown, interrupts) still ends the thread
            logger.error(
                "Automatic embedding retry failed (%s): %s", type(exc).__name__, sanitize_error(exc, settings)
            )


@app.on_event("shutdown")
def shutdown() -> None:
    embedding_retry_stop.set()
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


@app.get("/admin/login")
def admin_login_page(request: Request, notice: str | None = None):
    if admin_ok(request):
        return RedirectResponse("/admin/tokens", status_code=303)
    if not admin_password():
        return html_page("Admin disabled", admin_disabled_html(), 503)
    return html_page("Admin login", login_form_html(notice=notice))


@app.post("/admin/login")
async def admin_login(request: Request):
    password = admin_password()
    if not password:
        return html_page("Admin disabled", admin_disabled_html(), 503)

    settings = admin_settings()
    form = await form_values(request)
    if not hmac.compare_digest(secret_bytes(form.get("password", "")), secret_bytes(password)):
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
    response = RedirectResponse("/admin/tokens", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE,
        make_admin_cookie(password),
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
    settings = admin_settings()
    return admin_page(
        request, "Agent tokens", token_form_html(list_agents(settings.db_path), admin_csrf_token(request), notice)
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

    audit(settings.db_path, action="admin-rag-rebuild", route=request.url.path, status="ok", **request_meta(request))
    return admin_page(request, "RAG index updated", rag_rebuilt_html(result))


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
