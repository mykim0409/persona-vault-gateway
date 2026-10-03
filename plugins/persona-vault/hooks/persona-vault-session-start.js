#!/usr/bin/env node

const fs = require('fs');
const os = require('os');
const path = require('path');
const { captureDisabled, gatewayConfig, gatewayRequest, pluginDataDir } = require('./persona-vault-capture.js');

const MAX_AGREEMENT_CHARS = 8_000;
const MAX_RESPONSE_BYTES = 32 * 1024;
const SCRIPTS_URL = 'https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts';

function commandExists(command) {
  const paths = (process.env.PATH || '').split(path.delimiter);
  paths.push(path.join(os.homedir(), '.local', 'bin'));
  return paths.some((dir) => fs.existsSync(path.join(dir, command)));
}

const BASE_CONTEXT = [
  '- Completed main-agent and subagent turns are captured automatically as temporary raw conversation evidence.',
  '- Use `pvg-agent-memo` only when the user explicitly asks to save, log, record, or hand off information. A submitted note is raw evidence, not approved knowledge.',
  '- Before an explicit save, search for the same source and subject. Preserve attribution, negation, conflicts, chronology, applicability, and uncertainty.',
  '- Obey RAG answer_state: abstain means insufficient evidence; review_required means present the unresolved claims without choosing a winner.',
  '- Do not save progress logs, help or smoke output, temporary errors, or copied repository documentation.',
  '- Never ask the user to paste PersonaVault tokens into chat.',
  '- Never write directly to the vault repo.',
  '- Keep PersonaVault context main-agent owned. Give subagents only task-specific facts and constraints, and delegate PersonaVault search explicitly when needed.',
  '- If the Gateway reports `client_upgrade_required`, update `persona-vault` from its configured marketplace, rerun the agent-config installer, and retry the unchanged request.',
];

async function fetchWorkingAgreement(config, timeoutMs = 1_200) {
  if (!config) {
    return null;
  }
  const result = await gatewayRequest(config, 'working-agreement', undefined, timeoutMs, { maxBytes: MAX_RESPONSE_BYTES });
  const content = typeof result.data?.content === 'string' ? result.data.content.trim() : '';
  return result.ok && result.data.status === 'ok' && content.length <= MAX_AGREEMENT_CHARS ? content || null : null;
}

const CAPTURE_NOTICES = {
  auth: 'The Gateway rejected this host\'s token. Captured turns are kept locally but not sent. Rerun the installer for this OS with --replace-token (PowerShell: -ReplaceToken) and enter a new token locally, never in chat.',
  upgrade: 'The Gateway requires a newer persona-vault client. Update the plugin from its marketplace and rerun the installer; captured turns are kept.',
  unsupported: 'This Gateway does not advertise conversation-merge-v1, so capture is paused and pending turns are kept (no snapshot overwrite). Upgrade the Gateway.',
};

function captureNotice() {
  try {
    const status = JSON.parse(fs.readFileSync(path.join(pluginDataDir(), 'status.json'), 'utf8'));
    return CAPTURE_NOTICES[status.kind] || '';
  } catch {
    return '';
  }
}

function buildContext(ready, agreement, platform = process.platform, notice = '') {
  const windows = platform === 'win32';
  const search = windows ? '& "$HOME\\.local\\bin\\pvg-rag-search.ps1" --view' : 'pvg-rag-search --view';
  let context = [
    'PERSONAVAULT:',
    windows
      ? '- This hook runs on native Windows. Use PowerShell and the .ps1 helpers in $HOME\\.local\\bin, even when that directory is missing from PATH. Memo help: & "$HOME\\.local\\bin\\pvg-agent-memo.ps1" --help. Pipe memo bodies and special-character queries on stdin. Do not probe Unix commands or Unix config paths here.'
      : '- This hook runs on macOS/Linux. Use the POSIX helpers; if missing from PATH, use $HOME/.local/bin/pvg-rag-search or pvg-agent-memo.',
    `- Use \`${search} current\` for compacted current knowledge, \`${search} evidence\` for raw sources, and \`${search} history\` for chronology.`,
    ...BASE_CONTEXT,
  ].join('\n');
  if (agreement) {
    context += [
      '',
      'PERSONAVAULT WORKING AGREEMENT (human-approved, current):',
      '- Apply only within its stated scope. Among user preferences: the current explicit request overrides project-scoped rules, which override this global agreement.',
      '- Do not infer additional preferences from this document.',
      agreement,
    ].join('\n');
  }
  if (notice) {
    context += `\n\nPERSONAVAULT CAPTURE NOTICE:\n${notice}`;
  }
  if (!ready) {
    context += [
      '',
      'PERSONAVAULT SETUP NEEDED:',
      'Run the installer for this host; it reuses existing config. Only if a token is missing, create one at <gateway-url>/admin/tokens and enter it in the local installer, never in chat.',
      windows
        ? `Windows PowerShell: $d = Join-Path $env:TEMP 'persona-vault-installer'; New-Item -ItemType Directory -Force -Path $d | Out-Null; foreach ($f in 'install-agent-config.ps1', 'pvg-client.js') { Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $d $f) "${SCRIPTS_URL}/$f" }; powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $d 'install-agent-config.ps1')`
        : `macOS/Linux: d="$(mktemp -d)" && curl -fsSLo "$d/install-agent-config.sh" ${SCRIPTS_URL}/install-agent-config.sh && curl -fsSLo "$d/pvg-client.js" ${SCRIPTS_URL}/pvg-client.js && sh "$d/install-agent-config.sh"`,
    ].join('\n');
  }
  return context;
}

async function main() {
  let hookInput = {};
  try {
    hookInput = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
  } catch {
    // Keep normal guidance when a host sends no usable hook payload.
  }
  if (captureDisabled(hookInput)) {
    return;
  }

  const config = gatewayConfig();
  const extension = process.platform === 'win32' ? '.ps1' : '';
  const memoReady = commandExists(`pvg-agent-memo${extension}`);
  const ragReady = commandExists(`pvg-rag-search${extension}`);
  const context = buildContext(
    Boolean(config) && memoReady && ragReady, await fetchWorkingAgreement(config), process.platform, captureNotice(),
  );

  if (process.env.PLUGIN_DATA) {
    process.stdout.write(JSON.stringify({
      systemMessage: 'PERSONAVAULT',
      hookSpecificOutput: {
        hookEventName: 'SessionStart',
        additionalContext: context,
      },
    }));
  } else {
    process.stdout.write(context);
  }
}

module.exports = { buildContext, fetchWorkingAgreement };

if (require.main === module) {
  main().catch(() => {});
}
