# PersonaVault Gateway Operations

**English** | [한국어](operations.ko.md)

For the first install see [setup.md](setup.md), and for hosting options see [hosting.md](hosting.md). Run `docker compose` commands in the directory
that holds `compose.yml`. Run the Vault layout commands in a local Vault checkout and the Curator commands in a source checkout.

- [Vault layout](#vault-layout)
- [Semantic search](#semantic-search)
- [Remote access](#remote-access)
- [Hooks and the Curator](#hooks-and-the-curator)
- [Troubleshooting](#troubleshooting)
- [Upgrade](#upgrade)
- [Backup and restore](#backup-and-restore)

## Vault layout

The Vault is a Git repository of Markdown files. Follow [metadata.md](metadata.md) for the format and
[CURATOR.md](CURATOR.md) for the curation procedure.

| Directory | Purpose | Edited by |
| --- | --- | --- |
| `00_Inbox/` | Unsorted notes | Person |
| `10_User/` | Collaboration rules (`WORKING_AGREEMENT.md`), user records | Person, approved Curator |
| `20_Projects/` | Project goals and decisions | Person, approved Curator |
| `30_Conversations/raw/` | Raw agent conversations (by date) | Agent (the only path the Gateway writes) |
| `30_Conversations/summaries/` | Conversation summaries | Person, Curator |
| `50_Knowledge/` | Verified, reusable knowledge | Person, Curator |
| `90_Private/` | Personal notes | Person |

- `.git/`, `.obsidian/`, and `.tmp/` are excluded from indexing and search. Because sync commits changes, add `.obsidian/` and
  `.tmp/` to `.gitignore` and push that before you connect the Gateway. If they are already tracked, run `git rm -r --cached --ignore-unmatch -- .obsidian .tmp`
  and commit (they remain in past history).
- Edit in a local clone and run `git pull --rebase` before you push. Do not edit `30_Conversations/raw/` directly.
- `90_Private/` is also readable with a read token, and it is sent to Cloudflare if you turn on semantic search.
- Download the collaboration rules template only if the file does not exist yet.

```bash
mkdir -p 10_User
test -e 10_User/WORKING_AGREEMENT.md || curl -fsSLo 10_User/WORKING_AGREEMENT.md \
  https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/docs/WORKING_AGREEMENT.md
```

The Gateway clones the Vault on the server and periodically runs commit, `pull --rebase`, and push (default 300 seconds,
`VAULT_SYNC_INTERVAL_SECONDS`). The supported Git host is a GitHub SSH URL.

## Semantic search

The default is keyword search (`EMBEDDING_PROVIDER=none`). To turn semantic search on, set **all** of the following and restart.

```text
EMBEDDING_PROVIDER=cloudflare
CLOUDFLARE_ACCOUNT_ID=<account-id>
CLOUDFLARE_API_TOKEN=<workers-ai-token>
```

- **Compose**: put the values above and `COMPOSE_PROFILES=semantic` in the shell environment or in a `.env` next to `compose.yml`, then run `docker compose up -d`
  again. The `semantic` profile starts the pinned Qdrant, and `QDRANT_URL` is fixed to `http://qdrant:6333`
  (`QDRANT_COLLECTION` defaults to `persona_vault`).
- **Railway and Render**: the supplied configs have no Qdrant. Along with the values above, add `QDRANT_URL=<a Qdrant address the Gateway can reach>`
  (and `QDRANT_COLLECTION` if needed) as service environment variables yourself, and run Qdrant yourself. Make it reachable only over a private path.

- Token: Cloudflare dashboard `Workers AI` → `Use REST API` → `Create a Workers AI API Token`
  (`Workers AI - Read`, `Workers AI - Edit`). Store it only as a platform secret or in `.env`.
- Indexed chunks (including `90_Private/`) and search queries are sent to Cloudflare Workers AI.
  See the [data policy](https://developers.cloudflare.com/workers-ai/platform/data-usage/).
- After starting, run `Update RAG index` in admin once and check that `rag_indexed` in `/readyz` is `true`.
  After that only changed chunks are embedded. If it fails, run it again; two updates never run at the same time.
- If there is no index, it is stale, or Qdrant is unavailable, search answers with keyword (check `index.stale`).
  If the Cloudflare limit is exceeded, it retries automatically after the reset.
- The model is `@cf/qwen/qwen3-embedding-0.6b` (1024 dimensions). If the provider, model, or dimension changes, everything is reindexed.
- With `EMBEDDING_PROVIDER=none`, `Update RAG index` does nothing and leaves existing vectors untouched.

## Remote access

- The container listens on `PORT` (default `8000`), and Compose publishes it on the host at `GATEWAY_BIND_ADDR` (default loopback `127.0.0.1`), port
  `GATEWAY_HOST_PORT` (default `18080`). Before you change the address, prepare an encrypted private path or a TLS endpoint.
  Plain public HTTP exposes tokens, the admin password, and conversations. Keep Qdrant private and do not expose its port.
  If you host on public HTTPS, `/setup` and `/admin` are exposed too (before the claim the setup code protects them, after it the admin password, session, CSRF, and login limit).
  Finish the claim promptly and do not share the code. For a private deployment, restrict admin with network and access controls where possible.
  This repository does not provide a proxy, certificates, or DDNS.
- If the HTTPS-terminating front end can be known by exact IP, put that IP in the environment variable (comma-separated for several).

  ```text
  FORWARDED_ALLOW_IPS=203.0.113.10
  ```

  Do not use `*`. `X-Forwarded-*` from addresses not on the trusted list is ignored, and in that case the Secure cookie decision and the
  login limit are based on the connection address. All users behind the front end are counted as the one front-end address.
- On a platform (Railway, Render, and so on) where the front-end address cannot be trusted or is unknown and you connect only over HTTPS, `PVG_SECURE_COOKIES=true` forces Secure on the admin
  cookie. It does not trust forwarded headers, so the login limit is based on the connection address. Over plain HTTP the
  Secure cookie is not sent and you cannot log in, so use it only when access is HTTPS-only.
- Admin login allows 5 attempts per 5 minutes per client address. Beyond that it returns `429` with `Retry-After`.
  It lives only in process memory, resets on restart, and is not shared, so it does not replace network
  access control. The CSRF token and same-origin check remain in place.
- Admin's mutating forms require a CSRF token, and creating, rotating, or disabling an existing agent ID goes through one more confirmation screen.

## Hooks and the Curator

- The hook sends your requests, final replies, and subagent delegations and results. Tool output and reasoning are excluded.
  A new record is stored in the local `spool/v2/<session-hash>.jsonl` (plaintext) and sent at Stop.
- Before sending, it masks token and credential patterns and omits code, config, diff, and log blocks, but this is heuristic.
  Do not paste secrets into conversations. See [SECURITY.md](../SECURITY.md) for the exact scope.
- If the Gateway does not advertise `conversation-merge-v1`, the spool is kept and sending stops. Update the Gateway first, then
  update the plugin on each PC.
- To turn capture off, disable the PersonaVault hook in `/hooks` on that platform. A Curator session started from a
  Vault root that has the canonical `CURATOR.md` is not captured.
- SessionStart reads `10_User/WORKING_AGREEMENT.md` with the read token and passes it to the session.
- Manual memo: pass the body on stdin to `pvg-agent-memo --project <name> --kind procedure --outcome success "<title>"`.
  For Windows syntax see the
  [Windows guide](https://github.com/mykim0409/persona-vault-gateway/blob/main/plugins/persona-vault/skills/persona-vault/references/windows.md).

The Curator (`pvg-wiki`) only proposes plans, and a person approves, applies, commits, and pushes. Follow
[CURATOR.md](CURATOR.md) for the full procedure. Run the CLI from a source checkout with `uv sync --frozen`, then `uv run pvg-wiki ...`.

The CLI and directly run Python default to `cloudflare` when `EMBEDDING_PROVIDER` is unset (the Gateway service default is `none`).
Against a keyword-only Gateway, set `EMBEDDING_PROVIDER=none` explicitly for `compact-finish`. Against a semantic Gateway,
you need the same Vault, DB, and Qdrant settings as the Gateway and a compatible existing index.

Read-only check: `uv run --frozen python -m gateway.cli --vault /path/to/vault health` (`conflicts list` also works).

## Troubleshooting

- **`/readyz` returns 503**: normal before setup is finished (before the claim or the Vault connection). `/healthz` returns 200 whenever the app is alive.
  Continue at `/setup` or `/admin/vault`. With semantic search on, it can also be a Qdrant connection or index mismatch, so run
  `Update RAG index`.
- **Lost the setup code**: before the claim, set `PVG_SETUP_TOKEN` (20 to 200 printable ASCII characters, no spaces) and restart to use the new value.
  You may also find it in the log printed once at first start.
- **Connection error (`/admin/vault`)**: follow the fixed message on the screen. Common causes are an unregistered deploy key or a missing **Allow write access**,
  a typo in the repository URL, an empty repository with no commit (make a commit, then **Retry**), or a pinned GitHub host key mismatch.
  If a push is rejected, check write access or protected branches. The connection step does not prove write access.
- **Sync BLOCKED**: resolve the Vault's rebase, merge, or conflict state yourself and sync resumes.
- **Login 429**: try again after `Retry-After` seconds.
- The API is `/gateway/v3`. `/gateway/v1` and `/gateway/v2` return `410 client_upgrade_required`.
  SQLite is migrated automatically at startup, and an older Gateway will not open a DB with a newer schema.

## Upgrade

To move to a new Release, replace `compose.yml` in the same place after verifying its checksum and run `docker compose up -d` (the image is pinned by
digest). On a platform, change the image reference in the config to the new release yourself and deploy manually. There is no automatic redeploy.
`persona-vault-data` (`/data`) is kept as is, so settings and the Vault remain. Back up before you upgrade, as described in
[Backup and restore](#backup-and-restore) below.

## Backup and restore

1. Stop with `docker compose stop`.
2. Preserve the one `/data` volume: the Vault clone (including unpushed commits and uncommitted changes, `/data/vault`), SQLite (`/data/gateway.db`:
   token hashes, audit), and settings (`/data/setup`: the admin hash and the deploy key's private key).
3. A Qdrant volume, if you use one, is derived data that can be rebuilt from the Vault (`Update RAG index`, which incurs Cloudflare usage).

Copies contain secrets, so store them encrypted. To restore, put the volume contents back into the same setup and run `docker compose up -d`.
Without SQLite, reissue tokens. `docker compose down -v`, `docker volume rm`, and `docker system prune --volumes` are
not everyday procedures. Do not use them before you have verified a restore.
