# Native Windows Commands

Use PowerShell. From a cmd.exe-only tool, invoke PowerShell explicitly for these snippets;
do not install or probe Bash/WSL just to run the plugin. The `.ps1` launchers are thin wrappers that run
the same shared Node helper as the POSIX launchers, with the same `--long-flags`; old PowerShell spellings
such as `-View` and `-SessionId` are still accepted as aliases.

## Locate Once, Then Invoke

The installer puts wrappers in `$HOME\.local\bin`, which need not be in PATH:

```powershell
$Rag = Join-Path $HOME '.local\bin\pvg-rag-search.ps1'
$Memo = Join-Path $HOME '.local\bin\pvg-agent-memo.ps1'
Test-Path -LiteralPath $Rag
Test-Path -LiteralPath $Memo
& $Rag --view current 'what is the current policy for this project?'
```

Use `--view evidence` for raw sources or `--view history` for chronology. A new shell invocation may
require assigning `$Rag`/`$Memo` again.
Config is `%APPDATA%\persona-vault-gateway\env.json`, or `$HOME\.config\persona-vault-gateway\env.json`
when APPDATA is absent. Do not look for the Unix `env` file or print token-bearing JSON.

If script execution is blocked by policy, run the same file through PowerShell without changing the
command: `powershell -NoProfile -ExecutionPolicy Bypass -File $Rag --view current '...'`.
Windows PowerShell 5.1 can mangle embedded double quotes in arguments to native programs; put such text
on stdin (a search with no query argument reads the query from stdin).

`pvg-rag-search.cmd` and `pvg-agent-memo.cmd` are optional compatibility launchers for cmd.exe-only tools.
`cmd.exe` expands `%VAR%` and mangles `"`, `^`, and `&` in their arguments, so pass bodies and any such
text on stdin and keep arguments simple and quoted. Prefer the `.ps1` launchers.

## Explicitly Requested Notes Only

Help does not write a note:

```powershell
& $Memo --help
```

After the required source/subject search, pipe the approved body to the launcher; the wrapper sends the
pipeline to the helper as UTF-8:

```powershell
$body = @'
Situation: an experiment exposed a reproducible constraint.
Outcome: the constraint was verified in run_123.
Applicability: this project and environment only.
Uncertainty: not reproduced elsewhere.
'@
$body | & $Memo --title 'Verified experiment constraint' --project Example `
  --kind debugging --outcome success --session-id run-session-123 `
  --provenance direct_observation --evidence run_123
```

This is syntax guidance, not permission to submit an example or test memo.

## Refresh Only When Required

PowerShell uses `$env:`, not the Bash `VAR=1 command` prefix. Restore the previous value:

```powershell
$previous = $env:PVG_RAG_REFRESH
try {
  $env:PVG_RAG_REFRESH = '1'
  & $Rag --view all 'recent changes'
} finally {
  $env:PVG_RAG_REFRESH = $previous
}
```

Follow the main skill's keyword-fallback/quota rule before requesting refresh.

## Setup Only If Missing

Prefer the [bundled installer](../../../scripts/install-agent-config.ps1), resolving its path
from the installed plugin, not the project cwd; it installs the shared `pvg-client.js` beside it.
Otherwise give the user this Windows-only setup:

```powershell
$Dir = Join-Path $env:TEMP 'persona-vault-installer'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$Base = 'https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts'
foreach ($File in 'install-agent-config.ps1', 'pvg-client.js') { Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $Dir $File) "$Base/$File" }
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir 'install-agent-config.ps1')
```

The installer reuses existing config and prompts locally for missing values. To replace a rotated token,
rerun it with `-ReplaceToken` and enter the new token at its hidden prompt. If a token is needed,
direct the user to `<gateway-url>/admin/tokens`; never request it in chat. Stop after a concrete
setup or permission error and report it rather than falling back to Linux commands or raw token inspection.
