# Hosting

**English** | [한국어](hosting.ko.md)

The Gateway installs the same way wherever you host it: the same image, the same start command (`python -m gateway.server`), and the same browser setup screen (`/setup`),
with no per-platform runtime fork. The setup procedure is in [setup.md](setup.md).

> The Railway and Render config files have **not been deployed or verified.** They have only had static checks and no account was used. Both
> are paid resources, and there is no one-click deploy button or template ID. You verify the account, cost, SSL, proxy routing, and whether the
> platform can reach the Vault repository over SSH yourself.

## Common contract

- The port is `PORT` (default `8000`), and search is keyword (`EMBEDDING_PROVIDER=none`).
- Attach one persistent volume at `/data`: the Vault at `/data/vault`, SQLite at `/data/gateway.db`, and settings at `/data/setup` (private settings, hash, key).
  Without a volume, settings and the Vault clone are lost on every redeploy.
- There must be **exactly one instance** (the Git worker and setup state live inside the process). Do not enable replicas or autoscaling.
- Before setup, `/healthz` returns 200 and `/readyz` returns 503, and search and capture are off.
- The setup code is printed once in the container log, or you set `PVG_SETUP_TOKEN` (20 to 200 printable ASCII characters) as a platform secret.
  Do not paste it into chats or issues.
- The platform terminates HTTPS and does not know its front-end address, so the Railway and Render configs set `PVG_SECURE_COOKIES=true` (it does not trust
  forwarded headers and only forces Secure cookies, so you can log in only over HTTPS). Compose defaults to `false`.
  See [operations.md](operations.md#remote-access).
- TLS and access control are the platform's or the operator's responsibility. This repository does not provide a proxy, certificates, or DDNS.

## Comparison

| Option | Surface in this repository | Status | Notes |
| --- | --- | --- | --- |
| Docker Compose | `compose.yml` | Verified by the Compose smoke in CI | Loopback `127.0.0.1:18080`. Private path and TLS are the operator's |
| Railway | `.railway/railway.ts` | Not deployed, not verified | Pinned release image. The volume is attached to one service only. The domain is created manually |
| Render | `render.yaml` | Not deployed, not verified | Pinned release image. Needs a paid instance with a persistent disk |
| Fly.io | None | Researched only | Volumes are possible per the official docs, no surface provided |
| DigitalOcean App Platform | None | Researched only | No persistent volume, so not suitable |

## Docker Compose

Exactly the commands in [setup.md](setup.md#1-start-the-gateway). To use it from other PCs, use an encrypted private path such as an SSH tunnel, or a TLS endpoint you already
operate, and do not use plain public HTTP.

## Railway

Per the [official docs](https://docs.railway.com/infrastructure-as-code), new services cannot use `railway.toml`/`railway.json`, so this uses a
narrowly scoped tool based on the TypeScript SDK (the lockfile-pinned SDK in `.railway/`). `.railway/railway.ts` declares one service, one `/data` volume, and one
replica, and uses a pinned release image with `autoUpdates` off (the release replaces the tag with the exact digest). It does not watch the source repository,
so it does not redeploy on push, and you change the version yourself.

- The volume is [attached to one service only](https://docs.railway.com/reference/volumes). Do not delete or detach it.
- The generated domain is not managed in the file, so create it yourself in the UI. Set `PVG_SETUP_TOKEN` yourself in the dashboard (optional),
  or read the code from the deploy log otherwise.

## Render

Based on the [Blueprint spec](https://render.com/docs/blueprint-spec) and [disks](https://render.com/docs/disks). A persistent disk attaches only on a paid plan
(0.5 CPU / 512 MB), and a service with a disk is a single instance. `render.yaml` uses `runtime: image` with a pinned release image (the start command is the image's
default `python -m gateway.server`), a `/data` disk, automatic deploys off (`autoDeployTrigger: "off"`), and an automatically generated `PVG_SETUP_TOKEN` at creation.
Because the online URL opens immediately, read that value from the dashboard Environment before you open `/setup`. It does not create a custom domain, proxy, or DNS.
Creating the Blueprint incurs cost, so check first and proceed yourself.

## Researched only

- **Fly.io**: possible in theory with [configuration](https://docs.fly.io/reference/configuration) and [volumes](https://docs.fly.io/launch/volume-storage),
  but this repository provides no config. If you use it, confirm yourself that the volume is bound to one machine.
- **DigitalOcean App Platform**: per the [official docs](https://docs.digitalocean.com/products/app-platform/how-to/store-data/), the container
  filesystem is not persistent, which does not fit the `/data` contract. Not recommended.

## What the operator must verify

Account and billing, domain and SSL, proxy routing, whether the platform can reach the Vault repository over outbound GitHub SSH (port 22), and volume backup.
The repository files do not guarantee these. A public HTTPS service also exposes `/setup` and `/admin`. Before the claim they are protected by the setup code that only the
operator knows, and after it by the admin password, session, CSRF, and login limit, so finish the claim promptly and do not share the code.
The supplied Railway and Render configs do not isolate admin; to isolate it you must add the platform's access control yourself. Do not use plain HTTP.
