<p align="center">
  <img src="docs/assets/persona-vault-icon.svg" width="64" height="64" alt="PersonaVault icon: a cairn of three stacked sediment stones in amber, terracotta and garnet, with two loose grains of raw evidence above">
</p>

# PersonaVault Gateway

**Your agents change. Your knowledge stays.**

PersonaVault is a user-owned knowledge base, independent of any one agent or environment.
Connected AI agents across your PCs search and contribute to the same Markdown Vault.
Your accumulated context stays when tools change, and you review and approve what becomes knowledge.
Use Obsidian, VS Code, or any Markdown editor.

**English** | [한국어](README.ko.md)

[Get started](#get-started) · [How it works](#how-it-works) · [Data and control](#data-and-control)

![PersonaVault concept illustration: "Agents come and go. Your knowledge settles." Agents, devices and tools drop loose grains of raw evidence above a "you review" line. Below it, a cairn of warm-colored sediment stones (decisions, context, preferences, lessons) holds the knowledge you chose to keep in plain Markdown and Git, which connected agents can retrieve.](docs/assets/persona-vault-overview.svg)

> **Self-hosted beta.** Built for personal self-hosting. The security audit and pre-release QA are
> not finished, so read [Data and control](#data-and-control) and [SECURITY.md](SECURITY.md) first.

## Why PersonaVault

- **One knowledge base across agents.** Connected agents on any of your PCs search the same
  earlier sessions and notes, so switching agent or device does not mean explaining the project
  again.
- **Recover why a decision was made.** The `evidence` view returns the raw conversation behind a
  decision, and `history` and `conflicts` show how it changed.
- **Turn raw chat into approved knowledge.** Sessions land as raw Markdown, and you decide what
  becomes knowledge. Nothing is promoted automatically.
- **Own plain Markdown.** Your Vault is a private Git repository. The metadata and file layout are
  the contract, so there is no editor or agent lock-in.

## What it looks like

Illustrative requests to a connected agent:

```text
"Why did we change the retry policy last month? Check the evidence."
"What is our current token rotation procedure?"
"Save what we learned from this debugging session as a procedure note."
```

## Get started

### 1. Run the Gateway once (server)

You need Docker Compose (or a platform, see [docs/hosting.md](docs/hosting.md)) and a private GitHub
repository for the Vault (with at least one commit). The default deployment uses keyword search. To build
from source instead, run `docker compose -f compose.yml -f compose.build.yml up -d --build`.

Run the Docker commands on the **server**. Use a dedicated, permanent directory for a new install (the Compose project and volume identity depend on it, so keep it for upgrades).

```bash
curl -fsSLO https://github.com/mykim0409/persona-vault-gateway/releases/latest/download/compose.yml
curl -fsSLO https://github.com/mykim0409/persona-vault-gateway/releases/latest/download/compose.yml.sha256
sha256sum -c compose.yml.sha256            # macOS: shasum -a 256 -c
docker compose up -d
docker compose logs                        # one-time setup code
```

By default Compose binds the port only to the server's `127.0.0.1`. Choose the Gateway URL that matches how you reach it:

| How you reach it | Gateway URL |
| --- | --- |
| Hosted service or an existing HTTPS hostname | That HTTPS URL, for example `https://vault.example.com` |
| Your own domain or DDNS hostname with an HTTPS reverse proxy you configured | `https://vault.example.com`, see [docs/hosting.md](docs/hosting.md#domain-or-ddns-access) |
| The server is this PC | `http://127.0.0.1:18080` |
| No HTTPS address (optional) | [SSH tunnel](docs/setup.md#optional-ssh-tunnel), then `http://127.0.0.1:18080` |

Open `/setup` on that base URL (for example `https://vault.example.com/setup`), and give the plugin installer the same base URL without `/setup`. Claim it with the setup code and choose an admin password, enter the
GitHub repository SSH URL, register the shown **public** deploy key on the Vault repository with write
access, then connect (retry if needed) and issue agent tokens. Until setup finishes, `/healthz` is 200 but
`/readyz` is 503 and search and capture are off. The connection step does not prove write access.
Step by step: [docs/setup.md](docs/setup.md). Docker, Railway, and Render install the same way; the Railway
and Render configs are prepared but not verified on a live account: [docs/hosting.md](docs/hosting.md).
Upgrades, backup, semantic search: [docs/operations.md](docs/operations.md).

Reach it from other PCs only over an encrypted path; plain public HTTP exposes agent tokens and the admin password.

### 2. Install the plugin (each PC)

Codex and Claude Code are the agent platforms supported today, through the shared plugin below.
[Custom GPT Actions](docs/gpt-actions.md) are an alternative path. Any other client needs its own
integration with the Gateway API; automatic capture is currently provided only by the supplied
plugin.

Node.js is required for the hooks and the token helper.

```text
# Claude Code
/plugin marketplace add mykim0409/persona-vault-gateway
/plugin install persona-vault@persona-vault-gateway
/reload-plugins
```

```bash
# Codex
codex plugin marketplace add mykim0409/persona-vault-gateway
codex plugin add persona-vault --marketplace persona-vault-gateway
```

Review the hook commands before trusting them (`/hooks`). Then install the token helper and give it
your Gateway URL and token as described in section 3 of [docs/setup.md](docs/setup.md), which also
covers Windows PowerShell. Never paste a token into an agent chat.

### 3. Try it

```bash
printf '%s\n' "For this project, record the reason for deployment decisions." \
  | "$HOME/.local/bin/pvg-agent-memo" --title "Deployment notes" --project Example
"$HOME/.local/bin/pvg-rag-search" --view evidence "deployment decisions"
```

This saves a raw note (not approved knowledge) and finds it again; `--view current` may legitimately be empty on a new Vault.
Windows PowerShell version: [docs/setup.md](docs/setup.md#3-set-up-the-agent-plugin-on-each-pc).

The plugin also ships a small MCP server with two tools, `pvg_search` and `pvg_memo`. Agents call them instead of the helper commands. They read the same token config, so the installer is still required and the helpers remain the fallback. In Claude Code they are named `mcp__plugin_persona-vault_pvg__pvg_search` and `mcp__plugin_persona-vault_pvg__pvg_memo`: allowlist only `pvg_search` if you want searches without an approval prompt, and keep `pvg_memo` prompting, because a note is saved only when you explicitly ask.

## How it works

1. **Capture.** Hooks send sessions to the Gateway, which writes them only under
   `30_Conversations/raw/`.
2. **Approve.** A person curates raw conversations into knowledge. The optional Curator
   (`pvg-wiki`) is an experimental local CLI that proposes a plan; it never applies, commits, or
   pushes on its own, and it is not a background service.
3. **Retrieve.** Keyword search, plus semantic search where it is available, returns results with
   an `answer_state`, which is a signal and not a guarantee that a statement is true.

The product core is capture, plain Markdown, and keyword retrieval. Semantic search is an optional
enhancement on top of it, and the Curator is a separate optional tool. The table shows what
implements each today.

| Layer | Current implementation |
| --- | --- |
| Knowledge store | Private Git repository of Markdown files, synced by the Gateway itself |
| Gateway | FastAPI and SQLite: API, auth, path policy, Markdown writer, admin |
| Semantic search (optional) | Qdrant, with embeddings from the Cloudflare Workers AI REST API |
| Curator (optional) | `pvg-wiki`, an experimental local CLI |

The Gateway defaults to keyword-only search (`EMBEDDING_PROVIDER=none`): no embedding provider or
Qdrant is contacted. Semantic search is an explicit option that needs `EMBEDDING_PROVIDER=cloudflare`,
the Cloudflare credentials, and a Qdrant service. The direct Python runtime still defaults to `cloudflare` when `EMBEDDING_PROVIDER` is
unset, for older integrations; set it explicitly (for example `none` for CLI `compact-finish` against
a keyword-only Gateway).

Cloudflare Workers AI is the only production embedding path today. It is called directly over REST,
so there is no separate Cloudflare Worker to develop or deploy. Other providers are not implemented
yet, and the hash embedding in the code exists for tests only. See
[docs/operations.md](docs/operations.md).

## Data and control

- **Automatic capture is not a privacy boundary.** Hooks send your requests, the main agent's final
  replies, and subagent delegations and results. Standalone tool and reasoning blocks are excluded,
  but your messages and final replies are collected. Before a new record is stored locally and
  before every send, the hook runs a deterministic local filter (no extra LLM call, no new
  dependency): it masks common credentials (key/value and JSON fields, Bearer/Basic headers, URL
  passwords, private keys, common token formats), replaces recognized fenced code, config, env,
  diff, and log blocks (and obvious unfenced diff/env/log dumps) with a content-free
  `[omitted <kind> block, N lines]` marker, and keeps only the project directory name instead of
  the absolute working-directory (cwd) metadata (paths you mention in message text are not generally
  scrubbed). This is heuristic, not a DLP guarantee: code in prose or unrecognized formats
  can remain, and old spool data is not cleaned retroactively (pending legacy records are filtered
  only on the wire; the local file keeps its original text). It covers the hooks only, not direct
  API calls, manually written Vault files, or Cloudflare embedding input. Do not paste
  credentials, private keys, confidential source code, or other sensitive data into captured chats.
- **Captured text is stored in plaintext and kept in Git.** The local spool is plaintext JSONL and
  its contents are sent to the Gateway. The Vault is synced through Git, so removing a file later
  does not erase its history.
- **A read token reads the whole Vault.** That means every Markdown file except `.git/`,
  `.obsidian/`, and `.tmp/`, including `90_Private/`. Writes are limited to
  `30_Conversations/raw/`.
- **With semantic search on, text leaves your server.** Only if you enable it, indexed chunks
  (including `90_Private/`) and search queries are sent to Cloudflare Workers AI for embedding. See
  the [Cloudflare data policy](https://developers.cloudflare.com/workers-ai/platform/data-usage/).
- **The admin login limiter is not network security.** It allows 5 attempts per 5 minutes per client
  address and then returns `429` with `Retry-After`. It is process-local and bounded, resets on
  restart, and is not shared across processes. Forwarded headers are honored only from exactly
  trusted proxies (never a wildcard); CSRF protection is unchanged. Keep the admin UI private behind
  an encrypted path or TLS and access control.
- **Curation needs a person.** Every plan is approved by a human, and raw deletion is not guaranteed
  to be atomic. See the [Curator protocol](docs/CURATOR.md).

This beta has not completed a security audit. See [SECURITY.md](SECURITY.md).

## Documentation

| Guide | Covers |
| --- | --- |
| [docs/setup.md](docs/setup.md) | First install, plugins, token helper |
| [docs/hosting.md](docs/hosting.md) | Hosting options: Compose, Railway, Render, others |
| [docs/operations.md](docs/operations.md) | Vault layout, semantic search, remote access, upgrade, backup |
| [Windows guide](plugins/persona-vault/skills/persona-vault/references/windows.md) | Windows command syntax |
| [docs/gpt-actions.md](docs/gpt-actions.md) | Custom GPT Actions as an alternative to the plugin |
| [docs/metadata.md](docs/metadata.md) | Markdown metadata contract |
| [docs/CURATOR.md](docs/CURATOR.md) | Curator protocol |
| [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) | Development and security policy |

Licensed under the [MIT License](LICENSE).
