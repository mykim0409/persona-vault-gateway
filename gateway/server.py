"""Managed launcher: `python -m gateway.server` serves the Gateway with browser-first onboarding.

One uvicorn worker (setup state, rate limits and the Git sync thread are process-local), listening on PORT
(default 8000). Persistent data lives under PVG_DATA_DIR (default /data). Set PVG_SECURE_COOKIES=true when the
public URL is HTTPS but the proxy is not trusted for forwarded headers (never widen FORWARDED_ALLOW_IPS for this).
Set PVG_TRUSTED_PROXY_HOPS=1 behind one such proxy (Render, Railway) so the login limiter keys on the real client.
"""
import os
import sys

import uvicorn

from . import onboarding

DEFAULT_PORT = 8000
HOST = "0.0.0.0"  # the container or platform decides what actually reaches this port


def port_from_env(env=os.environ) -> int:
    raw = (env.get("PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535.")
    return int(raw)


def main(run=uvicorn.run) -> int:
    try:
        port = port_from_env()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    onboarding.apply_defaults()
    run("gateway.app:app", host=HOST, port=port, workers=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
