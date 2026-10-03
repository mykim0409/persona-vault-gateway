# PersonaVault Gateway Setup

**English** | [한국어](setup.ko.md)

Put the Gateway on a server, configure it in the browser (`/setup`), then connect the plugin on each PC. The default is keyword search.
Docker, Railway, and Render all use the same image and the same setup screen. For platforms see [hosting.md](hosting.md); for Vault layout,
semantic search, remote access, upgrade, backup, and troubleshooting see [operations.md](operations.md).

You need: Docker Compose v2 on the server, Node.js on each PC, and a GitHub account.
On Linux, put `sudo` before `docker` if you need elevated permissions.

## 0. Vault repository

Prepare a **private** GitHub repository and make at least one commit (for example, add a README). An empty repository cannot be connected.

## 1. Start the Gateway

For a new install, use a dedicated, permanent directory (the Compose project and volume identity depend on it, so keep it for upgrades).

```bash
curl -fsSLO https://github.com/mykim0409/persona-vault-gateway/releases/latest/download/compose.yml
curl -fsSLO https://github.com/mykim0409/persona-vault-gateway/releases/latest/download/compose.yml.sha256
sha256sum -c compose.yml.sha256        # macOS: shasum -a 256 -c
docker compose up -d
docker compose logs                    # one-time setup code
```

No `.env` or key file is needed. All data (the Vault clone, SQLite, and settings) is stored in one volume,
`persona-vault-data` (`/data` in the container). If you set `PVG_SETUP_TOKEN` (20 to 200 characters) yourself, that value is the setup code;
otherwise it is printed once in the log.
To run from source, clone the repository and run `docker compose -f compose.yml -f compose.build.yml up -d --build`.

## 2. Configure in the browser

Open `http://127.0.0.1:18080/setup`. Until setup is finished, only `/healthz` returns 200, `/readyz` returns 503, and search and capture are off.

1. Enter the setup code and an admin password (16 to 128 characters), then **Claim this Gateway**.
2. Enter `git@github.com:OWNER/REPO.git` in **Repository SSH URL** and **Generate deploy key**.
3. Register the displayed **public** key under the repository's Settings → Deploy keys and turn on **Allow write access**.
4. **Connect and clone** (**Retry** if it fails). When it finishes, the Vault is connected and `/readyz` returns 200.
5. In Agent tokens (`/admin/tokens`), issue one token per PC. A token is shown only once. A normal plugin uses `Read + Write`; search-only uses `Read`.

The connection step does not prove write access. Without it, later sync pushes are rejected and shown on the Vault sync screen.
Do not paste the setup code into chats.

Compose publishes the port only on the server's `127.0.0.1` (the native server listens on `0.0.0.0` and hosted services are public HTTPS, so neither is loopback).
From other PCs, use an address reachable over an encrypted path (a private network or a TLS endpoint) as the plugin's Gateway URL. Exposing plain HTTP
publicly exposes tokens and the admin password.
See [operations.md](operations.md#remote-access).

Note: a read token reads the whole Vault including `90_Private/`, and automatic capture stores conversation content as plaintext in the Gateway and
in Git. Read [SECURITY.md](../SECURITY.md).

## 3. Set up the agent plugin

Use a separate agent id and token for each PC. Node.js is required.

Claude Code:

```text
/plugin marketplace add mykim0409/persona-vault-gateway
/plugin install persona-vault@persona-vault-gateway
/reload-plugins
```

Codex:

```bash
codex plugin marketplace add mykim0409/persona-vault-gateway
codex plugin add persona-vault --marketplace persona-vault-gateway
```

Check the PersonaVault hook commands in `/hooks`, then trust them.

Installing the token helper prompts for the Gateway URL and token. Never paste a token into an agent chat.

<details>
<summary>macOS / Linux</summary>

```bash
base=https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts
d="$(mktemp -d)" && curl -fsSLo "$d/install-agent-config.sh" "$base/install-agent-config.sh" \
  && curl -fsSLo "$d/pvg-client.js" "$base/pvg-client.js" \
  && sh "$d/install-agent-config.sh"
```

To replace a token, run the same installer again with `--replace-token`.
</details>

<details>
<summary>Windows PowerShell</summary>

```powershell
$base = 'https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts'
$d = Join-Path $env:TEMP 'persona-vault-installer'
New-Item -ItemType Directory -Force -Path $d | Out-Null
foreach ($f in 'install-agent-config.ps1', 'pvg-client.js') { Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $d $f) "$base/$f" }
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $d 'install-agent-config.ps1')
```

To replace a token, run `install-agent-config.ps1` again with `-ReplaceToken`.
For command syntax, follow the [Windows guide](https://github.com/mykim0409/persona-vault-gateway/blob/main/plugins/persona-vault/skills/persona-vault/references/windows.md).
</details>

Verify the install:

```bash
pvg-rag-search --view current "current operations policy"
```

On Windows: `& "$HOME\.local\bin\pvg-rag-search.ps1" --view current "current operations policy"`.
