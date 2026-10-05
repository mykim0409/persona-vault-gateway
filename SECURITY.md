# Security Policy

**English** | [한국어](SECURITY.ko.md)

PersonaVault Gateway is self-hosted beta software that you operate yourself for personal use. It is not a SaaS.
It does not claim to have completed a security audit and offers no security guarantees
(license: [MIT](LICENSE), no warranty).

## Data boundaries to know

- **Vault read scope**: an agent token with read permission can read every Markdown file except the technical
  directories excluded from indexing (`.git/`, `.obsidian/`, `.tmp/`). That includes `90_Private/`.
  The security boundary applies only to the paths where writes are allowed.
- **Tokens**: a raw token is shown once at issuance and only its hash is stored in the DB. The SQLite DB itself
  is not encrypted.
- **Automatic conversation capture**: the plugin hook sends your requests, subagent results, and final replies to
  the Gateway as raw conversations. Before local storage and again when sending, the hook applies a deterministic
  filter that masks secret patterns, omits recognized code, config, diff, and log blocks, and keeps only the
  project directory name instead of the absolute cwd metadata (no extra LLM call). Paths you write in message text
  are generally not masked. This is best effort, not a DLP guarantee. Existing spool data and Git history are not
  cleaned retroactively, and direct API calls, manually written Vault files, and Cloudflare input are outside this
  filter. Do not paste secrets into conversations.
- **Cloudflare (optional)**: the default deployment is keyword-only (`EMBEDDING_PROVIDER=none`), so nothing is
  sent to Cloudflare. Only if you enable semantic search yourself (`EMBEDDING_PROVIDER=cloudflare`, credentials,
  Qdrant) are Vault document chunks (including `90_Private/`) and search queries sent to Cloudflare Workers AI.
- **Initial setup (`/setup`)**: the setup code (printed once in the log, or `PVG_SETUP_TOKEN`) is the only
  authentication before the first claim. Do not paste it into chats, issues, or shared logs. Once claimed, the
  generated code file is deleted and `/setup` no longer opens, and claim attempts are rate limited per client
  address. The admin password is stored only as a salted hash. The private deploy key lives only in a private
  file under `/data/setup`, and the screen shows only the public key. If you host on public HTTPS, `/setup` and
  `/admin` are exposed by the same service. Before the claim they are protected by the setup code that only the
  operator knows, and after it by the admin password, session, CSRF, and login limit. So finish the claim
  promptly, do not share the code, and treat `PVG_SETUP_TOKEN` as a platform secret. The supplied Railway and
  Render configs do not isolate admin. The connection step does not prove write access, and a rejected push
  shows up in the sync state.
- **Local spool**: the hook's retry spool stays on each PC as plaintext JSONL.

## Operating assumptions

- Binding depends on how you deploy. Compose publishes the port only on the host loopback (`127.0.0.1`) by
  default (`GATEWAY_BIND_ADDR`). The native server (`python -m gateway.server`) listens on `0.0.0.0`, and hosted
  services are public HTTPS that also expose `/setup` and `/admin`, so neither is loopback. Encrypting remote
  connections is the operator's responsibility. Make the Gateway IP and port reachable only through an encrypted
  private path (VPN, tunnel, and so on) or a TLS endpoint you already operate. No particular proxy is required,
  and this repository does not provide a proxy, certificate or ACME automation, or server and account
  provisioning. Exposing it over plain public HTTP is not safe, because agent tokens, the admin password, and
  captured conversations travel unencrypted.
- Keep Qdrant private and do not expose its port. For a private deployment, restrict admin with network and access
  controls where possible. Admin login is limited to 5 attempts per 5 minutes per client address and returns `429`
  with `Retry-After` beyond that. The limit lives only in process memory with a bounded tracking size, resets on
  restart, and is not shared across processes or replicas, so it does not replace network security. The existing
  CSRF protection is unchanged. Forwarded headers are honored only from proxies you configured as trusted by
  exact IP; never trust with a wildcard (`*`).
- The source and Git history are the reference. The running DB, Vault, and Qdrant state must be backed up and
  managed separately. A `/data` backup contains the deploy key's private key, the admin hash, unpushed Vault Git
  content, and SQLite, so store it encrypted.
- Do not use example passwords. Choose the admin password yourself in `/setup`, and if you enable semantic
  search, issue the Cloudflare token yourself ([docs/setup.md](docs/setup.md)). Only the public key is
  registered; never take the private key off the server.

## Remaining beta risks

- The security audit is not finished. The browser setup path is new code, and if the code is exposed before the
  claim, a third party could claim first.
- The admin login limit is based on process memory, and the single-process Gateway cannot scale out to replicas.
- SQLite and the deploy key sit on the same volume, so volume access is Vault write access.
- Signature and provenance verification for release assets and images is not provided (only SHA-256 checksums and
  image digests). The Railway and Render configs have not been deployed or verified on a live account.
- On platforms that do not know the HTTPS front-end address, `PVG_SECURE_COOKIES=true` only forces Secure cookies.
  Trust forwarded headers (`FORWARDED_ALLOW_IPS`) only by exact IP and do not use `*`.
- Behind a platform proxy (Render, Railway), `PVG_TRUSTED_PROXY_HOPS=1` keys the login and setup limiter on the client
  address that proxy appended to `X-Forwarded-For`; without it everyone shares one bucket. Set it to the real number of
  proxies and no higher, otherwise a client-supplied entry can pick its own bucket. It affects only that limiter.

## Reporting a vulnerability

If the repository's Security tab shows a private reporting button, use it to report.

> **Status: needs confirmation.** The repository is public, but it has not yet been confirmed that GitHub private
> vulnerability reporting is enabled. This document does not claim the feature is on. A maintainer must enable it
> under the repository's Settings → Security (Code security) and confirm that the private reporting button
> appears on the Security tab. How:
> [Configuring private vulnerability reporting for a repository](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository).

- Do not post secrets, tokens, exploit code, or reproduction details in public issues, PRs, or discussions.
- If no private path is visible, open only a minimal public issue that asks for contact, without secrets or details.
- No response time or fix deadline is promised. This is a personal project and is handled on a best-effort basis.
