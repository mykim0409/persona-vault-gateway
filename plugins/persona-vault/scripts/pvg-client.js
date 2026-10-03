#!/usr/bin/env node
// Shared PersonaVault Gateway client: config, HTTP, and the pvg-rag-search / pvg-agent-memo commands.
// Hooks require this file; the OS installers only copy it beside thin launchers.

const fs = require('fs');
const http = require('http');
const https = require('https');
const os = require('os');
const path = require('path');

const MAX_DIAGNOSTIC_CHARS = 200;
const MAX_ERROR_BODY_BYTES = 4096;
const NOTE_TYPES = {
  observation: 'observation', proposal: 'proposal', handoff: 'handoff', episode: 'observation', candidate: 'proposal',
};
const OUTCOMES = ['success', 'failure', 'mixed', 'unknown', 'not_applicable'];
const PROVENANCE = ['direct_observation', 'derived', 'reported', 'human_asserted'];
const VIEWS = ['all', 'current', 'evidence', 'history', 'conflicts'];

function redactSecrets(value) {
  return String(value || '')
    .replace(/((?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+/gi, '$1[REDACTED]')
    .replace(/(authorization\s*[:=]\s*bearer\s+)[^\s"'`]+/gi, '$1[REDACTED]')
    .replace(/-----BEGIN [^-\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\n]*PRIVATE KEY-----/g, '[REDACTED PRIVATE KEY]')
    .replace(/\bpvg_[A-Za-z0-9_-]{16,}\b/g, '[REDACTED PVG TOKEN]')
    .replace(/\bgh[pousr]_[A-Za-z0-9_]{20,}\b/g, '[REDACTED GITHUB TOKEN]')
    .replace(/\bsk-[A-Za-z0-9_-]{20,}\b/g, '[REDACTED API KEY]');
}

function wellFormed(text) {
  return text.replace(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g, '�');
}

// Cut at max UTF-16 units without leaving half of a surrogate pair behind.
function truncateText(text, max, suffix = '') {
  if (text.length <= max) return text;
  let end = Math.max(0, max - suffix.length);
  if (end > 0 && /[\uD800-\uDBFF]/.test(text[end - 1])) end -= 1;
  return `${text.slice(0, end)}${suffix}`;
}

function stripBom(text) {
  return text.charCodeAt(0) === 0xFEFF ? text.slice(1) : text;
}

function parseEnvFile(text) {
  const result = {};
  for (const line of stripBom(text).split(/\r?\n/)) {
    const match = line.match(/^\s*(?:export\s+)?([A-Z0-9_]+)=(.*)\s*$/);
    if (!match) {
      continue;
    }
    let value = match[2].trim();
    if (value.startsWith("'") && value.endsWith("'")) {
      value = value.slice(1, -1).split("'\\''").join("'");
    } else if (value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1);
    }
    result[match[1]] = value;
  }
  return result;
}

function configDir() {
  const root = process.env.APPDATA || process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config');
  return path.join(root, 'persona-vault-gateway');
}

function gatewayConfig() {
  const dir = configDir();
  const jsonPath = path.join(dir, 'env.json');
  try {
    const config = fs.existsSync(jsonPath)
      ? JSON.parse(stripBom(fs.readFileSync(jsonPath, 'utf8')))
      : parseEnvFile(fs.readFileSync(path.join(dir, 'env'), 'utf8'));
    const url = new URL(config.PERSONA_VAULT_GATEWAY_URL);
    if (!['http:', 'https:'].includes(url.protocol) || !config.PERSONA_VAULT_TOKEN) {
      return null;
    }
    return {
      url: url.toString().replace(/\/$/, ''),
      token: config.PERSONA_VAULT_TOKEN,
    };
  } catch {
    return null;
  }
}

function classifyStatus(status, code) {
  if (status === 401 || status === 403) return 'auth';
  if (status === 409) return 'conflict';
  if (status === 410 && code === 'client_upgrade_required') return 'upgrade';
  if (status === 0 || status === 408 || status === 425 || status === 429 || status >= 500) return 'transient';
  return 'payload';
}

function sanitizeDiagnostic(value, token) {
  let text = redactSecrets(value);
  if (token) text = text.split(token).join('[REDACTED]');
  return truncateText(wellFormed(text).replace(/[\u0000-\u001f\u007f]+/g, ' ').trim(), MAX_DIAGNOSTIC_CHARS);
}

// Bounded, sanitized view of a non-2xx body: never the raw (possibly input-echoing) text.
function failureResult(status, text, token) {
  let detail = null;
  try {
    detail = JSON.parse(text)?.detail ?? null;
  } catch {
    // Non-JSON error bodies keep only their sanitized prefix.
  }
  const code = typeof detail?.code === 'string' ? sanitizeDiagnostic(detail.code, token) : '';
  const plugin = detail?.plugin && typeof detail.plugin === 'object' ? {
    name: sanitizeDiagnostic(detail.plugin.name || '', token),
    marketplace: sanitizeDiagnostic(detail.plugin.marketplace || '', token),
  } : null;
  return {
    ok: false,
    status,
    kind: classifyStatus(status, code),
    error: code || sanitizeDiagnostic(typeof detail === 'string' ? detail : text, token),
    plugin,
  };
}

// Resolves (never rejects) to { ok, status, kind, data, error, plugin }; kind is
// ok | auth | upgrade | conflict | payload | transient.
function gatewayRequest(config, route, payload, timeoutMs, options = {}) {
  const { maxBytes = 64_000, json = payload === undefined } = options;
  return new Promise((resolve) => {
    const body = payload === undefined ? null : Buffer.from(JSON.stringify(payload));
    const target = new URL(`${config.url}/gateway/v3/${route}`);
    const transport = target.protocol === 'https:' ? https : http;
    let timer;
    const finish = (value) => {
      clearTimeout(timer);
      resolve(value);
    };
    const transient = (error, status = 0) => finish({ ok: false, status, kind: 'transient', error });
    const request = transport.request(target, {
      method: body ? 'POST' : 'GET',
      headers: {
        Authorization: `Bearer ${config.token}`,
        Accept: 'application/json',
        ...(body ? { 'Content-Type': 'application/json', 'Content-Length': body.length } : {}),
      },
    }, (response) => {
      const status = response.statusCode;
      const success = status >= 200 && status < 300;
      const limit = success ? (json ? maxBytes : 0) : MAX_ERROR_BODY_BYTES;
      const chunks = [];
      let size = 0;
      response.on('data', (chunk) => {
        size += chunk.length;
        if (size <= limit) chunks.push(chunk);
        else if (success && json) {
          request.destroy();
          transient('response_too_large', status);
        }
      });
      response.on('error', () => transient('network_error', status));
      response.on('aborted', () => transient('network_error', status));
      response.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        if (!success) return finish(failureResult(status, text, config.token));
        if (!json) return finish({ ok: true, status, kind: 'ok', data: null });
        try {
          finish({ ok: true, status, kind: 'ok', data: JSON.parse(stripBom(text)) });
        } catch {
          transient('invalid_json', status);
        }
      });
    });
    timer = setTimeout(() => { request.destroy(); transient('timeout'); }, Math.max(1, timeoutMs));
    request.on('error', (error) => transient(sanitizeDiagnostic(error.code || 'network_error')));
    request.end(body);
  });
}

// ---- Commands: identical validation, payload, and error handling on every OS ----

class UsageError extends Error {}

const MEMO_VALUE_FLAGS = ['title', 'project', 'subject', 'session-id', 'type', 'kind', 'outcome', 'provenance'];
const MEMO_LIST_FLAGS = {
  alias: 'aliases', 'error-signature': 'errorSignatures', 'repository-source': 'repositorySources',
  tag: 'tags', tags: 'tags', evidence: 'evidence', method: 'methods', 'derived-from': 'derivedFrom',
  supports: 'supports', contradicts: 'contradicts', supersedes: 'supersedes', applies: 'applies',
};

const MEMO_USAGE = `Usage: pvg-agent-memo [OPTIONS] [--] [TITLE]

Reads the body from stdin.

  --type observation|proposal|handoff   (legacy aliases episode, candidate accepted)
  --title TITLE
  --kind KIND
  --outcome success|failure|mixed|unknown|not_applicable
  --project PROJECT
  --subject ID
  --alias TEXT
  --error-signature TEXT
  --session-id ID
  --repository-source REPO@COMMIT:PATH
  --tag TAG
  --provenance direct_observation|derived|reported|human_asserted
  --evidence REF
  --method REF
  --derived-from ID
  --supports ID
  --contradicts ID
  --supersedes ID
  --applies KEY=VALUE

Repeatable options may be given more than once. PowerShell spellings such as -SessionId are accepted.`;
const SEARCH_USAGE = 'Usage: pvg-rag-search [--view all|current|evidence|history|conflicts] [--query] QUERY\n'
  + 'Without QUERY the query is read from stdin.';

// Accepts --long-flag and the documented PowerShell spelling (-SessionId -> --session-id).
function flagName(token) {
  if (/^-[A-Za-z]/.test(token) && !token.startsWith('--') && token.length > 2) {
    return token.slice(1).replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
  }
  return token.startsWith('--') ? token.slice(2) : token.slice(1);
}

function parseArgs(argv, valueFlags, listFlags = {}) {
  const values = {};
  const lists = {};
  const positional = [];
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--') {
      positional.push(...argv.slice(index + 1));
      break;
    }
    if (!token.startsWith('-') || token === '-') {
      positional.push(token);
      continue;
    }
    const name = flagName(token);
    if (name === 'h' || name === 'help') {
      values.help = true;
    } else if (valueFlags.includes(name) || Object.hasOwn(listFlags, name)) {
      if (index + 1 >= argv.length) throw new UsageError(`--${name} requires a value`);
      const value = argv[index += 1];
      if (Object.hasOwn(listFlags, name)) (lists[listFlags[name]] ||= []).push(value);
      else values[name] = value;
    } else {
      throw new UsageError(`Unknown option: ${token}`);
    }
  }
  return { values, lists, positional };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return stripBom(Buffer.concat(chunks).toString('utf8'));
}

const clean = (items = []) => items.map((item) => item.trim()).filter(Boolean);

function oneOf(name, value, allowed) {
  if (!allowed.includes(value)) throw new UsageError(`--${name} requires ${allowed.join(', ')}`);
  return value;
}

async function memoPayload(argv) {
  const { values, lists, positional } = parseArgs(argv, MEMO_VALUE_FLAGS, MEMO_LIST_FLAGS);
  if (values.help) return { help: MEMO_USAGE };
  const type = (values.type ?? 'observation').toLowerCase();
  if (!Object.hasOwn(NOTE_TYPES, type)) throw new UsageError('--type requires observation, proposal, or handoff');
  const outcome = oneOf('outcome', values.outcome ?? 'unknown', OUTCOMES);
  const provenance = oneOf('provenance', values.provenance ?? 'reported', PROVENANCE);
  const applicability = {};
  for (const item of clean(lists.applies)) {
    const split = item.indexOf('=');
    if (split < 1 || !item.slice(0, split).trim()) throw new UsageError('--applies requires KEY=VALUE');
    applicability[item.slice(0, split).trim()] = item.slice(split + 1).trim();
  }
  const repositorySources = clean(lists.repositorySources).map((item) => {
    const match = item.match(/^(.+)@([0-9a-fA-F]{7,64}):(.+)$/s);
    if (!match) throw new UsageError('--repository-source requires REPO@COMMIT:PATH');
    return { repo_id: match[1], commit: match[2], path: match[3] };
  });
  const title = (values.title ?? (positional.join(' ') || 'Agent memo')).trim();
  const body = (await readStdin()).trim();
  if (!title) throw new UsageError('memo title is required');
  if (!body) throw new UsageError('memo body is required');
  const payload = {
    kind: 'note',
    title,
    body,
    tags: clean(lists.tags),
    note_type: NOTE_TYPES[type],
    note_kind: values.kind ?? 'note',
    outcome,
    applicability,
    relations: {
      supports: clean(lists.supports), contradicts: clean(lists.contradicts), supersedes: clean(lists.supersedes),
    },
    subject_id: values.subject?.trim() || null,
    subject_aliases: clean(lists.aliases),
    error_signatures: clean(lists.errorSignatures),
    session_id: values['session-id']?.trim() || null,
    repository_sources: repositorySources,
    provenance: {
      mode: provenance,
      evidence_refs: clean(lists.evidence),
      method_refs: clean(lists.methods),
      derived_from: clean(lists.derivedFrom),
    },
  };
  if (values.project?.trim()) payload.project = values.project.trim();
  return { route: 'capture', payload, timeoutMs: 20_000, retry: 'retry the unchanged Markdown body' };
}

async function searchPayload(argv) {
  const { values, positional } = parseArgs(argv, ['view', 'query']);
  if (values.help) return { help: SEARCH_USAGE };
  const view = oneOf('view', values.view ?? process.env.PVG_RAG_VIEW ?? 'all', VIEWS);
  const limit = process.env.PVG_RAG_LIMIT ? Number(process.env.PVG_RAG_LIMIT) : 5;
  if (!Number.isInteger(limit) || limit < 1) throw new UsageError('PVG_RAG_LIMIT must be a positive integer');
  const query = (values.query ?? positional.join(' ')).trim() || (await readStdin()).trim();
  if (!query) throw new UsageError('query is required');
  const refresh = ['1', 'true', 'yes'].includes((process.env.PVG_RAG_REFRESH || '').toLowerCase());
  return {
    route: 'search', payload: { query, limit, refresh, view }, timeoutMs: 60_000, retry: 'retry the search', search: true,
  };
}

function failureMessage(result, config, retry) {
  const where = `HTTP ${result.status || 'network'}${result.error ? `: ${result.error}` : ''}`;
  if (result.kind === 'upgrade') {
    const name = result.plugin?.name || 'persona-vault';
    const marketplace = result.plugin?.marketplace || 'mykim0409/persona-vault-gateway';
    return `PersonaVault client update required. Update ${name} from marketplace ${marketplace}, rerun this installer, then ${retry}.`;
  }
  if (result.kind === 'auth') {
    return `PersonaVault rejected the token (${where}). Create or rotate a token at ${config.url}/admin/tokens, then rerun the installer with --replace-token (PowerShell: -ReplaceToken).`;
  }
  if (result.kind === 'transient') return `PersonaVault is temporarily unavailable (${where}); ${retry} later.`;
  return `PersonaVault rejected the request (${where}).`;
}

async function runCommand(command, argv) {
  const prepare = { 'rag-search': searchPayload, 'agent-memo': memoPayload }[command];
  if (!prepare) throw new UsageError('usage: pvg-client.js rag-search|agent-memo [OPTIONS]');
  const request = await prepare(argv);
  if (request.help) {
    process.stdout.write(`${request.help}\n`);
    return 0;
  }
  const config = gatewayConfig();
  if (!config) {
    process.stderr.write(`Missing or invalid PersonaVault config in ${configDir()}. Create a token at <gateway-url>/admin/tokens, then run the PersonaVault installer for this OS.\n`);
    return 1;
  }
  const result = await gatewayRequest(config, request.route, request.payload, request.timeoutMs,
    { json: true, maxBytes: 8 * 1024 * 1024 });
  if (!result.ok) {
    process.stderr.write(`${failureMessage(result, config, request.retry)}\n`);
    return result.kind === 'upgrade' ? 75 : 1;
  }
  if (!request.search) {
    process.stdout.write(`${JSON.stringify(result.data)}\n`);
  } else if (process.env.PVG_RAG_JSON) {
    process.stdout.write(`${JSON.stringify(result.data, null, 2)}\n`);
  } else {
    const state = result.data.answer_state || {};
    process.stdout.write(`Answer state: ${state.state || 'unknown'} (${state.reason || 'unspecified'})\n`
      + `${result.data.context || '(no PersonaVault RAG results)'}\n`);
  }
  return 0;
}

module.exports = {
  configDir,
  gatewayConfig,
  gatewayRequest,
  parseEnvFile,
  redactSecrets,
  runCommand,
  stripBom,
  truncateText,
  wellFormed,
};

if (require.main === module) {
  runCommand(process.argv[2], process.argv.slice(3)).then((code) => {
    process.exitCode = code;
  }, (error) => {
    process.stderr.write(`${error instanceof UsageError ? error.message : `PersonaVault helper failed: ${sanitizeDiagnostic(error.message)}`}\n`);
    process.exitCode = error instanceof UsageError ? 2 : 1;
  });
}
