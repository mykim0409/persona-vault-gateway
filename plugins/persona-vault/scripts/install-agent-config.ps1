# Installs the shared pvg-client.js plus thin pvg-agent-memo / pvg-rag-search launchers (.ps1 primary, .cmd compat).
# All request policy lives in pvg-client.js; this script only deploys and configures.
param(
    [string]$GatewayUrl = $env:PERSONA_VAULT_GATEWAY_URL,
    [string]$Token = $env:PERSONA_VAULT_TOKEN,
    [switch]$ReplaceToken
)

$ErrorActionPreference = "Stop"

$ConfigRoot = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $HOME ".config" }
$ConfigDir = Join-Path $ConfigRoot "persona-vault-gateway"
$EnvFile = Join-Path $ConfigDir "env.json"
$BinDir = Join-Path $HOME ".local\bin"
$LibDir = Join-Path $HOME ".local\share\persona-vault-gateway"
$ClientSource = Join-Path $PSScriptRoot "pvg-client.js"
$ClientTarget = Join-Path $LibDir "pvg-client.js"
$MemoPs1 = Join-Path $BinDir "pvg-agent-memo.ps1"
$RagPs1 = Join-Path $BinDir "pvg-rag-search.ps1"
$MemoCmd = Join-Path $BinDir "pvg-agent-memo.cmd"
$RagCmd = Join-Path $BinDir "pvg-rag-search.cmd"
# Node's JSON.parse rejects a BOM, and Windows PowerShell 5.1 "Set-Content -Encoding UTF8" writes one.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Read-SecretValue($Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

# PowerShell passes @args straight to node (no cmd.exe reparse) and the pipeline body as UTF-8 stdin.
# Windows PowerShell 5.1 encodes native-command stdin with the GLOBAL $OutputEncoding (default US-ASCII);
# a plain assignment inside a script only creates a script-local shadow the engine never reads.
$PsWrapperTemplate = @'
# Thin launcher: validation, payload, HTTP and error policy all live in pvg-client.js.
$Client = Join-Path $PSScriptRoot '..\share\persona-vault-gateway\pvg-client.js'
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine('Node.js is required for PersonaVault helpers.')
    exit 127
}
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$PreviousConsole = $null
$PreviousPipeEncoding = $global:OutputEncoding
try { $PreviousConsole = [Console]::OutputEncoding; [Console]::OutputEncoding = $Utf8 } catch { }
try {
    if ($MyInvocation.ExpectingInput) {
        $global:OutputEncoding = $Utf8
        $input | & node $Client __COMMAND__ @args
    } else {
        & node $Client __COMMAND__ @args
    }
    $Code = $LASTEXITCODE
} finally {
    $global:OutputEncoding = $PreviousPipeEncoding
    if ($PreviousConsole) { try { [Console]::OutputEncoding = $PreviousConsole } catch { } }
}
exit $Code
'@

function Write-PsWrapper($Path, $Command) {
    [IO.File]::WriteAllText($Path, $PsWrapperTemplate.Replace('__COMMAND__', $Command), [Text.Encoding]::ASCII)
}

function Write-Launcher($Path, $Command) {
    $Lines = @(
        '@echo off',
        'setlocal DisableDelayedExpansion',
        'where node >nul 2>nul',
        'if errorlevel 1 (',
        '  echo Node.js is required for PersonaVault helpers. 1>&2',
        '  exit /b 127',
        ')',
        "node `"%~dp0..\share\persona-vault-gateway\pvg-client.js`" $Command %*",
        'exit /b %ERRORLEVEL%'
    )
    [IO.File]::WriteAllText($Path, (($Lines -join "`r`n") + "`r`n"), [Text.Encoding]::ASCII)
}

if (!(Get-Command node -ErrorAction SilentlyContinue)) {
    throw "node is required for the PersonaVault helpers and plugin hooks."
}
if (!(Test-Path -LiteralPath $ClientSource)) {
    throw "Missing $ClientSource. Run the installer from the plugin's scripts directory, or download pvg-client.js next to it."
}

New-Item -ItemType Directory -Force -Path $ConfigDir, $BinDir, $LibDir | Out-Null
Copy-Item -LiteralPath $ClientSource -Destination $ClientTarget -Force
Write-PsWrapper $MemoPs1 "agent-memo"
Write-PsWrapper $RagPs1 "rag-search"
Write-Launcher $MemoCmd "agent-memo"
Write-Launcher $RagCmd "rag-search"

# An explicit -Token / $env:PERSONA_VAULT_TOKEN wins; -ReplaceToken prompts for a new one.
if (Test-Path -LiteralPath $EnvFile) {
    $Existing = [IO.File]::ReadAllText($EnvFile) | ConvertFrom-Json
    if (!$GatewayUrl) { $GatewayUrl = $Existing.PERSONA_VAULT_GATEWAY_URL }
    if (!$Token -and !$ReplaceToken) { $Token = $Existing.PERSONA_VAULT_TOKEN }
}

if (!$GatewayUrl) { $GatewayUrl = Read-Host "Gateway URL" }
if (!$Token) { $Token = Read-SecretValue "Agent token" }

if (!$GatewayUrl -or !$Token) {
    @"
Agent helpers installed, but token config was skipped.

Create or rotate an agent token at:
  <gateway-url>/admin/tokens

Then rerun this script.

Installed:
  $MemoPs1
  $RagPs1
  $ClientTarget
"@
    exit 0
}

$Json = @{
    PERSONA_VAULT_GATEWAY_URL = $GatewayUrl
    PERSONA_VAULT_TOKEN = $Token
} | ConvertTo-Json
[IO.File]::WriteAllText($EnvFile, $Json, $Utf8NoBom)

@"
Installed:
  $EnvFile
  $MemoPs1
  $RagPs1
  (compat) $MemoCmd, $RagCmd
  $ClientTarget

Add this to PATH if needed:
  $BinDir

Replace a rotated token later with:
  powershell -NoProfile -ExecutionPolicy Bypass -File "$PSCommandPath" -ReplaceToken

Verify helpers without writing a test memo:
  & "$MemoPs1" --help
  & "$RagPs1" "hello"
"@
