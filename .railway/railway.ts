// Railway infrastructure-as-code (railway/iac DSL, https://docs.railway.com/infrastructure-as-code/reference).
// New Railway services cannot use railway.toml/railway.json, so this file is the config-as-code surface.
// Nothing here has been planned or applied: no Railway account was used to write it.
//
// It declares ONE service from the published Gateway image, started with the Gateway's launcher, one persistent
// volume at /data (SQLite, Vault clone, private setup state) and exactly one replica.
// The image is pinned to the release: the source repository is not watched, so a push to its main branch never
// redeploys your service, and autoUpdates is disabled. The release bundle replaces the tag below with the image
// digest. Move to a newer release by changing this reference on purpose.
// What it deliberately leaves out:
//   - PVG_SETUP_TOKEN: set it yourself in the dashboard (optional, 20-200 printable ASCII characters). Without it
//     the Gateway prints a random token to the deploy log. Do not derive it from ctx.randomString(): that helper is
//     a deterministic hash, not a secret.
//   - domains: generate the Railway domain manually in the UI once you can read the setup token. It is not managed here.
//   - volume size and region: Railway's defaults for your plan apply.
// The volume is a stateful resource: do not delete or detach it.
import { defineRailway, image, project, service, volume } from "railway/iac";

const GATEWAY_IMAGE = "ghcr.io/mykim0409/persona-vault-gateway:gateway-v0.1.0";

export default defineRailway(() => {
  const data = volume("persona-vault-data");

  const gateway = service("persona-vault-gateway", {
    source: image(GATEWAY_IMAGE, { autoUpdates: { type: "disabled" } }),
    start: "python -m gateway.server",
    healthcheck: "/healthz",
    replicas: 1,
    env: {
      PORT: "8000",
      PVG_DATA_DIR: "/data",
      VAULT_DIR: "/data/vault",
      DB_PATH: "/data/gateway.db",
      EMBEDDING_PROVIDER: "none",
      PVG_SECURE_COOKIES: "true",
    },
    volumeMounts: { "/data": data },
  });

  return project("persona-vault", { resources: [gateway, data] });
});
