# Contributing

**English** | [한국어](CONTRIBUTING.ko.md)

This is a personal, self-hosted beta project. This document is the single guide for development and testing.
For the first install see [docs/setup.md](docs/setup.md), for hosting [docs/hosting.md](docs/hosting.md), for operations
[docs/operations.md](docs/operations.md), and for vulnerabilities
[SECURITY.md](SECURITY.md).

## Environment

- Python 3.13, Node 22, uv `0.12.17` (CI and Docker pin the same version)
- Install dependencies exactly as locked in `uv.lock`: `uv sync --frozen`
- The build backend is setuptools (>=77) and the license is [MIT](LICENSE).
- Compose images (Qdrant, the Gateway base `python:3.13-slim`) are pinned by multi-arch index digest.
  Updating a digest is an intentional change, and apt packages and the build backend are not pinned, so builds
  are not guaranteed to be bit-for-bit identical.

## Tests

These are the same commands as CI (`.github/workflows/ci.yml`). All of them run without real APIs or services.

```bash
uv sync --frozen
uv run --frozen python tests/test_core.py
uv run --frozen python tests/test_admin.py
uv run --frozen python tests/test_sync.py
uv run --frozen python tests/test_rag_benchmark.py
uv run --frozen python tests/test_wiki.py
uv run --frozen python tests/test_compaction.py
uv run --frozen python tests/test_bootstrap.py
uv run --frozen python tests/test_onboarding.py
uv run --frozen python tests/test_deployment_surfaces.py
uv run --frozen python tests/test_release_bundle.py
node tests/test_hook.js
uv run --frozen python tests/test_capture_integration.py
```

| Area | File |
| --- | --- |
| Core, auth, and path policy | `test_core.py` |
| Admin screens and login limiter | `test_admin.py` |
| Compose smoke helper logic (skip rules, backup and restore, test-only transport) and the native `gateway.server` first-use flow | `test_sync.py` |
| RAG search | `test_rag_benchmark.py` |
| Wiki and compaction | `test_wiki.py`, `test_compaction.py` |
| SSH deploy key and known_hosts helpers (`gateway.bootstrap`) | `test_bootstrap.py` |
| Release install bundle (including localized doc pairs) | `test_release_bundle.py` |
| Hook and capture | `test_hook.js`, `test_capture_integration.py` |
| Browser setup (`/setup` claim, deploy key, synthetic clone, the Gateway's built-in Git sync, readiness) | `test_onboarding.py` |
| Static checks of the deployment surfaces (`compose.yml`, `render.yaml`, `.railway/railway.ts`) | `test_deployment_surfaces.py` |

The Python tests above use only temporary directories and synthetic data and need no Docker daemon, network, real Git host, registry, or
cloud account (the SSH transport is replaced by a local bare repository). All of them run in CI.

- `node tests/test_client_windows.js` is for native Windows (CI `windows-latest`). If you could not
  run it on another environment, that is a **skip, not a pass.**
- Do not run tests against a personal Vault, real tokens, `.env`, or installed agent configuration. There are also no tests that call the real Cloudflare
  API.

## Compose smoke (needs Docker)

This is separate from the list above and needs the Docker CLI, the Compose v2 plugin (`docker compose`), a running Docker daemon, and a network for image
pull and build. Real Docker verification is done by the `compose-smoke` job in CI (ubuntu) with the same command.

```bash
python tests/test_compose_smoke.py
```

It layers a minimal override on the real `compose.yml` and `compose.build.yml` (source build) and runs under a unique `pvg-smoke-*` project.
Container names, the Gateway image tag, and volumes are all project-specific, and ports are loopback ephemeral.
The Gateway starts unclaimed like a first install and does not read a personal `.env` or any real secret. A test-only mounted
`sitecustomize.py` replaces the SSH transport with a synthetic bare repository, and production code has no such switch.
On exit it removes only its own project's containers, volumes, image tag, and temporary files. It runs two projects in turn.

- `keyword`: the default deployment (`EMBEDDING_PROVIDER=none`, no Cloudflare values, no Qdrant). pending (`/healthz` 200, `/readyz` 503) →
  claim (a wrong code is rejected) → synthetic clone → search and capture → push and pull → after recreating the container, login, tokens, and the Vault persist (`/setup` is
  closed) → copy the stopped `/data` volume to a new volume and verify the backup restores.
- `semantic`: the `semantic` profile with the real pinned Qdrant and the `hash` provider. Before indexing `rag_indexed` is not true, after indexing
  `/readyz` is 200, and authenticated search and capture work.

- If any of the following applies, it exits 2 (`SKIPPED`), which is a **skip, not a pass.** No Docker CLI,
  `docker compose version` fails (no Compose v2 plugin), or `docker info` fails in a full run (no
  daemon). `--prepare-only` also needs the Docker CLI and Compose v2 (it runs `docker compose config`), and it goes only as far as creating the
  temporary files and checking them, without a daemon.
- Railway and Render configs get only static checks (`test_deployment_surfaces.py` and the Railway typecheck in CI: `npm ci --ignore-scripts && npm run typecheck`
  in `.railway`). Do not deploy on a real account or test on a paid platform. If you could not run the typecheck for lack of tooling, that is a skip, not a pass.
- `test_sync.py` verifies these skip conditions with mocks and never calls real Docker.
- Limits: the real SSH and GitHub deploy key path, Cloudflare embedding, semantic search quality, and hosting platform behavior are not verified.
  CI is a single amd64 environment, so arm64 execution is not confirmed, and the atomicity of Git sync is not verified either.

## Running locally (isolated, synthetic data)

Start it only with temporary synthetic paths, without personal settings or APIs. The `hash` provider is for tests and does not promise real semantic search
quality, so do not judge search quality with this demo API. Do not create a default `.env` in the project.
The directly run Python runtime (`uvicorn gateway.app:app`) defaults to `cloudflare` when `EMBEDDING_PROVIDER` is unset (for compatibility with existing integrations),
which differs from `gateway.server` and the Compose default `none`. So always set it explicitly, as below.

```bash
tmp="$(mktemp -d)" && mkdir "$tmp/vault"
VAULT_DIR="$tmp/vault" DB_PATH="$tmp/gateway.db" ADMIN_PASSWORD="$(openssl rand -hex 16)" \
  EMBEDDING_PROVIDER=hash uv run --frozen uvicorn gateway.app:app --host 127.0.0.1 --port 8000
```

Check with `curl http://127.0.0.1:8000/healthz`, and delete `$tmp` when you are done.

## Pre-release secret scan (optional)

Install [gitleaks](https://github.com/gitleaks/gitleaks), then scan the whole Git history with the `.gitleaks.toml` at the repository root
(default rules plus custom pvg rules).

```bash
gitleaks git . --log-opts="--branches --remotes --tags --full-history" --redact=100 --ignore-gitleaks-allow
```

- Do not use a working-directory scan (`dir .`). It includes secret files such as a local `.env`.
- Do not print real credential values in the output or paste them into issues or PRs, and do not put real credentials
  in the allowlist. Revoke an exposed credential first.

## Plugin structure principles

One shared Node client (`pvg-client.js`) and one `persona-vault` skill are used by both Codex and Claude.
Only the host manifests and the per-OS launchers differ by surface. Keep command names and flags the same on every OS.

Language: skills and references, agent-facing templates, source comments and docstrings, and user-facing UI/CLI text are authored in English. This does not require Vault content to be in English, and it does not replace the localized README/docs or multilingual test fixtures.

## Branches and commits

- Create a feature branch from `develop` and open a PR. Do not commit directly to `main`.
- Split commits into one coherent unit of change each.

## Versions

The Gateway package, plugin, and API versions are independent. Currently the Gateway package is `0.1.0`, the plugin is `0.7.3`
(the Codex one includes a build suffix), and the API is `v3`. Do not bump versions in ordinary changes.

## Docs and known limitations

- Installation is `docs/setup.md`, hosting is `docs/hosting.md`, operations is `docs/operations.md`, and the Wiki curation protocol is `docs/CURATOR.md`.
  `docs/operations.md` must be included in the release bundle (`scripts/build_release_bundle.py` ALLOWLIST).
- Docs are paired in English (the default `.md`) and Korean (`.ko.md`). Update paired documents together in the same change. A document that
  ships in the release bundle must also have its `.ko.md` in the ALLOWLIST. Agent-facing templates (`docs/CURATOR.md`, `docs/WORKING_AGREEMENT.md`, and so on) remain
  canonical in English, and the Korean document is a translation for human readers. Paths the runtime reads, template download URLs, and the canonical `CURATOR.md`
  filename stay in English.
- The default deployment uses keyword search. Semantic search is used only when you turn it on explicitly (`EMBEDDING_PROVIDER=cloudflare` +
  `COMPOSE_PROFILES=semantic`), and API `v3` is not a contract that guarantees semantic search accuracy.
- Known QA gaps: the Windows launcher passed PowerShell 5.1/7 regression checks in CI, but real desktop and agent host integration and the full pre-release QA are not all finished, and the security review is not finished either.
- Without a Docker daemon the Compose smoke cannot run, and that result is a skip, not a pass. Do not claim native Windows or arm64 execution is confirmed until you have run it in that environment yourself.
- When you change dependencies, update `uv.lock` too and check `uv lock --check` and `uv sync --frozen`.

## Release (maintainer)

- Run the manual `Release` workflow (`workflow_dispatch`) on the default branch with an existing tag `gateway-vX.Y.Z`.
  The tag version must equal the Gateway package version in `pyproject.toml`.
- Only when the full CI (tests, Compose smoke, Windows client) passes does it build the image to GHCR and create the install bundle
  `persona-vault-gateway-X.Y.Z-install.tar.gz`, the standalone `compose.yml`, and each `.sha256` as a **draft** release.
  The image references in the bundle and in `compose.yml`, `render.yaml`, and `.railway/railway.ts` are replaced with the exact digest.
- Before you publish the draft, make the GHCR package public and verify an anonymous pull. Leave the global Docker login
  alone and use a temporary config directory.

  ```bash
  DOCKER_CONFIG="$(mktemp -d)" docker pull ghcr.io/OWNER/REPO@sha256:<digest>
  ```

- The image digest is in the draft's release notes.
- Publish the public Release and the public GHCR package only after the QA above (CI, anonymous pull check) is done. Keep the Render and Railway configs
  undeployed.
