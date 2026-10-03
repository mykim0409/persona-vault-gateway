"""Exercise the real Node hook against a temporary HTTP Gateway."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.app import app
from gateway.core import generate_token, init_db, upsert_agent


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    hook = repo / "plugins/persona-vault/hooks/persona-vault-capture.js"
    with TemporaryDirectory(prefix="pvg-capture-http-") as temporary:
        root = Path(temporary)
        vault = root / "vault"
        database = root / "gateway.db"
        plugin_data = root / "plugin-data"
        config_dir = root / "config/persona-vault-gateway"
        config_dir.mkdir(parents=True)
        spool = plugin_data / "spool/v2"
        spool.mkdir(parents=True)
        session_id = "large-capture-http-integration"
        session_key = hashlib.sha256(session_id.encode()).hexdigest()[:32]
        token = generate_token()
        init_db(database)
        upsert_agent(
            database, "capture-test", token, ["conversation-log"], ["30_Conversations/raw"]
        )

        timestamp = "2026-01-01T10:00:00+09:00"
        records = [
            {
                "event_id": f"seed-{index}",
                "session_id": session_id,
                "turn_id": f"turn-{index}",
                "kind": "main_request",
                "role": "user",
                "content": f"record-{index}: " + "\uac00" * 3_000,
                "timestamp": timestamp,
                "cwd": "/integration",
                "client": "codex",
            }
            for index in range(501)
        ]
        spool_file = spool / f"{session_key}.jsonl"
        spool_file.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        assert spool_file.stat().st_size > 4 * 1024 * 1024

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            address = f"http://127.0.0.1:{listener.getsockname()[1]}"
            config = {
                "PERSONA_VAULT_GATEWAY_URL": address,
                "PERSONA_VAULT_TOKEN": token,
            }
            (config_dir / "env.json").write_text(json.dumps(config), encoding="utf-8")
            environment = {
                "VAULT_DIR": str(vault),
                "DB_PATH": str(database),
                "EMBEDDING_PROVIDER": "hash",
                "HOST_ID": "capture-integration",
            }
            with patch.dict(os.environ, environment):
                server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                thread = threading.Thread(
                    target=server.run, kwargs={"sockets": [listener]}, daemon=True
                )
                thread.start()
                try:
                    for _ in range(100):
                        if server.started:
                            break
                        time.sleep(0.02)
                    assert server.started, "temporary Gateway did not start"
                    node_env = {
                        **os.environ,
                        "HOME": str(root),
                        "USERPROFILE": str(root),
                        "XDG_DATA_HOME": str(root / "data"),
                        "PLUGIN_DATA": str(plugin_data),
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "APPDATA": str(root / "config"),
                        "NO_PROXY": "127.0.0.1",
                    }
                    event = json.dumps({"hook_event_name": "Stop", "session_id": session_id})
                    checkpoint_file = spool / f"{session_key}.checkpoint.json"
                    # Multiple invocations allow the hook to respect its runtime deadline.
                    for _ in range(12):
                        result = subprocess.run(
                            ["node", str(hook)], input=event, env=node_env, text=True,
                            capture_output=True, timeout=10, check=True,
                        )
                        assert not result.stderr, result.stderr
                        if checkpoint_file.exists():
                            checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                            if checkpoint.get("days", {}).get("2026-01-01"):
                                break
                    else:
                        raise AssertionError("large daily capture never reached its success checkpoint")

                    files = list(vault.rglob("*.md"))
                    assert len(files) == 1, files
                    content = files[0].read_text(encoding="utf-8")
                    event_ids = re.findall(r'"event_id":\s*"(seed-\d+)"', content)
                    assert len(event_ids) == 501
                    assert set(event_ids) == {f"seed-{index}" for index in range(501)}
                    for index in (0, 250, 500):
                        assert records[index]["content"] in content
                    subprocess.run(
                        ["node", str(hook)], input=event, env=node_env, text=True,
                        capture_output=True, timeout=10, check=True,
                    )
                    assert files[0].read_text(encoding="utf-8") == content
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    assert not thread.is_alive(), "temporary Gateway did not stop"
    print("capture HTTP integration: 501 messages, >4MiB UTF-8, retry idempotency passed")


if __name__ == "__main__":
    main()
