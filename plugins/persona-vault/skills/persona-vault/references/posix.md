# macOS / Linux Commands

Use this only when the helper actually runs in a POSIX shell, including an explicitly selected WSL environment.
Helpers are installed in `$HOME/.local/bin`; use that full path when PATH does not include it.
Config is `${XDG_CONFIG_HOME:-$HOME/.config}/persona-vault-gateway/env`. Let helpers load it; do not print it.

```bash
"$HOME/.local/bin/pvg-rag-search" --view current 'what is the current policy for this project?'
"$HOME/.local/bin/pvg-rag-search" --view evidence 'what observations support this claim?'
"$HOME/.local/bin/pvg-agent-memo" --help
```

Only after an explicit save request and the required source/subject search:

```bash
printf '%s\n' 'Situation: an experiment exposed a reproducible constraint.
Outcome: the constraint was verified in run_123.
Applicability: this project and environment only.
Uncertainty: not reproduced elsewhere.' |
  "$HOME/.local/bin/pvg-agent-memo" --project Example \
    --kind debugging --outcome success --session-id run-session-123 \
    --provenance direct_observation --evidence run_123 'Verified experiment constraint'
```

This is syntax guidance, not permission to submit an example or test memo.
For fresh semantic matching when required, and only if the main skill's fallback/quota rule permits:

```bash
PVG_RAG_REFRESH=1 "$HOME/.local/bin/pvg-rag-search" --view all 'recent changes'
```

If helpers/config are missing, prefer the [bundled installer](../../../scripts/install-agent-config.sh)
from the installed plugin, not the project cwd; it installs the shared `pvg-client.js` beside it.
Otherwise give the user:

```bash
d="$(mktemp -d)" && base=https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts
curl -fsSLo "$d/install-agent-config.sh" "$base/install-agent-config.sh" &&
  curl -fsSLo "$d/pvg-client.js" "$base/pvg-client.js" && sh "$d/install-agent-config.sh"
```

The installer reuses existing config and prompts locally for missing values. To replace a rotated token, rerun it
with `--replace-token` and enter the new token at its hidden prompt. Obtain a token at
`<gateway-url>/admin/tokens` only if needed; never paste it into chat. Do not try the Windows installer here.
