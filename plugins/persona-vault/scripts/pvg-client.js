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

// ---- Privacy filters. Deterministic and idempotent (every marker is a fixed point). Local heuristics
// that reduce exposure, not a DLP guarantee; run time is only tested on bounded adversarial inputs, with
// no formal complexity claim. ----

const REDACTED = '[REDACTED]';
const KEEP_MARKER = '(?!\\[REDACTED)';
const CREDENTIAL_WORDS = 'pass(?:word|wd|phrase)|secret|token(?!s)|api[_-]?key|access[_-]?key|private[_-]?key|signature|credentials?';
const NAME = '[A-Za-z0-9_.-]{0,64}';
// A quoted (possibly unterminated, to end of line) or bare value, consumed whole: no length cap that could leave a suffix.
// The alternatives are disjoint, so matching is a single forward scan.
const VALUE = String.raw`"(?:[^"\\\r\n]|\\.)*"?|'(?:[^'\\\r\n]|\\.)*'?|${KEEP_MARKER}[^\s,;&"'\`]+`;
// name, then : or =, then the value; names may carry prefixes (AWS_SECRET_ACCESS_KEY).
const CREDENTIAL_ASSIGNMENT = new RegExp(
  String.raw`(?<![A-Za-z0-9_.-])(${NAME}(?:${CREDENTIAL_WORDS})${NAME})(["']?[ \t]*[:=][ \t]*)(${VALUE})`, 'gi',
);
const AUTHORIZATION = new RegExp(
  String.raw`(authorization["']?[ \t]*[:=][ \t]*["']?(?:bearer|basic|token|digest)[ \t]+)${KEEP_MARKER}[^\s"'\`]+`, 'gi',
);
const BEARER = /\b(bearer[ \t]+)[A-Za-z0-9._~+/=-]{24,}/gi;
const CLI_SECRET_FLAG = new RegExp(
  String.raw`(--(?:password|passwd|passphrase|token|secret|api-?key)[ \t]+)(?!-)(${VALUE})`, 'gi',
);
const URL_USERINFO = /\b([a-z][a-z0-9+.-]{1,15}:\/\/)[^\s/?#@"'`<>]+@/gi;
const PRIVATE_KEY_BEGIN = /-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----/g;
const PRIVATE_KEY_END = /-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----/;
const PRIVATE_KEY_LINE = /^[ \t]*(?:[A-Za-z0-9+/]{16,}={0,2}|[A-Za-z0-9+/]{2,}={1,2}|(?:Proc-Type|DEK-Info|Version|Comment):.*)[ \t]*$/;

function redactAssignment(match, name, separator, value, offset, whole) {
  if (/^["']/.test(value)) return `${name}${separator}${maskQuoted(value)}`;
  const next = whole.slice(offset + match.length).match(/^[ \t]+([a-z]+)/)?.[1] || '';
  if (name.toLowerCase() === 'token' && separator.includes(':')
      && /^(?:use|set|read|load|store|keep|pass|export|get)$/.test(value)
      && /^(?:a|an|the|your|env|environment|from|via|vault|variable|instead|only)$/.test(next)) {
    // Only "the token: use an env var" style instructions are prose; every other bare value is redacted.
    const start = Math.max(0, offset - 40);
    const before = whole.slice(start, offset);
    const line = before.slice(before.lastIndexOf('\n') + 1);
    if (!((before.includes('\n') || start === 0) && /^[ \t\-*>]*(?:export[ \t]+)?$/.test(line))) return match;
  }
  return `${name}${separator}${REDACTED}`;
}

function maskQuoted(value) {
  return `${value[0]}${REDACTED}${value.length > 1 && value.endsWith(value[0]) ? value[0] : ''}`;
}

// Where a private key starting at `from` (just after its BEGIN line marker) ends. An incomplete key
// ends at its last confirmed base64/header line; adjacent prose is never consumed. A complete block
// (matching END) is redacted whole, including a short final base64 line.
function privateKeyEnd(text, from) {
  let end = from;
  let pos = from;
  for (let first = true; pos <= text.length; first = false) {
    let eol = text.indexOf('\n', pos);
    if (eol < 0) eol = text.length;
    const line = text.slice(pos, eol).replace(/\r$/, '');
    const marker = PRIVATE_KEY_END.exec(line);
    if (marker && (first || marker.index === line.search(/\S/))) return pos + marker.index + marker[0].length;
    if (first ? /^(?:[A-Za-z0-9+/=]|\\[nr])*[ \t]*$/.test(line) : PRIVATE_KEY_LINE.test(line)) end = pos + line.length;
    else if (!first && /^[A-Za-z0-9+/]+={0,2}[ \t]*$/.test(line)) { /* short line: counts only if END follows */ }
    else if (first || line.trim()) return end;
    pos = eol + 1;
  }
  return end;
}

function redactPrivateKeys(text) {
  const begin = new RegExp(PRIVATE_KEY_BEGIN.source, 'g');
  const endSearch = new RegExp(PRIVATE_KEY_END.source, 'g');
  let endHit; // cached next END at/after the current BEGIN, so repeated BEGIN/no-END input stays linear
  let output = '';
  let pos = 0;
  for (let match = begin.exec(text); match; match = begin.exec(text)) {
    const from = match.index + match[0].length;
    if (endHit === undefined || (endHit && endHit.index < from)) {
      endSearch.lastIndex = from;
      endHit = endSearch.exec(text);
    }
    const nextBegin = text.indexOf('-----BEGIN ', from);
    // A closed BEGIN..END frame is redacted whole whatever its body looks like; only an unclosed key
    // falls back to line-by-line matching that keeps adjacent prose.
    const closed = endHit && (nextBegin < 0 || endHit.index < nextBegin);
    const end = closed ? endHit.index + endHit[0].length : privateKeyEnd(text, from);
    output += `${text.slice(pos, match.index)}[REDACTED PRIVATE KEY]`;
    pos = end;
    begin.lastIndex = end;
  }
  return output + text.slice(pos);
}

function redactSecrets(value) {
  return redactPrivateKeys(String(value || ''))
    .replace(AUTHORIZATION, `$1${REDACTED}`)
    .replace(BEARER, `$1${REDACTED}`)
    .replace(URL_USERINFO, (match, scheme) => {
      const userinfo = match.slice(scheme.length, -1);
      return userinfo.includes(':') || userinfo.length >= 20 ? `${scheme}${REDACTED}@` : match;
    })
    .replace(CLI_SECRET_FLAG, (match, flag, value) => `${flag}${/^["']/.test(value) ? maskQuoted(value) : REDACTED}`)
    .replace(CREDENTIAL_ASSIGNMENT, redactAssignment)
    .replace(/\bpvg_[A-Za-z0-9_-]{16,}\b/g, '[REDACTED PVG TOKEN]')
    .replace(/\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g, '[REDACTED GITHUB TOKEN]')
    .replace(
      /\b(?:sk-[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{35}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b/g,
      '[REDACTED API KEY]',
    );
}

const CODE_LANGS = new Set([
  'bash', 'sh', 'shell', 'zsh', 'fish', 'powershell', 'pwsh', 'ps1', 'cmd', 'bat', 'console', 'terminal',
  'js', 'javascript', 'jsx', 'mjs', 'ts', 'typescript', 'tsx', 'node', 'py', 'python', 'rb', 'ruby', 'go', 'rust', 'rs',
  'java', 'kt', 'kotlin', 'swift', 'c', 'h', 'cpp', 'cc', 'cs', 'csharp', 'php', 'lua', 'perl', 'scala', 'dart', 'sql',
  'html', 'xml', 'css', 'scss', 'vue', 'svelte', 'graphql', 'proto',
  'json', 'jsonc', 'json5', 'yaml', 'yml', 'toml', 'ini', 'conf', 'cfg', 'properties', 'env', 'dotenv', 'dockerfile',
  'makefile', 'tf', 'hcl', 'gradle', 'nginx', 'diff', 'patch', 'log', 'logs',
]);
const PROSE_LANGS = new Set(['text', 'txt', 'plain', 'plaintext', 'md', 'markdown', 'quote', 'prompt', 'instructions']);
const DIFF_START = /^(?:diff --git |@@ -\d+(?:,\d+)? \+\d+)/;
const DIFF_LINE = /^(?:[ +-]|@@ |\\ No newline|diff --git |index [0-9a-f]+\.\.|new file mode|deleted file mode|similarity index|rename (?:from|to) |old mode|new mode|Binary files )/;
const ENV_LINE = /^(?:export[ \t]+)?[A-Z][A-Z0-9_]{2,}=\S*/;
const LOG_LINE = /^\s*(?:\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[?\d{2}:\d{2}:\d{2}|\[?(?:DEBUG|INFO|WARN|WARNING|ERROR|TRACE|FATAL)\]?[: ]|at \S.*:\d+(?::\d+)?\)?$|File ".*", line \d+|Traceback \(most recent call last\))/;
const CODE_LINE = /[{};]\s*$|^\s*(?:def |class |function |const |let |var |import |from \S+ import |return\b|if\s*\(|for\s*\(|while\s*\(|public |private |#include|\/\/|\/\*|#!)|=>|^\s*<\/?[a-z][^>]*>$/;

function fenceOf(line) {
  const open = /^([ \t]{0,3})(`{3,}|~{3,})/.exec(line);
  if (!open) return null;
  const info = line.slice(open[0].length).trim();
  return open[2][0] === '`' && info.includes('`') ? null : { indent: open[1], char: open[2][0], length: open[2].length, info };
}

function closesFence(line, open) {
  const close = /^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$/.exec(line);
  return Boolean(close) && close[1][0] === open.char && close[1].length >= open.length;
}

function dumpKind(rows) {
  const lines = rows.filter((line) => line.trim());
  const share = (test) => lines.filter((line) => test.test(line)).length / lines.length;
  if (lines.length < 3) return null;
  if (lines.some((line) => DIFF_START.test(line))
      || (lines.some((line) => /^--- \S/.test(line)) && lines.some((line) => /^\+\+\+ \S/.test(line)))) return 'diff';
  if (share(ENV_LINE) >= 0.8) return 'env';
  if (lines.length >= 8 && share(LOG_LINE) >= 0.8) return 'log';
  if (lines.length >= 5 && share(CODE_LINE) >= 0.6) return 'code';
  return null;
}

// Non-empty labelled code/config/diff/log fences and unlabelled fences that clearly are one; prose and
// Markdown fences stay.
function fenceKind(info, body) {
  const lang = (info.split(/[\s{]/, 1)[0] || '').toLowerCase();
  if (PROSE_LANGS.has(lang)) return null;
  if (CODE_LANGS.has(lang)) return body.some((line) => line.trim()) ? lang : null;
  return dumpKind(body);
}

// An unfenced diff, env or log dump starting at plain[i]; stable, content-free kind/end or null.
function dumpRun(plain, i) {
  const run = (test, minimum, kind) => {
    let end = i;
    while (end < plain.length && test.test(plain[end])) end += 1;
    return end - i >= minimum ? { kind, end } : null;
  };
  if (DIFF_START.test(plain[i]) || (/^--- \S/.test(plain[i]) && /^\+\+\+ \S/.test(plain[i + 1] || ''))) {
    return run(DIFF_LINE, 4, 'diff');
  }
  if (ENV_LINE.test(plain[i])) return run(ENV_LINE, 3, 'env');
  if (LOG_LINE.test(plain[i])) return run(LOG_LINE, 8, 'log');
  return null;
}

function omitDumps(text) {
  const lines = text.split('\n');
  const plain = lines.map((line) => (line.endsWith('\r') ? line.slice(0, -1) : line));
  // The marker keeps the line ending of the line it replaces (CRLF text stays CRLF).
  const marker = (indent, kind, count, line) => `${indent}[omitted ${kind} block, ${count} line${count === 1 ? '' : 's'}]${line.endsWith('\r') ? '\r' : ''}`;
  const output = [];
  for (let i = 0; i < lines.length;) {
    const open = fenceOf(plain[i]);
    if (open) {
      let end = i + 1;
      while (end < lines.length && !closesFence(plain[end], open)) end += 1;
      const body = plain.slice(i + 1, end);
      const kind = fenceKind(open.info, body);
      const next = Math.min(end + 1, lines.length);
      if (kind) output.push(marker(open.indent, kind, body.length, lines[i]));
      else for (let k = i; k < next; k += 1) output.push(lines[k]);
      i = next;
      continue;
    }
    const dump = dumpRun(plain, i);
    if (dump) {
      output.push(marker('', dump.kind, dump.end - i, lines[i]));
      i = dump.end;
    } else {
      output.push(lines[i]);
      i += 1;
    }
  }
  return output.join('\n');
}

// Everything captured text goes through: drop code/config/diff/log dumps, then mask credentials.
function minimizeText(value) {
  return redactSecrets(omitDumps(String(value || '')));
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
  NOTE_TYPES,
  OUTCOMES,
  PROVENANCE,
  VIEWS,
  configDir,
  gatewayConfig,
  gatewayRequest,
  minimizeText,
  omitDumps,
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
