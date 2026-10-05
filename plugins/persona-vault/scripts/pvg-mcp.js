#!/usr/bin/env node
// PersonaVault MCP server (stdio, newline-delimited JSON-RPC 2.0): pvg_search and pvg_memo as a typed facade over
// pvg-client.js. Each call runs the shared helper, so validation, payloads, config, timeouts and error text match
// the pvg-rag-search / pvg-agent-memo commands. Nothing but JSON-RPC messages may reach stdout.

const { spawn } = require('child_process');
const path = require('path');
const { NOTE_TYPES, OUTCOMES, PROVENANCE, VIEWS } = require('./pvg-client.js');

const CLIENT = path.join(__dirname, 'pvg-client.js');
const VERSIONS = ['2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05'];
const NOTE_KINDS = [...new Set(Object.values(NOTE_TYPES))]; // the v3 types, not the legacy episode/candidate aliases
let version = '0.0.0';
try {
  version = require('../.claude-plugin/plugin.json').version;
} catch { /* the version is informational only */ }

const text = (description) => ({ type: 'string', description });
const list = (description) => ({ type: 'array', items: { type: 'string' }, description });
const TOOLS = [{
  name: 'pvg_search',
  description: 'Search PersonaVault Gateway for prior notes, decisions, project history and raw evidence. Exact names, IDs and '
    + 'error strings work well. Returns "Answer state: ..." then the context; abstain means insufficient evidence, '
    + 'review_required means unresolved conflicting claims: present them without choosing a winner.',
  inputSchema: {
    type: 'object',
    properties: {
      query: { ...text('What to look for.'), minLength: 1, maxLength: 500 },
      view: { type: 'string', enum: VIEWS, default: 'all', description: 'current: accepted knowledge; evidence: raw sources; history: prior states; conflicts: unresolved claims.' },
      limit: { type: 'integer', minimum: 1, maximum: 20, default: 5 },
    },
    required: ['query'],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
}, {
  name: 'pvg_memo',
  description: 'Save one raw note to PersonaVault as evidence for later curation. Call ONLY when the user explicitly asked to save, '
    + 'log, record, remember or hand off information, after searching the same subject. Never for progress logs, test notes or code copies.',
  inputSchema: {
    type: 'object',
    properties: {
      title: { ...text('Short subject line.'), minLength: 1 },
      body: { ...text('Markdown body: situation, action, outcome, applicability, evidence, uncertainty.'), minLength: 1 },
      note_type: { type: 'string', enum: NOTE_KINDS, default: 'observation' },
      kind: text('Narrower classification, e.g. debugging.'),
      outcome: { type: 'string', enum: OUTCOMES, default: 'unknown' },
      project: text('Project name.'),
      provenance: { type: 'string', enum: PROVENANCE, default: 'reported' },
      session_id: text('Distinct per independent run.'),
      subject: text('Stable subject ID.'),
      evidence: list('Run, file, commit, issue or PersonaVault ID references.'),
      tags: list('Tags.'),
    },
    required: ['title', 'body'],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
}];

// Checks arguments against a tool's inputSchema (required, string/integer/array types and bounds) and returns '' or a
// one-line problem. Enums, defaults and unknown keys are left to the helper, which names the allowed values.
function invalid(schema, args) {
  if (typeof args !== 'object' || args === null || Array.isArray(args)) return 'arguments must be an object';
  for (const key of schema.required) if (args[key] == null) return `${key} is required`;
  for (const [key, rule] of Object.entries(schema.properties)) {
    const value = args[key];
    if (value == null) continue;
    if (rule.type === 'string') {
      if (typeof value !== 'string') return `${key} must be a string`;
      if (value.length < (rule.minLength ?? 0)) return `${key} must not be empty`;
      if (value.length > (rule.maxLength ?? Infinity)) return `${key} must be at most ${rule.maxLength} characters`;
    } else if (rule.type === 'integer') {
      if (!Number.isInteger(value) || value < (rule.minimum ?? -Infinity) || value > (rule.maximum ?? Infinity)) {
        return `${key} must be an integer in ${rule.minimum}..${rule.maximum}`;
      }
    } else if (rule.type === 'array' && (!Array.isArray(value) || value.some((item) => typeof item !== 'string'))) {
      return `${key} must be an array of strings`;
    }
  }
  return '';
}

// Tool argument -> CLI flag. Values only ever follow their flag, so a leading "-" cannot become an option.
const MEMO_FLAGS = {
  title: 'title', project: 'project', kind: 'kind', outcome: 'outcome', note_type: 'type', provenance: 'provenance',
  session_id: 'session-id', subject: 'subject',
};
const MEMO_LISTS = { evidence: 'evidence', tags: 'tag' };

function memoArgv(args) {
  const argv = [];
  for (const [key, flag] of Object.entries(MEMO_FLAGS)) if (args[key] != null) argv.push(`--${flag}`, String(args[key]));
  for (const [key, flag] of Object.entries(MEMO_LISTS)) for (const item of [].concat(args[key] ?? [])) argv.push(`--${flag}`, String(item));
  return argv;
}

// The helper's env switches (refresh, JSON output) are pinned off so a host environment cannot change tool behavior.
function runClient(command, argv, { stdin = '', limit = '' } = {}) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [CLIENT, command, ...argv], {
      env: { ...process.env, PVG_RAG_REFRESH: '', PVG_RAG_JSON: '', PVG_RAG_LIMIT: limit },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const out = [];
    const err = [];
    child.stdout.on('data', (chunk) => out.push(chunk));
    child.stderr.on('data', (chunk) => err.push(chunk));
    child.stdin.on('error', () => {}); // the helper may exit on a usage error before reading its input
    child.once('error', () => resolve({ code: 1, out: '', err: 'PersonaVault helper could not start (is node on PATH?)' }));
    child.once('close', (code) => resolve({ code, out: Buffer.concat(out).toString('utf8'), err: Buffer.concat(err).toString('utf8') }));
    child.stdin.end(stdin);
  });
}

async function callTool(name, args) {
  const problem = invalid(TOOLS.find((tool) => tool.name === name).inputSchema, args);
  if (problem) return { content: [{ type: 'text', text: problem }], isError: true };
  const run = name === 'pvg_search'
    ? await runClient('rag-search', ['--view', String(args.view ?? 'all'), '--query', String(args.query ?? '')],
      { limit: args.limit == null ? '' : String(args.limit) })
    : await runClient('agent-memo', memoArgv(args), { stdin: String(args.body ?? '') });
  const ok = run.code === 0;
  return { content: [{ type: 'text', text: (ok ? run.out : run.err || run.out).trim() || `PersonaVault helper exited with ${run.code}` }], isError: !ok };
}

const send = (message) => process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', ...message })}\n`);

async function handle({ id, method, params }) {
  if (id === undefined) return; // notifications (initialized, cancelled, ...) are never answered
  if (method === 'initialize') {
    const requested = params?.protocolVersion;
    return send({ id, result: {
      protocolVersion: VERSIONS.includes(requested) ? requested : VERSIONS[0],
      capabilities: { tools: {} },
      serverInfo: { name: 'persona-vault', version },
    } });
  }
  if (method === 'ping') return send({ id, result: {} });
  if (method === 'tools/list') return send({ id, result: { tools: TOOLS } });
  if (method === 'tools/call') {
    if (!TOOLS.some((tool) => tool.name === params?.name)) return send({ id, error: { code: -32602, message: `Unknown tool: ${params?.name}` } });
    return send({ id, result: await callTool(params.name, params.arguments ?? {}) });
  }
  return send({ id, error: { code: -32601, message: `Method not found: ${method}` } }); // also answers the 2026-07-28 server/discover probe
}

// Split on "\n" only: readline would also split on U+2028/U+2029, which JSON.stringify leaves raw inside strings.
let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  for (let eol = buffer.indexOf('\n'); eol >= 0; eol = buffer.indexOf('\n')) {
    const line = buffer.slice(0, eol).trim();
    buffer = buffer.slice(eol + 1);
    if (!line) continue;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      send({ id: null, error: { code: -32700, message: 'Parse error' } });
      continue;
    }
    if (typeof message?.method === 'string') {
      handle(message).catch(() => message.id !== undefined && send({ id: message.id, error: { code: -32603, message: 'Internal error' } }));
    } else if (Array.isArray(message) || message?.id !== undefined) { // a batch, or a request without a method
      send({ id: message?.id ?? null, error: { code: -32600, message: 'Invalid Request' } });
    }
  }
});
process.stdout.on('error', () => process.exit(0));
