#!/usr/bin/env node

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {
  gatewayConfig, gatewayRequest, parseEnvFile, redactSecrets, truncateText, wellFormed,
} = require('../scripts/pvg-client.js');

const MAX_TEXT_CHARS = 64_000;
const MAX_TRANSCRIPT_READ_BYTES = 1_000_000;
const MAX_BATCH_BYTES = 3 * 1024 * 1024;
const MAX_CAPTURE_POSTS = 4;
const ORPHAN_IDLE_MS = 15 * 60_000;
const MERGE_FEATURE = 'conversation-merge-v1';

function localTimestamp(date = new Date()) {
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const hours = String(Math.floor(Math.abs(offsetMinutes) / 60)).padStart(2, '0');
  const minutes = String(Math.abs(offsetMinutes) % 60).padStart(2, '0');
  const local = new Date(date.getTime() + offsetMinutes * 60_000).toISOString().slice(0, -1);
  return `${local}${sign}${hours}:${minutes}`;
}

function hash(value, length = 24) {
  return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, length);
}

function cleanText(value) {
  const text = wellFormed(redactSecrets(value))
    .replace(/<recommended_plugins>[\s\S]*?<\/recommended_plugins>/g, '')
    .trim();
  return truncateText(text, MAX_TEXT_CHARS, '\n\n[truncated by PersonaVault hook]');
}

function textContent(value) {
  if (typeof value === 'string') {
    return value;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => textContent(item))
      .filter(Boolean)
      .join('\n');
  }
  if (!value || typeof value !== 'object') {
    return '';
  }
  if (value.type && !['text', 'input_text', 'output_text'].includes(value.type)) {
    return '';
  }
  for (const key of ['text', 'content', 'message', 'prompt']) {
    if (value[key] !== undefined) {
      const text = textContent(value[key]);
      if (text) {
        return text;
      }
    }
  }
  return '';
}

function transcriptUserText(record) {
  if (record?.role === 'user') {
    return textContent(record.content || record.message);
  }
  if (record?.type === 'user' && record.message?.role === 'user') {
    return textContent(record.message.content);
  }
  const payload = record?.payload;
  if (payload?.type === 'user_message') {
    return textContent(payload.message || payload.content);
  }
  if (payload?.type === 'message' && payload.role === 'user') {
    return textContent(payload.content);
  }
  return '';
}

function delegatedContext(transcriptPath, result = '') {
  const empty = { request: '', turnId: '', runId: '', handback: '' };
  if (!transcriptPath || !fs.existsSync(transcriptPath)) {
    return empty;
  }
  let fd;
  try {
    fd = fs.openSync(transcriptPath, 'r');
    const fileSize = fs.fstatSync(fd).size;
    const size = Math.min(fileSize, MAX_TRANSCRIPT_READ_BYTES);
    const buffer = Buffer.alloc(size);
    fs.readSync(fd, buffer, 0, size, fileSize - size);
    const lines = buffer.toString('utf8').split(/\r?\n/);
    if (fileSize > size) lines.shift();
    let request = '';
    let requestId = '';
    let turnId = '';
    let handback = '';
    let matched;
    for (const line of lines) {
      try {
        const record = JSON.parse(line);
        const payload = record.payload || {};
        if (record.type === 'turn_context'
            || ['task_started', 'task_complete'].includes(payload.type)) {
          if (payload.turn_id && payload.turn_id !== turnId) matched = undefined;
          turnId = String(payload.turn_id || turnId);
        }
        const text = cleanText(transcriptUserText(record));
        if (text) {
          matched = undefined;
          request = text;
          handback = '';
          requestId = String(record.uuid || record.id || payload.id || '');
          turnId = String(record.turn_id || payload.turn_id || turnId);
        }
        const message = record.message || payload;
        // Claude's SubagentHandback tool carries the real report; the hook's last message may be a closing line.
        for (const item of Array.isArray(message.content) ? message.content : []) {
          if (item?.type === 'tool_use' && item.name === 'SubagentHandback' && typeof item.input?.message === 'string') {
            handback = cleanText(item.input.message);
          }
        }
        const phase = message.phase || message.channel || record.phase || record.channel;
        let assistant = '';
        if ((!phase || phase === 'final') && message.role === 'assistant') {
          assistant = textContent(message.content);
        } else if (payload.type === 'task_complete') {
          assistant = textContent(payload.last_agent_message);
        } else if (payload.type === 'agent_message' && payload.phase === 'final') {
          assistant = textContent(payload.message);
        } else if (record.role === 'assistant' && (!phase || phase === 'final')) {
          assistant = textContent(record.content);
        }
        if (result && cleanText(assistant) === result) {
          matched = {
            request,
            turnId,
            handback,
            runId: turnId ? `turn:${turnId}` : requestId ? `request:${requestId}`
              : record.uuid ? `response:${record.uuid}` : '',
          };
        }
      } catch {
        // Ignore partial or non-JSON transcript lines.
      }
    }
    // ponytail: bounded tail read; widen the window if older request context is needed.
    return matched || {
      request, turnId, handback, runId: turnId ? `turn:${turnId}` : requestId ? `request:${requestId}` : '',
    };
  } catch {
    return empty;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function delegatedRequest(transcriptPath) {
  return delegatedContext(transcriptPath).request;
}

function pluginDataDir() {
  if (process.env.PLUGIN_DATA) {
    return process.env.PLUGIN_DATA;
  }
  if (process.env.CLAUDE_PLUGIN_DATA) {
    return process.env.CLAUDE_PLUGIN_DATA;
  }
  const base = process.env.APPDATA || process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
  return path.join(base, 'persona-vault-gateway', 'plugin');
}

function captureDisabled(input) {
  const cwd = String(input.cwd || '').trim();
  if (!cwd) {
    return false;
  }
  try {
    return /^id:\s*kn_persona_vault_curator_protocol_v1\s*$/m.test(
      fs.readFileSync(path.join(cwd, 'CURATOR.md'), 'utf8').slice(0, 4096),
    );
  } catch {
    return false;
  }
}

function readRecords(file) {
  if (!fs.existsSync(file)) {
    return [];
  }
  return fs.readFileSync(file, 'utf8')
    .split(/\r?\n/)
    .filter(Boolean)
    .flatMap((line) => {
      try {
        return [JSON.parse(line)];
      } catch {
        return [];
      }
    });
}

function appendRecord(file, record) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  fs.appendFileSync(file, `${JSON.stringify(record)}\n`, { encoding: 'utf8', mode: 0o600 });
  try {
    fs.chmodSync(file, 0o600);
  } catch {
    // Windows ACLs are managed by the user's profile directory.
  }
}

function readCheckpoint(file) {
  try {
    const checkpoint = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (checkpoint?.version === 1 && checkpoint.days && typeof checkpoint.days === 'object'
        && !Array.isArray(checkpoint.days)) {
      return checkpoint;
    }
  } catch {
    // A missing or damaged checkpoint safely falls back to resending the local spool.
  }
  return { version: 1, days: {} };
}

function writePrivateFile(file, content) {
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, content, { encoding: 'utf8', mode: 0o600 });
  fs.renameSync(temporary, file);
  try {
    fs.chmodSync(file, 0o600);
  } catch {
    // Windows ACLs are managed by the user's profile directory.
  }
}

function latestUnansweredRequest(records) {
  const answered = new Set(records.filter((record) => record.kind === 'main_response').map((record) => record.turn_id));
  return [...records].reverse().find((record) => record.kind === 'main_request' && !answered.has(record.turn_id));
}

function recordForInput(input, records, client, now = localTimestamp()) {
  const event = input.hook_event_name;
  const sessionId = String(input.session_id || '').trim();
  if (!sessionId) {
    return null;
  }
  const sessionKey = hash(`${client}:${sessionId}`, 16);
  // A delegated agent's task prompt/result is not human speech; SubagentStop records it as `request`.
  const delegated = Boolean(String(input.agent_id || '').trim());
  // The stored `now` makes a repeated prompt after pruning a new turn, while a retried append stays identical.
  const localTurn = (content) => `local-${hash(`${sessionId}:${content}:${now}:${records.length}`, 16)}`;

  if (event === 'UserPromptSubmit' && !delegated) {
    const content = cleanText(input.prompt);
    if (!content) {
      return null;
    }
    const last = records.at(-1);
    // Text-only dedupe applies only without an explicit turn id; distinct ids are distinct turns.
    if (!input.turn_id && last?.kind === 'main_request' && last.content === content) {
      return null;
    }
    const turnId = String(input.turn_id || localTurn(content));
    const eventId = `${client}:${sessionKey}:${hash(turnId, 20)}:user`;
    if (records.some((record) => record.event_id === eventId)) {
      return null;
    }
    return {
      event_id: eventId,
      session_id: sessionId,
      turn_id: turnId,
      kind: 'main_request',
      role: 'user',
      content,
      timestamp: now,
      cwd: input.cwd || '',
      client,
    };
  }

  if (event === 'Stop' && !delegated) {
    const content = cleanText(input.last_assistant_message);
    if (!content || (!input.turn_id && records.at(-1)?.kind === 'main_response' && records.at(-1)?.content === content)) {
      return null;
    }
    const request = latestUnansweredRequest(records);
    const turnId = String(input.turn_id || request?.turn_id || localTurn(content));
    const eventId = `${client}:${sessionKey}:${hash(turnId, 20)}:assistant`;
    if (records.some((record) => record.event_id === eventId)) {
      return null;
    }
    return {
      event_id: eventId,
      session_id: sessionId,
      turn_id: turnId,
      kind: 'main_response',
      role: 'assistant',
      content,
      timestamp: now,
      cwd: input.cwd || request?.cwd || '',
      client,
    };
  }

  if (event === 'SubagentStop') {
    const agentId = String(input.agent_id || '').trim();
    const closing = cleanText(input.last_assistant_message);
    const context = delegatedContext(input.agent_transcript_path, closing);
    const content = context.handback || closing;
    if (!agentId || !content) {
      return null;
    }
    const turnId = context.turnId || String(input.turn_id || input.run_id || '');
    const runId = context.runId || (turnId ? `turn:${turnId}` : '');
    const prefix = `${client}:${sessionKey}:subagent:`;
    const eventId = `${prefix}${hash(JSON.stringify([
      agentId, runId, context.runId ? '' : context.request, hash(input.last_assistant_message, 64),
      ...(context.handback ? [hash(context.handback, 64)] : []),
    ]), 40)}`;
    if (records.some((record) => record.event_id === eventId
        || (record.event_id === `${prefix}${hash(agentId, 20)}` && record.content === content
          && record.request === context.request && record.turn_id === turnId
          && (!context.runId || turnId)))) {
      return null;
    }
    return {
      event_id: eventId,
      session_id: sessionId,
      turn_id: turnId,
      kind: 'subagent_result',
      role: 'subagent',
      content,
      request: context.request,
      agent_id: agentId,
      agent_type: truncateText(cleanText(input.agent_type || 'subagent'), 120),
      timestamp: now,
      cwd: input.cwd || '',
      client,
    };
  }

  return null;
}

function payloadFromRecords(records, input) {
  const cwd = records.find((record) => record.cwd)?.cwd || input.cwd || '';
  const client = records[0]?.client || (process.env.PLUGIN_DATA ? 'codex' : 'claude');
  const project = (cwd && path.basename(cwd)) || client;
  return {
    kind: 'conversation',
    session_id: String(input.session_id),
    project,
    title: `${project} agent session`,
    started_at: records[0].timestamp,
    ended_at: records.at(-1).timestamp,
    messages: records.map((record) => ({
      role: record.role,
      content: record.content,
      event_id: record.event_id,
      turn_id: record.turn_id || null,
      agent_id: record.agent_id || null,
      agent_type: record.agent_type || null,
      request: record.request || null,
      timestamp: record.timestamp,
    })),
    context: {
      cwd,
      client,
      capture: 'persona-vault-plugin-hooks',
    },
    tags: ['agent-session', client],
    privacy: 'normal',
  };
}

function payloadsFromRecords(records, input) {
  const days = new Map();
  for (const record of records) {
    const day = String(record.timestamp || '').slice(0, 10);
    const dailyRecords = days.get(day) || [];
    dailyRecords.push(record);
    days.set(day, dailyRecords);
  }
  return [...days.values()].map((dailyRecords) => payloadFromRecords(dailyRecords, input));
}

function payloadDay(payload) {
  return String(payload.started_at || '').slice(0, 10);
}

function payloadHash(payload) {
  return hash(JSON.stringify(payload), 64);
}

function mergeBatch(payload, offset) {
  const batch = { ...payload, mode: 'merge', messages: [] };
  let bytes = Buffer.byteLength(JSON.stringify(batch));
  for (const message of payload.messages.slice(offset, offset + 500)) {
    const added = Buffer.byteLength(JSON.stringify(message)) + (batch.messages.length ? 1 : 0);
    if (bytes + added > MAX_BATCH_BYTES) break;
    batch.messages.push(message);
    bytes += added;
  }
  return batch.messages.length ? batch : null;
}

function prefixHash(payload, count) {
  return payloadHash({
    ...payload,
    ended_at: payload.messages[count - 1].timestamp,
    messages: payload.messages.slice(0, count),
  });
}

// A binding ties a spool to the endpoint+token it was captured under, so a later hook never
// replays it to a different server or agent. Only a hash is stored.
function bindingFor(config) {
  return hash(`${config.url}\n${config.token}`, 32);
}

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch {
    return undefined;
  }
}

function bindSession(file, binding, replace) {
  const current = readText(file);
  if (current === binding || (current !== undefined && !replace)) return;
  writePrivateFile(file, binding);
}

// Operator-visible state for failures that need action; bounded and free of payload text.
function writeStatus(dataDir, failure) {
  const file = path.join(dataDir, 'status.json');
  try {
    if (failure) {
      writePrivateFile(file, `${JSON.stringify({
        kind: failure.kind, status: failure.status || 0, error: failure.error || '', at: localTimestamp(),
      })}\n`);
    } else {
      fs.rmSync(file, { force: true });
    }
  } catch {
    // Status is advisory.
  }
}

// Oldest idle spool that is bound to the current endpoint/token and still has unsent days.
// Legacy or mismatched spools stay untouched: nothing proves which agent they belong to.
function findOrphanSession(spoolDir, ownKey, binding) {
  let names;
  try {
    names = fs.readdirSync(spoolDir);
  } catch {
    return null;
  }
  const candidates = [];
  for (const name of names) {
    const key = name.match(/^([0-9a-f]{32})\.jsonl$/)?.[1];
    if (!key || key === ownKey || readText(path.join(spoolDir, `${key}.binding`)) !== binding) continue;
    try {
      const spool = fs.statSync(path.join(spoolDir, name));
      if (Date.now() - spool.mtimeMs < ORPHAN_IDLE_MS) continue;
      const checkpointPath = path.join(spoolDir, `${key}.checkpoint.json`);
      const settled = fs.existsSync(checkpointPath) && readCheckpoint(checkpointPath).complete === true
        && fs.statSync(checkpointPath).mtimeMs >= spool.mtimeMs;
      if (!settled) candidates.push({ key, name, mtime: spool.mtimeMs });
    } catch {
      // A vanished file is simply not a candidate.
    }
  }
  for (const { key, name } of candidates.sort((a, b) => a.mtime - b.mtime).slice(0, 5)) {
    const sessionId = readRecords(path.join(spoolDir, name))[0]?.session_id;
    if (sessionId && hash(sessionId, 32) === key) return String(sessionId);
  }
  return null;
}

function sleep(milliseconds) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
}

function acquireLock(lockPath, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  do {
    try {
      return fs.openSync(lockPath, 'wx', 0o600);
    } catch (error) {
      if (error.code !== 'EEXIST') {
        return null;
      }
      try {
        if (Date.now() - fs.statSync(lockPath).mtimeMs > 30_000) {
          fs.unlinkSync(lockPath);
          continue;
        }
      } catch {
        continue;
      }
      if (Date.now() < deadline) sleep(Math.min(40, deadline - Date.now()));
    }
  } while (Date.now() < deadline);
  return null;
}

async function processSession(input, options) {
  const event = input.hook_event_name;
  const { deadline, orphan } = options;
  const sessionId = String(input.session_id || '').trim();
  if (!sessionId || captureDisabled(input)) {
    return false;
  }

  const config = gatewayConfig();
  const dataDir = pluginDataDir();
  const spoolDir = path.join(dataDir, 'spool', 'v2');
  fs.mkdirSync(spoolDir, { recursive: true, mode: 0o700 });
  const sessionKey = hash(sessionId, 32);
  const spoolPath = path.join(spoolDir, `${sessionKey}.jsonl`);
  const checkpointPath = path.join(spoolDir, `${sessionKey}.checkpoint.json`);
  const bindingPath = path.join(spoolDir, `${sessionKey}.binding`);
  const lockPath = `${spoolPath}.lock`;
  const lock = acquireLock(lockPath, event === 'SessionEnd' ? 200 : 3_000);
  if (lock === null) {
    return false;
  }

  let records;
  try {
    records = readRecords(spoolPath);
    const client = process.env.PLUGIN_DATA ? 'codex' : 'claude';
    const record = recordForInput(input, records, client);
    if (record) {
      appendRecord(spoolPath, record);
      records.push(record);
      if (config) bindSession(bindingPath, bindingFor(config), false);
    }
    if (!['Stop', 'SessionEnd'].includes(event) || !records.length) {
      return Boolean(record);
    }
  } finally {
    fs.closeSync(lock);
    fs.unlinkSync(lockPath);
  }

  if (!config) {
    return false;
  }
  const binding = bindingFor(config);
  if (orphan && readText(bindingPath) !== binding) return false;
  if (!orphan) bindSession(bindingPath, binding, true);
  // Network serialization must never prevent a concurrent hook from appending.
  const sendLockPath = `${spoolPath}.send.lock`;
  const sendLock = acquireLock(sendLockPath, 0);
  if (sendLock === null) return false;
  try {
    const snapshotLock = acquireLock(lockPath, Math.max(0, Math.min(200, deadline - Date.now())));
    if (snapshotLock === null) return false;
    try {
      // Another sender may have checkpointed/pruned since this hook appended.
      records = readRecords(spoolPath);
    } finally {
      fs.closeSync(snapshotLock);
      fs.unlinkSync(lockPath);
    }
    const checkpoint = readCheckpoint(checkpointPath);
    const payloads = payloadsFromRecords(records, input);
    const jobs = payloads.map((payload) => ({ payload, day: payloadDay(payload), digest: payloadHash(payload) }));
    const pending = jobs.filter(({ day, digest }) => checkpoint.days[day] !== digest)
      .sort((a, b) => a.day.localeCompare(b.day));
    const next = pending.findIndex(({ day }) => day > (checkpoint.last_day || ''));
    const queue = next < 0 ? pending : [...pending.slice(next), ...pending.slice(0, next)];
    const probe = queue.length && Date.now() < deadline
      ? await gatewayRequest(config, 'capabilities', undefined, Math.min(800, deadline - Date.now())) : null;
    let failure = null;
    // Only a confirmed merge-capable v3 Gateway may be written. A slow, failing or unreadable probe
    // waits for a later hook; a snapshot would replace whatever the server already holds.
    const features = probe?.ok && Array.isArray(probe.data?.features) ? probe.data.features : null;
    if (queue.length && !features?.includes(MERGE_FEATURE)) {
      if (probe) {
        failure = !probe.ok ? probe : features
          ? { kind: 'unsupported', status: probe.status, error: `${MERGE_FEATURE} not advertised` }
          : { kind: 'transient', status: probe.status, error: 'capabilities_unreadable' };
      }
      queue.length = 0;
    }
    if (!checkpoint.merge_progress || typeof checkpoint.merge_progress !== 'object'
        || Array.isArray(checkpoint.merge_progress)) checkpoint.merge_progress = {};
    let posts = 0;
    while (queue.length && posts < MAX_CAPTURE_POSTS && Date.now() < deadline - 50) {
      const job = queue.shift();
      const { payload, day, digest } = job;
      let count = 0;
      const progress = checkpoint.merge_progress[day];
      if (Number.isInteger(progress?.count) && progress.count > 0
          && progress.count <= payload.messages.length
          && progress.hash === prefixHash(payload, progress.count)) count = progress.count;
      const batch = mergeBatch(payload, count);
      if (!batch || batch.messages.length > 500
          || Buffer.byteLength(JSON.stringify(batch)) > MAX_BATCH_BYTES) continue;
      checkpoint.last_day = day;
      writePrivateFile(checkpointPath, `${JSON.stringify(checkpoint)}\n`);
      posts += 1;
      const response = await gatewayRequest(config, 'capture', batch, Math.min(2_500, deadline - Date.now()));
      if (!response.ok) {
        // Never ACK or prune on an error; keep the pending data for a later attempt.
        failure = response;
        if (['auth', 'upgrade'].includes(response.kind)) break;
        continue;
      }
      count += batch.messages.length;
      checkpoint.merge_progress[day] = { count, hash: prefixHash(payload, count) };
      if (count === payload.messages.length) checkpoint.days[day] = digest;
      else queue.push(job);
      writePrivateFile(checkpointPath, `${JSON.stringify(checkpoint)}\n`);
    }
    if (!orphan && (failure || probe?.ok || !pending.length)) writeStatus(dataDir, failure);

    const pruneLock = acquireLock(lockPath, Math.max(0, Math.min(200, deadline - Date.now())));
    if (pruneLock === null) return false;
    try {
      // Prune only whole days that are still identical to the acknowledged snapshot.
      records = readRecords(spoolPath);
      const current = payloadsFromRecords(records, input);
      const latestDay = current.map(payloadDay).sort().at(-1);
      const completed = new Set(current.filter((payload) => checkpoint.days[payloadDay(payload)] === payloadHash(payload)).map(payloadDay));
      const retained = records.filter((item) => {
        const day = String(item.timestamp || '').slice(0, 10);
        return day === latestDay || !completed.has(day);
      });
      const complete = completed.size === current.length;
      if (retained.length !== records.length) {
        writePrivateFile(spoolPath, `${retained.map((item) => JSON.stringify(item)).join('\n')}\n`);
        for (const day of completed) if (day !== latestDay) delete checkpoint.merge_progress[day];
      }
      if (retained.length !== records.length || checkpoint.complete !== complete) {
        checkpoint.complete = complete;
        writePrivateFile(checkpointPath, `${JSON.stringify(checkpoint)}\n`);
      }
      return complete;
    } finally {
      fs.closeSync(pruneLock);
      fs.unlinkSync(lockPath);
    }
  } finally {
    fs.closeSync(sendLock);
    fs.unlinkSync(sendLockPath);
  }
}

async function processHook(input, options = {}) {
  const deadline = options.deadline ?? Date.now() + (input.hook_event_name === 'SessionEnd' ? 800 : 3_300);
  const done = await processSession(input, { deadline, orphan: Boolean(options.orphan) });
  // Bounded replay: one idle, same-config spool per Stop, only with time left and a healthy own session.
  if (done && input.hook_event_name === 'Stop' && !options.orphan && deadline - Date.now() > 1_200) {
    const config = gatewayConfig();
    const orphanId = config && findOrphanSession(
      path.join(pluginDataDir(), 'spool', 'v2'), hash(String(input.session_id).trim(), 32), bindingFor(config),
    );
    if (orphanId) await processSession({ hook_event_name: 'Stop', session_id: orphanId }, { deadline, orphan: true });
  }
  return done;
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
}

async function main() {
  try {
    await processHook(await readStdin());
  } catch {
    // Capture is advisory: local or network failures must never block the agent.
  }
}

module.exports = {
  captureDisabled,
  cleanText,
  delegatedRequest,
  gatewayConfig,
  gatewayRequest,
  localTimestamp,
  mergeBatch,
  parseEnvFile,
  payloadFromRecords,
  payloadsFromRecords,
  pluginDataDir,
  processHook,
  recordForInput,
  transcriptUserText,
};

if (require.main === module) {
  main();
}
