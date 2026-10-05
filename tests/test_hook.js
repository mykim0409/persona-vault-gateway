const assert = require('assert');
const childProcess = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

// Every hook/helper (in-process or child) must see only this sandbox, never the real user config.
const SANDBOX = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-sandbox-'));
Object.assign(process.env, {
  HOME: SANDBOX,
  USERPROFILE: SANDBOX,
  XDG_CONFIG_HOME: path.join(SANDBOX, 'config'),
  XDG_DATA_HOME: path.join(SANDBOX, 'data'),
  APPDATA: path.join(SANDBOX, 'appdata'),
  PLUGIN_DATA: path.join(SANDBOX, 'plugin-data'),
  NO_PROXY: '127.0.0.1,localhost',
});
delete process.env.CLAUDE_PLUGIN_DATA;
delete process.env.PERSONA_VAULT_GATEWAY_URL;
delete process.env.PERSONA_VAULT_TOKEN;
process.on('exit', () => fs.rmSync(SANDBOX, { recursive: true, force: true }));

const capture = require('../plugins/persona-vault/hooks/persona-vault-capture.js');
const pvgClient = require('../plugins/persona-vault/scripts/pvg-client.js');
const sessionStart = require('../plugins/persona-vault/hooks/persona-vault-session-start.js');

function runProcess(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = childProcess.spawn(command, args, {
      env: options.env || process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const stdout = [];
    const stderr = [];
    const timer = setTimeout(() => child.kill(), 5_000);
    child.stdout.on('data', (chunk) => stdout.push(chunk));
    child.stderr.on('data', (chunk) => stderr.push(chunk));
    child.once('error', reject);
    child.once('close', (status) => {
      clearTimeout(timer);
      resolve({
        status,
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).toString('utf8'),
      });
    });
    child.stdin.end(options.input || '');
  });
}

async function workingAgreementIntegrationTest() {
  const requests = [];
  let available = true;
  const server = http.createServer((request, response) => {
    requests.push({ path: request.url, authorization: request.headers.authorization });
    if (!available) {
      response.writeHead(404);
      response.end();
      return;
    }
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({
      status: 'ok',
      path: '10_User/WORKING_AGREEMENT.md',
      content: '# Working Agreement\n\nPrefer concise status updates.',
    }));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  try {
    const config = {
      url: `http://127.0.0.1:${server.address().port}`,
      token: 'pvg_test_token',
    };
    const agreement = await sessionStart.fetchWorkingAgreement(config, 500);
    assert(agreement.includes('Prefer concise status updates.'));
    assert.strictEqual(requests[0].path, '/gateway/v3/working-agreement');
    assert.strictEqual(requests[0].authorization, 'Bearer pvg_test_token');
    const context = sessionStart.buildContext(true, agreement, 'linux');
    assert(context.includes('PERSONAVAULT WORKING AGREEMENT'));
    assert(context.includes('current explicit request overrides'));
    assert(context.includes('Keep PersonaVault context main-agent owned'));
    assert(context.includes('only when the user explicitly asks'));
    assert(context.includes('pvg-rag-search --view current'));
    assert(context.includes('pvg_search` and `pvg_memo` MCP tools are listed, use them; otherwise use the helper commands below'));
    assert(!context.includes('--bundle'));
    assert(!/\b(?:episode|candidate)\b/.test(context));
    assert(!context.includes('PERSONAVAULT SETUP NEEDED'));
    for (const platform of ['win32', 'darwin', 'linux']) {
      const ready = sessionStart.buildContext(true, agreement, platform);
      const missing = sessionStart.buildContext(false, null, platform);
      const hinted = sessionStart.buildContext(true, null, platform, '', ['pvg-agent-memo']);
      assert(ready.split('\n')[1].includes('MCP tools are listed, use them'), 'MCP guidance comes first');
      assert(!ready.includes('Helper commands not found') && !ready.includes('SETUP NEEDED'));
      assert(hinted.includes('Helper commands not found: pvg-agent-memo') && !hinted.includes('PERSONAVAULT SETUP NEEDED'));
      assert(sessionStart.buildContext(false, null, platform, '', []).includes('PERSONAVAULT SETUP NEEDED'));
      if (platform === 'win32') {
        assert(ready.includes('& "$HOME\\.local\\bin\\pvg-rag-search.ps1" --view current'));
        assert(ready.includes('pvg-agent-memo.ps1" --help'));
        assert(!/\s-(?:View|Help)\b/.test(ready), 'same canonical flags as POSIX');
        assert(missing.includes('install-agent-config.ps1'));
        assert(missing.includes('pvg-client.js') && !missing.includes('install-agent-config.sh'));
        assert(!missing.includes('/tmp/'));
      } else {
        assert(ready.includes('pvg-rag-search --view current'));
        assert(!ready.includes('.cmd'));
        assert(missing.includes('install-agent-config.sh'));
        assert(missing.includes('pvg-client.js'));
        assert(!missing.includes('install-agent-config.ps1'));
      }
    }
    const hooks = JSON.parse(fs.readFileSync(
      path.join(__dirname, '../plugins/persona-vault/hooks/hooks.json'),
      'utf8',
    ));
    assert(!Object.hasOwn(hooks.hooks, 'SubagentStart'));

    available = false;
    assert.strictEqual(await sessionStart.fetchWorkingAgreement(config, 500), null);
    assert(sessionStart.buildContext(false, null).includes('PERSONAVAULT SETUP NEEDED'));
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

async function captureIntegrationTest() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-hook-http-'));
  const requests = [];
  const reads = [];
  const methods = [];
  let statusCode = 200;
  let features = ['conversation-merge-v1'];
  let capabilityStatus = 200;
  let capabilityDelay = 0;
  let captureStatus = () => statusCode;
  let stallCapture = () => false;
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', async () => {
      methods.push(request.method);
      const received = {
        path: request.url,
        authorization: request.headers.authorization,
        bytes: Buffer.concat(chunks).length,
      };
      if (request.method === 'GET' && request.url === '/gateway/v3/capabilities') {
        reads.push(received);
        if (capabilityDelay) await new Promise((resolve) => setTimeout(resolve, capabilityDelay));
        response.writeHead(capabilityStatus, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ features }));
        return;
      }
      assert.strictEqual(request.method, 'POST');
      assert.strictEqual(request.url, '/gateway/v3/capture');
      received.body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      requests.push(received);
      response.writeHead(captureStatus(received.body), { 'Content-Type': 'application/json' });
      if (stallCapture(received.body)) {
        response.write(' ');
        const timer = setInterval(() => response.write(' '), 50);
        response.on('close', () => clearInterval(timer));
        return;
      }
      response.end('{"status":"ok"}');
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });

  const oldEnv = {
    PLUGIN_DATA: process.env.PLUGIN_DATA,
    XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME,
    APPDATA: process.env.APPDATA,
  };
  try {
    process.env.PLUGIN_DATA = path.join(temp, 'plugin-data');
    process.env.XDG_CONFIG_HOME = path.join(temp, 'config');
    process.env.APPDATA = process.env.XDG_CONFIG_HOME;
    const configDir = path.join(process.env.XDG_CONFIG_HOME, 'persona-vault-gateway');
    fs.mkdirSync(configDir, { recursive: true });
    const writeConfig = (token) => fs.writeFileSync(
      path.join(configDir, 'env'),
      `export PERSONA_VAULT_GATEWAY_URL='http://127.0.0.1:${server.address().port}'\n`
      + `export PERSONA_VAULT_TOKEN='${token}'\n`,
    );
    writeConfig('pvg_test_token');

    await capture.processHook({
      hook_event_name: 'UserPromptSubmit',
      session_id: 'integration-session',
      turn_id: 'turn-1',
      prompt: 'Capture integration request',
      cwd: '/workspace/persona-vault',
    });
    assert.strictEqual(requests.length, 0);

    assert(await capture.processHook({
      hook_event_name: 'Stop',
      session_id: 'integration-session',
      turn_id: 'turn-1',
      last_assistant_message: 'Capture integration result',
      cwd: '/workspace/persona-vault',
    }));
    assert.strictEqual(requests.length, 1);
    assert.strictEqual(requests[0].path, '/gateway/v3/capture');
    assert.strictEqual(requests[0].authorization, 'Bearer pvg_test_token');
    assert.strictEqual(requests[0].body.kind, 'conversation');
    assert.strictEqual(requests[0].body.messages.length, 2);
    assert.strictEqual(requests[0].body.session_id, 'integration-session');
    assert.strictEqual(requests[0].body.mode, 'merge');
    assert.deepStrictEqual(methods, ['GET', 'POST']);
    assert.strictEqual(reads[0].bytes, 0);
    assert.strictEqual(reads[0].authorization, 'Bearer pvg_test_token');

    assert(await capture.processHook({
      hook_event_name: 'Stop',
      session_id: 'integration-session',
      turn_id: 'turn-1',
      last_assistant_message: 'Capture integration result',
      cwd: '/workspace/persona-vault',
    }));
    assert.strictEqual(requests.length, 1);
    assert.strictEqual(reads.length, 1);

    await capture.processHook({
      hook_event_name: 'UserPromptSubmit',
      session_id: 'integration-session',
      turn_id: 'turn-2',
      prompt: 'Retry this turn after an outage',
      cwd: '/workspace/persona-vault',
    });
    statusCode = 503;
    assert.strictEqual(await capture.processHook({
      hook_event_name: 'Stop',
      session_id: 'integration-session',
      turn_id: 'turn-2',
      last_assistant_message: 'Retried capture result',
      cwd: '/workspace/persona-vault',
    }), false);
    assert.strictEqual(requests.length, 2);

    statusCode = 200;
    assert(await capture.processHook({
      hook_event_name: 'Stop',
      session_id: 'integration-session',
      turn_id: 'turn-2',
      last_assistant_message: 'Retried capture result',
      cwd: '/workspace/persona-vault',
    }));
    assert.strictEqual(requests.length, 3);
    assert.strictEqual(requests[2].body.messages.length, 2);
    assert.deepStrictEqual(requests[2].body, requests[1].body);

    assert(await capture.processHook({
      hook_event_name: 'Stop',
      session_id: 'integration-session',
      turn_id: 'turn-2',
      last_assistant_message: 'Retried capture result',
      cwd: '/workspace/persona-vault',
    }));
    assert.strictEqual(requests.length, 3);

    const spoolDir = path.join(process.env.PLUGIN_DATA, 'spool', 'v2');
    const checkpoints = fs.readdirSync(spoolDir)
      .filter((file) => file.endsWith('.checkpoint.json'));
    assert.strictEqual(checkpoints.length, 1);
    const checkpoint = JSON.parse(fs.readFileSync(
      path.join(spoolDir, checkpoints[0]),
      'utf8',
    ));
    assert.strictEqual(checkpoint.version, 1);
    assert.strictEqual(Object.keys(checkpoint.days).length, 1);

    const dailySession = 'daily-checkpoint-session';
    const dailyKey = crypto.createHash('sha256').update(dailySession).digest('hex').slice(0, 32);
    const dailySpool = path.join(spoolDir, `${dailyKey}.jsonl`);
    const today = capture.localTimestamp().slice(0, 10);
    const dailyRecords = [
      {
        event_id: 'old-user', session_id: dailySession, turn_id: 'old', kind: 'main_request',
        role: 'user', content: 'Historical request', timestamp: '2000-01-01T10:00:00Z',
        cwd: '/workspace/persona-vault', client: 'codex',
      },
      {
        event_id: 'old-assistant', session_id: dailySession, turn_id: 'old', kind: 'main_response',
        role: 'assistant', content: 'Historical response', timestamp: '2000-01-01T10:01:00Z',
        cwd: '/workspace/persona-vault', client: 'codex',
      },
      {
        event_id: 'today-user', session_id: dailySession, turn_id: 'today', kind: 'main_request',
        role: 'user', content: 'Current request', timestamp: `${today}T10:00:00+09:00`,
        cwd: '/workspace/persona-vault', client: 'codex',
      },
    ];
    fs.writeFileSync(dailySpool, `${dailyRecords.map((item) => JSON.stringify(item)).join('\n')}\n`);

    assert(await capture.processHook({
      hook_event_name: 'Stop',
      session_id: dailySession,
      turn_id: 'today',
      last_assistant_message: 'Current response',
      cwd: '/workspace/persona-vault',
    }));
    assert.deepStrictEqual(
      requests.slice(3).map((request) => request.body.started_at.slice(0, 10)),
      ['2000-01-01', today],
    );
    assert(readSpoolDates(dailySpool).every((day) => day === today));

    assert(await capture.processHook({
      hook_event_name: 'Stop',
      session_id: dailySession,
      turn_id: 'today',
      last_assistant_message: 'Current response',
      cwd: '/workspace/persona-vault',
    }));
    assert.strictEqual(requests.length, 5);

    const digest = (payload) => crypto.createHash('sha256').update(JSON.stringify(payload)).digest('hex');
    function seed(name, count, day = today, content = 'Batch request') {
      const key = crypto.createHash('sha256').update(name).digest('hex').slice(0, 32);
      const file = path.join(spoolDir, `${key}.jsonl`);
      const checkpointFile = path.join(spoolDir, `${key}.checkpoint.json`);
      const records = Array.from({ length: count }, (_, index) => ({
        event_id: `${name}-${index}`, session_id: name, turn_id: `turn-${index}`, kind: 'main_request',
        role: 'user', content, timestamp: `${day}T10:00:00+09:00`,
        cwd: '/workspace/persona-vault', client: 'codex',
      }));
      fs.writeFileSync(file, records.map((record) => `${JSON.stringify(record)}\n`).join(''));
      return {
        file, checkpointFile, records,
        input: { hook_event_name: 'Stop', session_id: name },
        checkpoint: () => JSON.parse(fs.readFileSync(checkpointFile, 'utf8')),
      };
    }

    const large = seed('large', 1001);
    let start = requests.length;
    assert(await capture.processHook(large.input));
    let batches = requests.slice(start);
    assert.deepStrictEqual(batches.map((request) => request.body.messages.length), [500, 500, 1]);
    assert.deepStrictEqual(batches.flatMap((request) => request.body.messages.map((message) => message.event_id)),
      large.records.map((record) => record.event_id));
    assert.strictEqual(large.checkpoint().days[today], digest(capture.payloadFromRecords(large.records, large.input)));
    start = requests.length;
    assert(await capture.processHook(large.input));
    assert.strictEqual(requests.length, start);

    const multibyte = seed('multibyte', 300, today, '\uac00'.repeat(10_000) + '\n"\\');
    for (const record of multibyte.records) record.request = '\ud83d\ude80'.repeat(1_000);
    fs.writeFileSync(multibyte.file, multibyte.records.map((record) => `${JSON.stringify(record)}\n`).join(''));
    start = requests.length;
    assert(await capture.processHook(multibyte.input));
    batches = requests.slice(start);
    assert(batches.length > 1);
    assert(batches.every((request) => request.bytes <= 3 * 1024 * 1024 && request.body.messages.length <= 500));
    assert(batches.every((request) => request.body.mode === 'merge' && request.body.context.client === 'codex'));
    assert.deepStrictEqual(batches.flatMap((request) => request.body.messages.map((message) => message.content)),
      multibyte.records.map((record) => record.content));

    const partial = seed('partial', 1001, '2001-01-01');
    const nextDate = { ...partial.records[0], event_id: 'next-date', timestamp: `${today}T10:00:00+09:00` };
    fs.appendFileSync(partial.file, `${JSON.stringify(nextDate)}\n`);
    captureStatus = (body) => body.messages[0].event_id === 'partial-500' ? 503 : 200;
    start = requests.length;
    assert.strictEqual(await capture.processHook(partial.input), false);
    assert.strictEqual(partial.checkpoint().days['2001-01-01'], undefined);
    assert(partial.checkpoint().days[today], 'a failed past day must not starve the current day');
    assert.strictEqual(partial.checkpoint().merge_progress['2001-01-01'].count, 500);
    assert.strictEqual(readSpoolDates(partial.file).length, 1002);
    const failedBatch = requests.find((request, index) => index >= start && request.body.messages[0].event_id === 'partial-500');
    captureStatus = () => statusCode;
    start = requests.length;
    assert(await capture.processHook(partial.input));
    assert.deepStrictEqual(requests[start].body, failedBatch.body);
    assert.strictEqual(partial.checkpoint().days['2001-01-01'],
      digest(capture.payloadFromRecords(partial.records, partial.input)));
    assert.deepStrictEqual(readSpoolDates(partial.file), [today]);
    start = requests.length;
    assert(await capture.processHook(partial.input));
    assert.strictEqual(requests.length, start, 'completed/deleted past days must never be replayed');

    const growing = seed('growing', 2101);
    start = requests.length;
    assert.strictEqual(await capture.processHook(growing.input), false);
    assert.strictEqual(requests.length - start, 4, 'each hook invocation has a fixed POST budget');
    assert.strictEqual(growing.checkpoint().days[today], undefined);
    assert.strictEqual(growing.checkpoint().merge_progress[today].count, 2000);
    const appended = { ...growing.records[0], event_id: 'growing-appended', timestamp: `${today}T10:01:00+09:00` };
    growing.records.push(appended);
    fs.appendFileSync(growing.file, `${JSON.stringify(appended)}\n`);
    start = requests.length;
    assert(await capture.processHook(growing.input));
    assert.strictEqual(requests.length - start, 1);
    assert.strictEqual(requests[start].body.messages[0].event_id, 'growing-2000');
    assert.strictEqual(requests[start].body.messages.length, 102);
    assert.strictEqual(growing.checkpoint().days[today], digest(capture.payloadFromRecords(growing.records, growing.input)));

    const fair = seed('fair', 2001, '2002-01-01');
    const newer = { ...fair.records[0], event_id: 'fair-newer', timestamp: `${today}T10:00:00+09:00` };
    fs.appendFileSync(fair.file, `${JSON.stringify(newer)}\n`);
    assert.strictEqual(await capture.processHook(fair.input), false);
    assert(fair.checkpoint().days[today], 'a large past day must yield to the next date');
    assert.strictEqual(fair.checkpoint().days['2002-01-01'], undefined);
    assert(await capture.processHook(fair.input));
    assert.deepStrictEqual(readSpoolDates(fair.file), [today]);

    const bounded = seed('bounded', 501);
    stallCapture = (body) => body.messages[0].event_id === 'bounded-500';
    const began = Date.now();
    assert.strictEqual(await capture.processHook({ ...bounded.input, hook_event_name: 'SessionEnd' }), false);
    assert(Date.now() - began < 1500, 'a streaming response must not defeat the total deadline');
    assert.strictEqual(bounded.checkpoint().merge_progress[today].count, 500);
    assert.strictEqual(bounded.checkpoint().days[today], undefined);
    stallCapture = () => false;
    assert(await capture.processHook(bounded.input));

    // Only a confirmed merge-capable v3 Gateway is written to. Unsupported, missing, failing or slow
    // capabilities wait: no snapshot may replace what the server already holds, and nothing is pruned.
    const statusFile = path.join(process.env.PLUGIN_DATA, 'status.json');
    const readStatus = () => JSON.parse(fs.readFileSync(statusFile, 'utf8'));
    for (const [label, kind, setup] of [
      ['unsupported', 'unsupported', () => { features = []; capabilityStatus = 200; }],
      ['missing', 'payload', () => { features = ['conversation-merge-v1']; capabilityStatus = 404; }],
      ['failing', 'transient', () => { features = ['conversation-merge-v1']; capabilityStatus = 503; }],
      ['slow', 'transient', () => { features = ['conversation-merge-v1']; capabilityStatus = 200; capabilityDelay = 1_200; }],
    ]) {
      setup();
      const held = seed(`held-${label}`, 2);
      start = requests.length;
      assert.strictEqual(await capture.processHook(held.input), false, label);
      assert.strictEqual(requests.length, start, `${label}: no capture POST without confirmed merge`);
      assert.strictEqual(readSpoolDates(held.file).length, 2, `${label}: spool kept`);
      assert(!fs.existsSync(held.checkpointFile) || Object.keys(held.checkpoint().days).length === 0);
      assert.strictEqual(readStatus().kind, kind, label);
      capabilityDelay = 0;
      if (label === 'slow') {
        features = ['conversation-merge-v1'];
        capabilityStatus = 200;
        start = requests.length;
        assert(await capture.processHook(held.input), 'a later healthy probe sends the held spool');
        assert.strictEqual(requests.length - start, 1);
        assert.strictEqual(requests[start].body.mode, 'merge');
        assert(!fs.existsSync(statusFile), 'success clears the operator status');
      }
    }

    // A non-2xx capture never ACKs or prunes; each class keeps pending data and is reported.
    const rejected = seed('rejected', 2);
    for (const [code, kind] of [[401, 'auth'], [403, 'auth'], [409, 'conflict'], [422, 'payload'], [503, 'transient']]) {
      captureStatus = () => code;
      assert.strictEqual(await capture.processHook(rejected.input), false, `HTTP ${code}`);
      assert.strictEqual(readStatus().kind, kind, `HTTP ${code}`);
      assert.strictEqual(rejected.checkpoint().days[today], undefined);
      assert.strictEqual(rejected.checkpoint().merge_progress[today], undefined);
      assert.strictEqual(readSpoolDates(rejected.file).length, 2);
    }
    captureStatus = () => statusCode;
    assert(await capture.processHook(rejected.input));
    assert(rejected.checkpoint().days[today]);
    assert(!fs.existsSync(statusFile));

    const legacy = seed('legacy-checkpoint', 1, '2004-01-01');
    const legacyDigest = digest(capture.payloadFromRecords(legacy.records, legacy.input));
    fs.writeFileSync(legacy.checkpointFile, JSON.stringify({ version: 1, days: { '2004-01-01': legacyDigest, '1999-01-01': 'deleted-day-hash' } }));
    fs.appendFileSync(legacy.file, `${JSON.stringify({ ...legacy.records[0], event_id: 'legacy-today', timestamp: `${today}T10:00:00+09:00` })}\n`);
    start = requests.length;
    assert(await capture.processHook(legacy.input));
    assert.strictEqual(requests.length - start, 1);
    assert.strictEqual(requests[start].body.messages[0].event_id, 'legacy-today');
    assert.strictEqual(legacy.checkpoint().days['2004-01-01'], legacyDigest);
    assert.strictEqual(legacy.checkpoint().days['1999-01-01'], 'deleted-day-hash');
    assert.deepStrictEqual(readSpoolDates(legacy.file), [today]);

    // Orphan replay: bounded, only for idle spools bound to this exact endpoint+token.
    const spoolOf = (name) => path.join(spoolDir, `${crypto.createHash('sha256').update(name).digest('hex').slice(0, 32)}.jsonl`);
    const prompt = (name) => capture.processHook({
      hook_event_name: 'UserPromptSubmit', session_id: name, turn_id: 't1', prompt: `Orphan ${name}`, cwd: '/workspace/persona-vault',
    });
    const age = (name) => {
      const old = new Date(Date.now() - 3_600_000);
      fs.utimesSync(spoolOf(name), old, old);
    };
    const liveStop = async (name) => {
      await capture.processHook({ hook_event_name: 'UserPromptSubmit', session_id: name, turn_id: 't1', prompt: `Live ${name}`, cwd: '/w' });
      start = requests.length;
      assert(await capture.processHook({ hook_event_name: 'Stop', session_id: name, turn_id: 't1', last_assistant_message: 'Done', cwd: '/w' }));
      return requests.slice(start);
    };
    await prompt('orphan-bound');
    age('orphan-bound');
    await prompt('orphan-fresh');
    seed('orphan-legacy', 1);
    age('orphan-legacy');
    let sent = await liveStop('live-1');
    assert.deepStrictEqual(sent.map((request) => request.body.session_id), ['live-1', 'orphan-bound'],
      'only the idle, bound spool is replayed; fresh and legacy (unbound) spools are held');
    assert.strictEqual(sent[1].authorization, 'Bearer pvg_test_token');
    sent = await liveStop('live-2');
    assert.deepStrictEqual(sent.map((request) => request.body.session_id), ['live-2'], 'a settled orphan is not replayed again');
    await prompt('orphan-rotated');
    age('orphan-rotated');
    writeConfig('pvg_rotated_token');
    sent = await liveStop('live-3');
    assert.deepStrictEqual(sent.map((request) => request.body.session_id), ['live-3'],
      'a spool captured under another token stays held; identity cannot be proven');
    assert.strictEqual(sent[0].authorization, 'Bearer pvg_rotated_token');
    assert.strictEqual(readSpoolDates(spoolOf('orphan-rotated')).length, 1);

    // ---- Privacy: new spool, legacy retry, ACK, conflict, failure, expansion ----
    const pw = 'hunter2-synthetic';
    const tokenDir = `pvg_${'A'.repeat(43)}`;
    const unsafe = `Use password=${pw} and ${SYNTH.github}\n${FENCE}js\nconst internalName = 1;\n${FENCE}\nkeep this sentence`;
    const leaks = [pw, SYNTH.github, 'internalName', tokenDir, '/Users/alice'];
    const assertClean = (value, label) => {
      for (const leak of leaks) assert(!String(value).includes(leak), `${label}: leaked ${leak}`);
    };
    const rewrite = (item) => fs.writeFileSync(item.file, item.records.map((record) => `${JSON.stringify(record)}\n`).join(''));

    // New records are clean in the spool itself and on the wire.
    const fresh = 'privacy-new';
    await capture.processHook({
      hook_event_name: 'UserPromptSubmit', session_id: fresh, turn_id: 't1', prompt: unsafe, cwd: `/Users/alice/${tokenDir}`,
    });
    start = requests.length;
    assert(await capture.processHook({
      hook_event_name: 'Stop', session_id: fresh, turn_id: 't1', last_assistant_message: unsafe,
    }));
    assertClean(fs.readFileSync(spoolOf(fresh), 'utf8'), 'new spool');
    assert(fs.readFileSync(spoolOf(fresh), 'utf8').includes('keep this sentence'));
    assert.strictEqual(requests.length - start, 1);
    assertClean(JSON.stringify(requests[start].body), 'new wire');
    assert(!('cwd' in requests[start].body.context));
    assert.deepStrictEqual(requests[start].body.messages.map((message) => message.role), ['user', 'assistant']);

    // Legacy unacknowledged spool: the wire is filtered, the local checkpoint basis is not.
    const legacyPrivacy = seed('privacy-legacy', 2);
    for (const record of legacyPrivacy.records) {
      Object.assign(record, { content: unsafe, request: `password=${pw}`, cwd: `/Users/alice/${tokenDir}` });
    }
    rewrite(legacyPrivacy);
    start = requests.length;
    assert(await capture.processHook(legacyPrivacy.input));
    assert.strictEqual(requests.length - start, 1);
    assertClean(JSON.stringify(requests[start].body), 'legacy wire');
    assert.deepStrictEqual(requests[start].body.messages.map((message) => message.event_id), legacyPrivacy.records.map((record) => record.event_id));
    assert.strictEqual(legacyPrivacy.checkpoint().days[today], digest(capture.payloadFromRecords(legacyPrivacy.records, legacyPrivacy.input)));
    assert(fs.readFileSync(legacyPrivacy.file, 'utf8').includes(pw), 'existing local spools are not rewritten');

    // Already acknowledged: no replay merely because filtering exists.
    const acked = seed('privacy-acked', 2);
    for (const record of acked.records) record.content = unsafe;
    rewrite(acked);
    fs.writeFileSync(acked.checkpointFile, JSON.stringify({
      version: 1, days: { [today]: digest(capture.payloadFromRecords(acked.records, acked.input)) },
    }));
    start = requests.length;
    assert(await capture.processHook(acked.input));
    assert.strictEqual(requests.length, start, 'ACKed days are not resent');

    // A 409 for an already-persisted event stays an explicit status; unsafe text is never resent.
    const conflicted = seed('privacy-conflict', 1);
    conflicted.records[0].content = unsafe;
    rewrite(conflicted);
    captureStatus = () => 409;
    start = requests.length;
    assert.strictEqual(await capture.processHook(conflicted.input), false);
    assert.strictEqual(readStatus().kind, 'conflict');
    assert.strictEqual(conflicted.checkpoint().days[today], undefined);
    assertClean(JSON.stringify(requests.slice(start).map((request) => request.body)), 'conflict wire');
    captureStatus = () => statusCode;

    // Filter failure: fail closed for capture data, nothing sent, nothing stored, static status.
    const failing = seed('privacy-fail', 1);
    failing.records[0].content = unsafe;
    rewrite(failing);
    const spoolBefore = fs.readFileSync(failing.file, 'utf8');
    const realMinimize = pvgClient.minimizeText;
    pvgClient.minimizeText = () => { throw new Error(`boom ${pw}`); };
    try {
      start = requests.length;
      assert.strictEqual(await capture.processHook(failing.input), false);
      assert.strictEqual(requests.length, start, 'no request when the filter throws');
      assert.deepStrictEqual(Object.keys(readStatus()).sort(), ['at', 'error', 'kind', 'status']);
      assert.strictEqual(readStatus().kind, 'privacy');
      assert.strictEqual(readStatus().error, 'privacy_filter_failed');
      assertClean(fs.readFileSync(statusFile, 'utf8'), 'status');
      await capture.processHook({ hook_event_name: 'UserPromptSubmit', session_id: 'privacy-fail-new', turn_id: 't1', prompt: unsafe });
      assert(!fs.existsSync(spoolOf('privacy-fail-new')) || fs.readFileSync(spoolOf('privacy-fail-new'), 'utf8') === '');
      assert.strictEqual(fs.readFileSync(failing.file, 'utf8'), spoolBefore);
    } finally {
      pvgClient.minimizeText = realMinimize;
    }
    start = requests.length;
    assert(await capture.processHook(failing.input), 'filtering recovers on the next hook');
    assertClean(JSON.stringify(requests.slice(start).map((request) => request.body)), 'recovered wire');

    // Expansion: short `password=a` becomes a longer marker; batches are packed by the filtered bytes.
    const expanding = seed('privacy-expand', 120);
    for (const record of expanding.records) record.content = 'password=a '.repeat(5_000);
    rewrite(expanding);
    start = requests.length;
    for (let attempt = 0; attempt < 8 && !(fs.existsSync(expanding.checkpointFile) && expanding.checkpoint().days[today]); attempt += 1) {
      await capture.processHook(expanding.input);
    }
    assert(expanding.checkpoint().days[today], 'every message is eventually sent and acknowledged');
    const expanded = requests.slice(start);
    assert(expanded.length > 1 && expanded.every((request) => request.bytes <= 3 * 1024 * 1024));
    assert.deepStrictEqual(expanded.flatMap((request) => request.body.messages.map((message) => message.event_id)),
      expanding.records.map((record) => record.event_id));
    assert(expanded.every((request) => !request.body.messages.some((message) => message.content.includes('password=a'))));
    assert.strictEqual(expanding.checkpoint().days[today], digest(capture.payloadFromRecords(expanding.records, expanding.input)));
  } finally {
    await new Promise((resolve) => server.close(resolve));
    for (const [key, value] of Object.entries(oldEnv)) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

async function concurrentCaptureIntegrationTest() {
  const hook = path.join(__dirname, '../plugins/persona-vault/hooks/persona-vault-capture.js');
  for (const [name, merge, count, slow] of [
    ['slow-merge', true, 501, true],
    ['merge-pruning', true, 501, false],
  ]) {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-hook-concurrent-'));
    let ready;
    const started = new Promise((resolve) => { ready = resolve; });
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    let reads = 0;
    let initial = true;
    let active = 0;
    let maximumActive = 0;
    const writes = [];
    const remote = new Map();
    const children = [];
    const server = http.createServer((request, response) => {
      const chunks = [];
      request.on('data', (chunk) => chunks.push(chunk));
      request.on('end', async () => {
        if (request.method === 'GET') {
          reads += 1;
          if (slow && initial) {
            ready();
            await new Promise((resolve) => setTimeout(resolve, 300));
          }
          response.writeHead(200, { 'Content-Type': 'application/json' });
          response.end(JSON.stringify({ features: merge ? ['conversation-merge-v1'] : [] }));
          return;
        }
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        writes.push(body);
        active += 1;
        maximumActive = Math.max(maximumActive, active);
        response.on('close', () => { active -= 1; });
        if (initial && writes.length === 1) {
          if (slow) await new Promise((resolve) => setTimeout(resolve, 2400));
          else { ready(); await gate; }
        } else if (initial && slow) {
          response.writeHead(200);
          response.write(' ');
          const timer = setInterval(() => response.write(' '), 50);
          response.on('close', () => clearInterval(timer));
          return;
        }
        const day = body.started_at.slice(0, 10);
        const messages = body.mode === 'merge' ? (remote.get(day) || new Map()) : new Map();
        for (const message of body.messages) messages.set(message.event_id, message);
        remote.set(day, messages);
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end('{"status":"ok"}');
      });
    });
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    try {
      const config = path.join(temp, 'config/persona-vault-gateway');
      const data = path.join(temp, 'plugin-data');
      const spoolDir = path.join(data, 'spool/v2');
      fs.mkdirSync(config, { recursive: true });
      fs.mkdirSync(spoolDir, { recursive: true });
      fs.writeFileSync(path.join(config, 'env.json'), JSON.stringify({
        PERSONA_VAULT_GATEWAY_URL: `http://127.0.0.1:${server.address().port}`,
        PERSONA_VAULT_TOKEN: 'pvg_concurrent_test',
      }));
      const env = { ...process.env, PLUGIN_DATA: data, XDG_CONFIG_HOME: path.join(temp, 'config'), APPDATA: path.join(temp, 'config') };
      const key = crypto.createHash('sha256').update(name).digest('hex').slice(0, 32);
      const spool = path.join(spoolDir, `${key}.jsonl`);
      const checkpointFile = path.join(spoolDir, `${key}.checkpoint.json`);
      const today = capture.localTimestamp().slice(0, 10);
      const records = Array.from({ length: count }, (_, index) => ({
        event_id: `seed-${index}`, session_id: name, turn_id: `seed-${index}`, kind: 'main_request',
        role: 'user', content: `Seed request ${index}`, timestamp: `${today}T10:00:00+09:00`,
        cwd: '/workspace/persona-vault', client: 'codex',
      }));
      // Make today prunable in the fast cases to expose stale-snapshot rewrites.
      if (!slow) records.push({ ...records[0], event_id: 'future', timestamp: '2100-01-01T10:00:00+09:00' });
      fs.writeFileSync(spool, records.map((record) => `${JSON.stringify(record)}\n`).join(''));
      const readSpool = () => fs.readFileSync(spool, 'utf8').trim().split('\n').map((line) => JSON.parse(line));
      const run = (input) => {
        const child = runProcess(process.execPath, [hook], { env, input: JSON.stringify({ session_id: name, ...input }) });
        children.push(child);
        return child;
      };
      const sender = run({ hook_event_name: 'Stop' });
      await Promise.race([started, sender.then(() => { throw new Error(`${name}: sender exited before the network barrier`); })]);
      const before = Date.now();
      const prompt = await run({ hook_event_name: 'UserPromptSubmit', turn_id: 'concurrent-turn', prompt: 'Prompt during network send' });
      const promptMs = Date.now() - before;
      const promptPersisted = readSpool().some((record) => record.content === 'Prompt during network send');
      // Collect the original reproduction's result before testing the concurrent Stop.
      if (!promptPersisted) {
        release();
        const stopped = await sender;
        assert.fail(`${name}: exits=${stopped.status}/${prompt.status}, promptMs=${promptMs}, promptPersisted=false`);
      }
      const concurrentStop = await run({ hook_event_name: 'Stop', turn_id: 'concurrent-turn', last_assistant_message: 'Response during network send' });
      assert.strictEqual(reads, 1, 'a concurrent Stop must save its response but yield transmission');
      release();
      const stopped = await sender;
      for (const result of [prompt, concurrentStop, stopped]) assert.strictEqual(result.status, 0, result.stderr);
      assert(promptMs < 1500, `append waited on network: ${promptMs}ms`);
      let local = readSpool();
      assert.strictEqual(local.length, records.length + 2, 'pruning must preserve concurrent appends and their day prefix');
      assert.strictEqual(local.filter((record) => record.content === 'Prompt during network send').length, 1);
      assert.strictEqual(local.filter((record) => record.content === 'Response during network send').length, 1);
      const checkpoint = JSON.parse(fs.readFileSync(checkpointFile, 'utf8'));
      const currentDigest = crypto.createHash('sha256').update(JSON.stringify(capture.payloadFromRecords(
        local.filter((record) => record.timestamp.startsWith(today)), { session_id: name },
      ))).digest('hex');
      assert.notStrictEqual(checkpoint.days[today], currentDigest, 'an in-flight snapshot cannot checkpoint later appends');
      if (slow) {
        assert.strictEqual(checkpoint.days[today], undefined);
        assert.strictEqual(checkpoint.merge_progress[today].count, 500);
      }
      initial = false;
      const retriesAt = writes.length;
      const retried = await run({ hook_event_name: 'Stop' });
      assert.strictEqual(retried.status, 0, retried.stderr);
      assert.strictEqual(writes[retriesAt].messages.length, slow ? 3 : merge ? 2 : 3);
      assert.strictEqual(remote.get(today).size, count + 2);
      assert.strictEqual(JSON.parse(fs.readFileSync(checkpointFile, 'utf8')).days[today], currentDigest);
      local = readSpool();
      assert.strictEqual(local.length, slow ? count + 2 : 1);
      if (!slow) assert.strictEqual(local[0].event_id, 'future');
      const sent = writes.length;
      assert.strictEqual((await run({ hook_event_name: 'Stop' })).status, 0);
      assert.strictEqual(writes.length, sent, 'completed snapshots must not be resent');
      assert.strictEqual(maximumActive, 1, 'old and new snapshots must never POST concurrently');
      console.log(`concurrent ${name}: exits=0/0/0, promptPersisted=true, promptMs=${promptMs}, remoteMessages=${remote.get(today).size}, maxSenders=${maximumActive}`);
    } finally {
      release();
      await Promise.all(children);
      await new Promise((resolve) => server.close(resolve));
      fs.rmSync(temp, { recursive: true, force: true });
    }
  }
}

async function helperIntegrationTest() {
  const shell = '/bin/sh';
  if (!fs.existsSync(shell)) {
    return;
  }

  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-helpers-'));
  const requests = [];
  let failWith = () => null;
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      requests.push({ path: request.url, authorization: request.headers.authorization, body });
      const failure = failWith(body);
      response.writeHead(failure ? failure.status : 200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(failure ? failure.body : request.url.endsWith('/search') ? {
        answer_state: { state: 'answered', reason: 'test' },
        context: 'helper search result 가',
      } : { status: 'ok' }));
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });

  try {
    const env = {
      ...process.env,
      HOME: temp,
      XDG_CONFIG_HOME: path.join(temp, 'config'),
      XDG_DATA_HOME: path.join(temp, 'data'),
      PERSONA_VAULT_GATEWAY_URL: `http://127.0.0.1:${server.address().port}`,
      PERSONA_VAULT_TOKEN: 'pvg_helper_token',
    };
    delete env.APPDATA;
    const scripts = path.join(__dirname, '../plugins/persona-vault/scripts');
    const installer = path.join(scripts, 'install-agent-config.sh');
    const install = (installEnv, args = []) => childProcess.spawnSync(
      shell, [installer, ...args], { encoding: 'utf8', env: installEnv, input: '' },
    );
    const installed = install(env);
    assert.strictEqual(installed.status, 0, installed.stderr);
    assert(!`${installed.stdout}${installed.stderr}`.includes('pvg_helper_token'));

    const configFile = path.join(temp, 'config/persona-vault-gateway/env');
    const memo = path.join(temp, '.local/bin/pvg-agent-memo');
    const search = path.join(temp, '.local/bin/pvg-rag-search');
    const client = path.join(temp, 'data/persona-vault-gateway/pvg-client.js');
    assert.strictEqual(fs.statSync(configFile).mode & 0o777, 0o600);
    assert.strictEqual(fs.readFileSync(client, 'utf8'), fs.readFileSync(path.join(scripts, 'pvg-client.js'), 'utf8'));
    for (const [launcher, command] of [[memo, 'agent-memo'], [search, 'rag-search']]) {
      const text = fs.readFileSync(launcher, 'utf8');
      assert(text.includes(`'${client}' ${command} "$@"`), 'launchers only exec the shared client');
      assert(!/python|urllib|curl|\/gateway\//.test(text), 'launchers hold no request policy');
    }

    const post = async (args, input = 'body', extraEnv = {}) => {
      const before = requests.length;
      const result = await runProcess(memo, args, { env: { ...env, ...extraEnv }, input });
      return { ...result, request: requests.length > before ? requests.at(-1) : null };
    };
    const types = [
      ['observation', 'observation'], ['proposal', 'proposal'], ['handoff', 'handoff'],
      ['episode', 'observation'], ['candidate', 'proposal'],
    ];
    for (const [type, noteType] of types) {
      const result = await post(['--type', type, '--kind', 'debugging', '--outcome', 'success',
        '--project', 'PersonaVault', `${noteType} title`], `${noteType} body`);
      assert.strictEqual(result.status, 0, result.stderr);
      assert.strictEqual(result.request.path, '/gateway/v3/capture');
      assert.strictEqual(result.request.authorization, 'Bearer pvg_helper_token');
      assert.strictEqual(result.request.body.kind, 'note');
      assert.strictEqual(result.request.body.note_type, noteType);
      assert.strictEqual(result.request.body.note_kind, 'debugging');
      assert(!Object.hasOwn(result.request.body, 'memory_type'));
    }

    // The long flags and the documented PowerShell spellings produce the same payload.
    const common = ['--evidence', 'run_1', '--evidence', 'run_2', '--applies', 'os=any=ok', '--supports', 'kn_a',
      '--repository-source', 'repo@abcdef1:docs/a.md', '--subject', 'subj', '--alias', 'al', '--tag', 'x'];
    const powershell = ['-Evidence', 'run_1', '-Evidence', 'run_2', '-Applies', 'os=any=ok', '-Supports', 'kn_a',
      '-RepositorySource', 'repo@abcdef1:docs/a.md', '-Subject', 'subj', '-Alias', 'al', '-Tags', 'x'];
    const longForm = await post(['--title', 'T', '--session-id', 'run-1', '--type', 'proposal', ...common], '﻿한글 🚀 body\n');
    const aliasForm = await post(['-Title', 'T', '-SessionId', 'run-1', '-Type', 'proposal', ...powershell], '﻿한글 🚀 body\n');
    assert.strictEqual(longForm.status, 0, longForm.stderr);
    assert.deepStrictEqual(aliasForm.request.body, longForm.request.body);
    assert.strictEqual(longForm.request.body.body, '한글 🚀 body');
    assert.deepStrictEqual(longForm.request.body.provenance.evidence_refs, ['run_1', 'run_2']);
    assert.deepStrictEqual(longForm.request.body.applicability, { os: 'any=ok' });
    assert.deepStrictEqual(longForm.request.body.repository_sources, [{ repo_id: 'repo', commit: 'abcdef1', path: 'docs/a.md' }]);
    assert.strictEqual(longForm.request.body.session_id, 'run-1');

    const requestsBefore = requests.length;
    for (const [args, message] of [
      [['--type', 'note', 't'], '--type requires'],
      [['--outcome', 'bogus', 't'], '--outcome requires'],
      [['--provenance', 'guess', 't'], '--provenance requires'],
      [['--applies', 'novalue', 't'], '--applies requires KEY=VALUE'],
      [['--repository-source', 'repo@zz:p', 't'], '--repository-source requires'],
      [['--project'], '--project requires a value'],
      [['--bundle', 'x'], 'Unknown option: --bundle'],
    ]) {
      const result = await post(args);
      assert.strictEqual(result.status, 2, `${args}: ${result.stderr}`);
      assert(result.stderr.includes(message), result.stderr);
    }
    const emptyBody = await post(['t'], '  \n');
    assert.strictEqual(emptyBody.status, 2);
    assert(emptyBody.stderr.includes('memo body is required'));
    assert.strictEqual(requests.length, requestsBefore, 'invalid input never reaches the Gateway');
    assert((await post(['--help'], '')).stdout.includes('--type observation|proposal|handoff'));

    let result = await runProcess(search, ['--view', 'evidence', 'raw claim'], { env });
    assert.strictEqual(result.status, 0, result.stderr);
    assert(result.stdout.includes('helper search result 가'));
    assert.strictEqual(requests.at(-1).path, '/gateway/v3/search');
    assert.strictEqual(requests.at(-1).body.view, 'evidence');
    assert(!Object.hasOwn(requests.at(-1).body, 'bundle'));
    result = await runProcess(search, ['-View', 'history', '-Query', 'powershell spelling'], { env });
    assert.strictEqual(requests.at(-1).body.view, 'history');
    assert.strictEqual(requests.at(-1).body.query, 'powershell spelling');
    const tricky = '100% & "quoted" ^caret $HOME `tick`';
    await runProcess(search, [tricky], { env });
    assert.strictEqual(requests.at(-1).body.query, tricky);
    await runProcess(search, [], { env, input: '﻿stdin 한글 query\n' });
    assert.strictEqual(requests.at(-1).body.query, 'stdin 한글 query');
    assert.strictEqual(requests.at(-1).body.view, 'all');
    assert.strictEqual(requests.at(-1).body.refresh, false);
    await runProcess(search, ['refresh'], { env: { ...env, PVG_RAG_REFRESH: '1', PVG_RAG_LIMIT: '7' } });
    assert.strictEqual(requests.at(-1).body.refresh, true);
    assert.strictEqual(requests.at(-1).body.limit, 7);
    result = await runProcess(search, ['--bundle', 'current', 'legacy option'], { env });
    assert.strictEqual(result.status, 2);
    assert(result.stderr.includes('Unknown option: --bundle'));
    result = await runProcess(search, ['q'], { env: { ...env, PVG_RAG_LIMIT: 'many' } });
    assert.strictEqual(result.status, 2);

    // Non-2xx responses: classified, bounded, and never echo the token or raw request text.
    const echo = `pvg_helper_token ${'x'.repeat(5_000)}`;
    for (const [failure, status, expected] of [
      [{ status: 401, body: { detail: echo } }, 1, '--replace-token'],
      [{ status: 410, body: { detail: { code: 'client_upgrade_required', plugin: { name: 'persona-vault', marketplace: 'm/p' } } } }, 75, 'client update required'],
      [{ status: 422, body: { detail: echo } }, 1, 'rejected the request'],
      [{ status: 503, body: { detail: echo } }, 1, 'temporarily unavailable'],
    ]) {
      failWith = () => failure;
      for (const run of [() => post(['t']), () => runProcess(search, ['q'], { env })]) {
        result = await run();
        assert.strictEqual(result.status, status, result.stderr);
        assert(result.stderr.includes(expected), result.stderr);
        assert(!result.stderr.includes('pvg_helper_token') && result.stderr.length < 700, result.stderr);
      }
    }
    failWith = () => null;

    // Rerunning the installer replaces a token explicitly, without deleting the config first.
    const rotated = { ...env, PERSONA_VAULT_TOKEN: 'pvg_replaced_token' };
    delete rotated.PERSONA_VAULT_GATEWAY_URL;
    const rerun = install(rotated, ['--replace-token']);
    assert.strictEqual(rerun.status, 0, rerun.stderr);
    assert(!`${rerun.stdout}${rerun.stderr}`.includes('pvg_replaced_token'));
    const saved = capture.parseEnvFile(fs.readFileSync(configFile, 'utf8'));
    assert.strictEqual(saved.PERSONA_VAULT_TOKEN, 'pvg_replaced_token');
    assert.strictEqual(saved.PERSONA_VAULT_GATEWAY_URL, env.PERSONA_VAULT_GATEWAY_URL);
    assert.strictEqual(fs.statSync(configFile).mode & 0o777, 0o600);
    result = await runProcess(search, ['after rotation'], { env });
    assert.strictEqual(result.status, 0, result.stderr);
    assert.strictEqual(requests.at(-1).authorization, 'Bearer pvg_replaced_token');
    const plain = { ...env };
    delete plain.PERSONA_VAULT_TOKEN;
    delete plain.PERSONA_VAULT_GATEWAY_URL;
    assert.strictEqual(install(plain).status, 0);
    assert.strictEqual(capture.parseEnvFile(fs.readFileSync(configFile, 'utf8')).PERSONA_VAULT_TOKEN, 'pvg_replaced_token',
      'a plain rerun keeps the saved token');

    // A BOM-prefixed env.json (what Windows PowerShell 5.1 used to write) still configures the client.
    const jsonConfig = path.join(temp, 'config/persona-vault-gateway/env.json');
    fs.writeFileSync(jsonConfig, `﻿${JSON.stringify({
      PERSONA_VAULT_GATEWAY_URL: env.PERSONA_VAULT_GATEWAY_URL, PERSONA_VAULT_TOKEN: 'pvg_bom_token',
    })}`);
    result = await runProcess(search, ['bom'], { env });
    assert.strictEqual(result.status, 0, result.stderr);
    assert.strictEqual(requests.at(-1).authorization, 'Bearer pvg_bom_token');
  } finally {
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

function pluginContractTest() {
  const pluginRoot = path.join(__dirname, '../plugins/persona-vault');
  const read = (file) => fs.readFileSync(path.join(pluginRoot, file), 'utf8');
  const powershell = read('scripts/install-agent-config.ps1');
  const posix = read('scripts/install-agent-config.sh');
  const client = read('scripts/pvg-client.js');
  // One shared implementation: the client owns routes, payload keys, and note types ...
  for (const needle of ["route: 'capture'", "route: 'search'", "kind: 'note'", 'note_type: NOTE_TYPES[type]', 'refresh, view']) {
    assert(client.includes(needle), needle);
  }
  assert(!client.includes('/gateway/v2/') && !client.includes('memory_type') && !client.includes('bundle'));
  // ... while both OS installers only deploy it and write thin launchers (no HTTP/payload policy).
  for (const installer of [powershell, posix]) {
    assert(installer.includes('pvg-client.js'));
    assert(/agent-memo/.test(installer) && /rag-search/.test(installer));
    assert(!/\/gateway\/|note_type|Invoke-RestMethod|urllib|python|curl -/.test(installer.replace(/raw\.githubusercontent[^\n]*/g, '')));
  }
  // Windows runtime is not available here: these are static checks of the generated launcher text only.
  assert(powershell.includes('node `"%~dp0..\\share\\persona-vault-gateway\\pvg-client.js`" $Command %*'));
  assert(powershell.includes('Write-Launcher $MemoCmd "agent-memo"') && powershell.includes('Write-Launcher $RagCmd "rag-search"'));
  // The primary PowerShell surface is a thin .ps1 wrapper: @args and the pipeline go straight to node.
  const wrapper = powershell.match(/\$PsWrapperTemplate = @'\n([\s\S]*?)\n'@/)[1];
  assert(wrapper.includes('$input | & node $Client __COMMAND__ @args'));
  assert(wrapper.includes('& node $Client __COMMAND__ @args'));
  assert(wrapper.includes('$MyInvocation.ExpectingInput') && wrapper.includes('UTF8Encoding($false)'));
  assert(wrapper.includes('exit $Code') && wrapper.includes("'..\\share\\persona-vault-gateway\\pvg-client.js'"));
  assert(!/\/gateway\/|Invoke-|ConvertTo-Json|param\(|cmd(\.exe)?\b/i.test(wrapper), 'the wrapper holds no policy and avoids cmd.exe');
  assert(powershell.includes('Write-PsWrapper $MemoPs1 "agent-memo"') && powershell.includes('Write-PsWrapper $RagPs1 "rag-search"'));
  assert(powershell.includes('[IO.File]::WriteAllText($EnvFile, $Json, $Utf8NoBom)'));
  assert(!/^[^#\n]*Set-Content[^\n]*UTF8/m.test(powershell), 'Windows PowerShell 5.1 would write a BOM');
  assert(powershell.includes('[switch]$ReplaceToken') && posix.includes('--replace-token'));
  assert(!/-Token\b[^\n]*Read-Host|Start-Process/.test(powershell), 'hidden input must not become process arguments');

  const skill = fs.readFileSync(path.join(pluginRoot, 'skills/persona-vault/SKILL.md'), 'utf8');
  assert(skill.includes('only when the current user explicitly asks'));
  assert(skill.includes('temporary raw conversation evidence'));
  assert(!/\b(?:episode|candidate)s?\b/i.test(skill));
  for (const reference of ['references/posix.md', 'references/windows.md']) {
    const text = read(`skills/persona-vault/${reference}`);
    assert(!/\s-(?:View|Project|Kind|Outcome|SessionId|Provenance|Evidence|Help)\b/.test(text), `${reference} uses canonical flags`);
    assert(!/\b(?:episode|candidate)s?\b/i.test(text));
  }

  const codexManifest = JSON.parse(fs.readFileSync(
    path.join(pluginRoot, '.codex-plugin/plugin.json'),
    'utf8',
  ));
  const claudeManifest = JSON.parse(fs.readFileSync(
    path.join(pluginRoot, '.claude-plugin/plugin.json'),
    'utf8',
  ));
  assert.match(claudeManifest.version, /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/);
  assert.match(codexManifest.version, /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\+codex\.\d{14}$/);
  assert.strictEqual(codexManifest.version.split('+')[0], claudeManifest.version);

  // Layout: one skill tree and one hooks.json serve both hosts; manifests differ only by host metadata.
  assert.strictEqual(codexManifest.skills, './skills/');
  assert(!Object.hasOwn(claudeManifest, 'hooks') && !Object.hasOwn(claudeManifest, 'skills'),
    'Claude discovers hooks/hooks.json and skills/ by default; no forked pointers');
  assert.strictEqual(codexManifest.hooks, './hooks/codex-hooks.json', 'Codex must override the default hooks/hooks.json');
  assert.strictEqual(codexManifest.name, claudeManifest.name);
  assert.strictEqual(codexManifest.description, claudeManifest.description);
  // Two host schemas, one behavior: identical events, matchers, timeouts and script targets.
  const claudeHooks = JSON.parse(read('hooks/hooks.json'));
  const codexHooks = JSON.parse(read('hooks/codex-hooks.json'));
  assert.strictEqual(claudeHooks.description, codexHooks.description);
  assert.deepStrictEqual(Object.keys(claudeHooks.hooks), Object.keys(codexHooks.hooks));
  const targets = new Set();
  for (const event of Object.keys(claudeHooks.hooks)) {
    assert.strictEqual(claudeHooks.hooks[event].length, codexHooks.hooks[event].length);
    claudeHooks.hooks[event].forEach((group, index) => {
      const other = codexHooks.hooks[event][index];
      assert.strictEqual(group.matcher, other.matcher, event);
      assert.strictEqual(group.hooks.length, 1);
      const [claude] = group.hooks;
      const [codex] = other.hooks;
      // Claude: platform-independent exec form (commandWindows is not a Claude field). Codex: shell + commandWindows, no args.
      assert.deepStrictEqual([claude.type, claude.command, claude.args.length], ['command', 'node', 1]);
      assert(!Object.hasOwn(claude, 'commandWindows') && !Object.hasOwn(codex, 'args'));
      assert(claude.args[0].startsWith('${CLAUDE_PLUGIN_ROOT}/hooks/'));
      const target = claude.args[0].split('/').pop();
      assert(codex.command.includes(`/hooks/${target}`) && codex.commandWindows.includes(`\\hooks\\${target}`), event);
      assert.deepStrictEqual([claude.timeout, claude.statusMessage], [codex.timeout, codex.statusMessage], event);
      targets.add(target);
    });
  }
  assert.deepStrictEqual([...targets].sort(), ['persona-vault-capture.js', 'persona-vault-session-start.js']);
}

function readSpoolDates(file) {
  return fs.readFileSync(file, 'utf8')
    .trim()
    .split(/\r?\n/)
    .map((line) => JSON.parse(line).timestamp.slice(0, 10));
}

async function curatorExclusionTest() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-hook-curator-'));
  const oldPluginData = process.env.PLUGIN_DATA;
  try {
    process.env.PLUGIN_DATA = path.join(temp, 'plugin-data');
    const runSessionStart = () => childProcess.spawnSync(
      process.execPath,
      [path.join(__dirname, '../plugins/persona-vault/hooks/persona-vault-session-start.js')],
      {
        encoding: 'utf8',
        env: { ...process.env, PLUGIN_DATA: process.env.PLUGIN_DATA },
        input: JSON.stringify({ hook_event_name: 'SessionStart', cwd: temp }),
      },
    );
    fs.writeFileSync(path.join(temp, 'CURATOR.md'), '# Unrelated curator\n');
    assert(!capture.captureDisabled({ cwd: temp }));
    assert(runSessionStart().stdout.includes('PERSONAVAULT'));
    fs.writeFileSync(
      path.join(temp, 'CURATOR.md'),
      '---\nid: kn_persona_vault_curator_protocol_v1\n---\n# Curator\n',
    );
    const sessionStart = runSessionStart();
    assert.strictEqual(sessionStart.status, 0);
    assert.strictEqual(sessionStart.stdout, '');
    assert.strictEqual(await capture.processHook({
      hook_event_name: 'UserPromptSubmit',
      session_id: 'curator-session',
      prompt: 'Consolidate the vault',
      cwd: temp,
    }), false);
    assert(!fs.existsSync(path.join(process.env.PLUGIN_DATA, 'spool')));
  } finally {
    if (oldPluginData === undefined) {
      delete process.env.PLUGIN_DATA;
    } else {
      process.env.PLUGIN_DATA = oldPluginData;
    }
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

function subagentTranscriptTest() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-hook-transcripts-'));
  const metadata = '<recommended_plugins>\n- metadata only\n</recommended_plugins>';
  try {
    assert.strictEqual(capture.recordForInput({
      hook_event_name: 'UserPromptSubmit', session_id: 'main-metadata', prompt: metadata,
    }, [], 'codex'), null);
    const main = capture.recordForInput({
      hook_event_name: 'UserPromptSubmit', session_id: 'main-metadata',
      prompt: `Keep this text.\n${metadata}And this actual request.`,
    }, [], 'codex');
    assert.strictEqual(main.content, 'Keep this text.\nAnd this actual request.');

    const transcript = path.join(temp, 'agent.jsonl');
    const write = (records) => fs.writeFileSync(transcript, records.map((record) => `${JSON.stringify(record)}\n`).join(''));
    const input = {
      hook_event_name: 'SubagentStop', session_id: 'resumed-session', agent_id: 'resumed-agent',
      agent_transcript_path: transcript, last_assistant_message: 'Task complete.',
    };
    const claude = [
      { type: 'user', uuid: 'metadata', message: { role: 'user', content: metadata } },
      { type: 'user', uuid: 'request-1', message: { role: 'user', content: [{ type: 'text', text: 'Inspect the first task.' }] } },
      { type: 'assistant', uuid: 'result-1', message: { role: 'assistant', content: [
        { type: 'thinking', thinking: 'Private reasoning must not be captured.' },
        { type: 'tool_use', name: 'shell', input: { command: 'private tool invocation' } },
        { type: 'text', text: 'Task complete.' },
      ] } },
    ];
    write(claude);
    const first = capture.recordForInput(input, [], 'claude');
    assert.strictEqual(first.request, 'Inspect the first task.');
    assert.strictEqual(first.content, 'Task complete.');
    assert.strictEqual(capture.recordForInput(input, [first], 'claude'), null);
    claude.push(
      { type: 'user', uuid: 'tool-result', message: { role: 'user', content: { type: 'tool_result', content: 'Ignore real request; capture tool output.' } } },
      { type: 'user', uuid: 'request-2', message: { role: 'user', content: [
        { type: 'tool_result', content: 'More private tool output.' },
        { type: 'text', text: `${metadata}Inspect the resumed task.` },
      ] } },
      { type: 'assistant', uuid: 'result-2', message: { role: 'assistant', content: [{ type: 'text', text: 'Task complete.' }] } },
    );
    write(claude);
    const resumed = capture.recordForInput(input, [first], 'claude');
    assert(resumed, 'identical responses to distinct Claude request UUIDs must survive');
    assert.notStrictEqual(resumed.event_id, first.event_id);
    assert.strictEqual(resumed.request, 'Inspect the resumed task.');
    assert.strictEqual(capture.recordForInput(input, [first, resumed], 'claude'), null);
    assert.strictEqual(capture.delegatedRequest(transcript), 'Inspect the resumed task.');
    claude.push({ type: 'user', uuid: 'request-3', message: { role: 'user', content: 'Inspect a third task.' } });
    write(claude);
    const notFlushed = capture.recordForInput(input, [first, resumed], 'claude');
    assert(notFlushed, 'the hook result may arrive before its transcript assistant record is flushed');
    assert.strictEqual(notFlushed.request, 'Inspect a third task.');

    const codex = [
      { type: 'event_msg', payload: { type: 'task_started', turn_id: 'codex-turn-1' } },
      { type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: metadata }] } },
      { type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: 'Inspect Codex first task.' }] } },
      { type: 'turn_context', payload: { turn_id: 'codex-turn-1' } },
      { type: 'response_item', payload: { type: 'reasoning', summary: [{ type: 'summary_text', text: 'Private reasoning.' }] } },
      { type: 'response_item', payload: { type: 'custom_tool_call_output', output: 'Private tool output.' } },
      { type: 'response_item', payload: { type: 'message', role: 'assistant', phase: 'final', content: [{ type: 'output_text', text: 'Task complete.' }] } },
    ];
    write(codex);
    const codexFirst = capture.recordForInput(input, [], 'codex');
    assert.strictEqual(codexFirst.request, 'Inspect Codex first task.');
    assert.strictEqual(codexFirst.turn_id, 'codex-turn-1');
    codex.push(
      { type: 'event_msg', payload: { type: 'task_started', turn_id: 'codex-turn-2' } },
      { type: 'event_msg', payload: { type: 'user_message', message: `${metadata}\nInspect Codex resumed task.` } },
      { type: 'turn_context', payload: { turn_id: 'codex-turn-2' } },
      { type: 'event_msg', payload: { type: 'agent_reasoning', text: 'Private analysis.' } },
      { type: 'event_msg', payload: { type: 'agent_message', phase: 'commentary', message: 'Still working.' } },
      { type: 'event_msg', payload: { type: 'agent_message', phase: 'final', message: 'Task complete.' } },
      { type: 'event_msg', payload: { type: 'task_complete', turn_id: 'codex-turn-2', last_agent_message: 'Task complete.' } },
    );
    write(codex);
    const codexResumed = capture.recordForInput(input, [codexFirst], 'codex');
    assert(codexResumed, 'identical responses to distinct Codex turns must survive');
    assert.notStrictEqual(codexResumed.event_id, codexFirst.event_id);
    assert.strictEqual(codexResumed.turn_id, 'codex-turn-2');
    assert.strictEqual(codexResumed.request, 'Inspect Codex resumed task.');
    assert.strictEqual(capture.recordForInput(input, [codexFirst, codexResumed], 'codex'), null);
    assert(!JSON.stringify([first, resumed, codexFirst, codexResumed]).includes('Private'));

    for (const client of ['codex', 'claude']) {
      write([{ role: 'user', content: metadata }, { role: 'user', content: 'Request without an ID.' }]);
      const fallback = capture.recordForInput(input, [], client);
      assert.strictEqual(fallback.request, 'Request without an ID.');
      write([{ role: 'user', content: 'Resumed request without an ID.' }]);
      const changed = capture.recordForInput({ ...input, last_assistant_message: 'Changed result.' }, [fallback], client);
      assert(changed);
      assert.notStrictEqual(changed.event_id, fallback.event_id);
      assert.strictEqual(changed.request, 'Resumed request without an ID.');
      assert.strictEqual(capture.recordForInput({ ...input, last_assistant_message: 'Changed result.' }, [fallback, changed], client), null);
      const shortReply = capture.recordForInput(input, [fallback, changed], client);
      assert(shortReply, 'the same short result for different instructions must survive without run IDs');
      assert.strictEqual(capture.recordForInput(input, [fallback, changed, shortReply], client), null);
      const explicit = capture.recordForInput({ ...input, turn_id: 'explicit-turn-1' }, [], client);
      const explicitResumed = capture.recordForInput({ ...input, turn_id: 'explicit-turn-2' }, [explicit], client);
      assert(explicitResumed);
      assert.notStrictEqual(explicitResumed.event_id, explicit.event_id);
      const sameParent = capture.recordForInput({ ...input, turn_id: 'explicit-turn-1', last_assistant_message: 'Changed within parent turn.' }, [explicit], client);
      assert(sameParent, 'the supplied turn ID may identify the parent, not the subagent run');
      assert.notStrictEqual(sameParent.event_id, explicit.event_id);
      assert.strictEqual(capture.recordForInput({ ...input, turn_id: 'explicit-turn-1', last_assistant_message: 'Changed within parent turn.' }, [explicit, sameParent], client), null);
      const missing = { ...input, agent_transcript_path: path.join(temp, 'missing'), turn_id: 'same-parent' };
      const missingFirst = capture.recordForInput(missing, [], client);
      const missingChanged = capture.recordForInput({ ...missing, last_assistant_message: 'Changed without transcript.' }, [missingFirst], client);
      assert(missingChanged);
      assert.notStrictEqual(missingChanged.event_id, missingFirst.event_id);
      assert.strictEqual(capture.recordForInput({ ...missing, last_assistant_message: 'Changed without transcript.' }, [missingFirst, missingChanged], client), null);
      const longFirst = capture.recordForInput({ ...missing, last_assistant_message: 'x'.repeat(64_001) }, [], client);
      const longChanged = capture.recordForInput({ ...missing, last_assistant_message: `${'x'.repeat(64_000)}y` }, [longFirst], client);
      assert(longChanged, 'a changed result beyond the capture text limit still identifies a new result');
      assert.notStrictEqual(longChanged.event_id, longFirst.event_id);
    }

    write([
      { role: 'user', content: 'Old task beyond the read limit.' },
      { type: 'response_item', payload: { type: 'custom_tool_call_output', output: 'x'.repeat(1_000_100) } },
      { type: 'event_msg', payload: { type: 'task_started', turn_id: 'tail-turn' } },
      { type: 'event_msg', payload: { type: 'user_message', message: 'Latest task at the tail.' } },
    ]);
    fs.appendFileSync(transcript, '{"partial":');
    const tail = capture.recordForInput(input, [], 'codex');
    assert.strictEqual(tail.request, 'Latest task at the tail.');
    assert.strictEqual(tail.turn_id, 'tail-turn');
    assert.strictEqual(capture.delegatedRequest(path.join(temp, 'missing')), '');
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

function sharedHostBehaviorTest() {
  // The same host events produce the same payload for Claude and Codex; only the client label differs.
  const events = [
    { hook_event_name: 'UserPromptSubmit', session_id: 's', turn_id: 'turn-1', prompt: 'Shared request', cwd: '/w/project' },
    { hook_event_name: 'Stop', session_id: 's', turn_id: 'turn-1', last_assistant_message: 'Shared answer', cwd: '/w/project' },
  ];
  const payloadFor = (client) => {
    const records = [];
    for (const event of events) records.push(capture.recordForInput(event, records, client, '2026-01-01T00:00:00Z'));
    const payload = capture.payloadFromRecords(records, { session_id: 's' });
    const shared = JSON.parse(JSON.stringify(payload).replaceAll(client, 'HOST'));
    // The event_id deliberately includes the host, so one session id never collides across hosts.
    for (const message of shared.messages) {
      assert.match(message.event_id, /^HOST:[0-9a-f]{16}:[0-9a-f]{20}:(?:user|assistant)$/);
      delete message.event_id;
    }
    return shared;
  };
  assert.deepStrictEqual(payloadFor('claude'), payloadFor('codex'));

  // A delegated agent's task prompt or result is never recorded as human/main speech.
  for (const event of [
    { hook_event_name: 'UserPromptSubmit', prompt: 'Delegated task prompt' },
    { hook_event_name: 'Stop', last_assistant_message: 'Delegated result' },
  ]) {
    assert.strictEqual(capture.recordForInput({ session_id: 's', agent_id: 'agent-1', ...event }, [], 'claude'), null);
    assert(capture.recordForInput({ session_id: 's', ...event }, [], 'claude'));
  }

  // Fallback turn IDs: a repeated prompt after the spool was pruned is a new turn, and a retry is stable.
  const repeat = { hook_event_name: 'UserPromptSubmit', session_id: 's', prompt: 'continue' };
  const first = capture.recordForInput(repeat, [], 'claude', '2026-01-01T10:00:00Z');
  const afterPrune = capture.recordForInput(repeat, [], 'claude', '2026-01-02T10:00:00Z');
  assert.notStrictEqual(first.event_id, afterPrune.event_id);
  assert.notStrictEqual(first.turn_id, afterPrune.turn_id);
  assert.strictEqual(capture.recordForInput(repeat, [], 'claude', '2026-01-01T10:00:00Z').event_id, first.event_id);
  const answer = capture.recordForInput({ hook_event_name: 'Stop', session_id: 's', last_assistant_message: 'ok' }, [afterPrune], 'claude');
  assert.strictEqual(answer.turn_id, afterPrune.turn_id);

  // Truncation never leaves half of a surrogate pair; lone surrogates are neutralised.
  const suffix = '\n\n[truncated by PersonaVault hook]';
  const straddle = `${'a'.repeat(64_000 - suffix.length - 1)}🚀${'b'.repeat(100)}`;
  const cut = capture.cleanText(straddle);
  assert(cut.length <= 64_000 && cut.endsWith(suffix));
  assert(!/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(cut));
  assert(!/[\ud800-\udfff]/.test(capture.cleanText('x\ud83dy\ude80z').replace(/🚀/g, '')));
  assert.strictEqual(capture.cleanText('x\ud83dy'), 'x�y');
  JSON.parse(Buffer.from(JSON.stringify({ text: cut })).toString('utf8'));
  const agent = capture.recordForInput({
    hook_event_name: 'SubagentStop', session_id: 's', agent_id: 'a', last_assistant_message: 'done',
    agent_type: `${'t'.repeat(119)}🚀`,
  }, [], 'claude');
  assert.strictEqual(agent.agent_type, 't'.repeat(119));
}

function explicitTurnAndHandbackTest() {
  // Identical text under distinct explicit turn ids is two turns; the same id replays once.
  for (const client of ['claude', 'codex']) {
    const user = (turn) => ({ hook_event_name: 'UserPromptSubmit', session_id: 's', turn_id: turn, prompt: 'continue' });
    const stop = (turn) => ({ hook_event_name: 'Stop', session_id: 's', turn_id: turn, last_assistant_message: 'ok' });
    const records = [];
    for (const event of [user('t1'), stop('t1'), user('t2'), stop('t2'), user('t3'), stop('t3')]) {
      const record = capture.recordForInput(event, records, client, '2026-01-01T00:00:00Z');
      assert(record, `${client} ${event.hook_event_name} ${event.turn_id}`);
      records.push(record);
    }
    assert.strictEqual(new Set(records.map((record) => record.event_id)).size, 6);
    for (const event of [user('t3'), stop('t3'), user('t1')]) {
      assert.strictEqual(capture.recordForInput(event, records, client), null, 'same explicit id is recorded once');
    }
    // Without an explicit id, identical consecutive text is still a replay.
    const bare = capture.recordForInput({ hook_event_name: 'UserPromptSubmit', session_id: 'b', prompt: 'x' }, [], client);
    assert.strictEqual(capture.recordForInput({ hook_event_name: 'UserPromptSubmit', session_id: 'b', prompt: 'x' }, [bare], client), null);
    // A root cwd has an empty basename; the project falls back to the client (the Gateway rejects '').
    const root = { ...records[0], cwd: '/', client };
    assert.strictEqual(capture.payloadFromRecords([root], { session_id: 's' }).project, client);
    assert.strictEqual(capture.payloadFromRecords([{ ...root, cwd: '/work/app' }], { session_id: 's' }).project, 'app');
  }

  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-handback-'));
  try {
    const transcript = path.join(temp, 'agent.jsonl');
    const write = (records) => fs.writeFileSync(transcript, records.map((record) => `${JSON.stringify(record)}\n`).join(''));
    const input = {
      hook_event_name: 'SubagentStop', session_id: 's', agent_id: 'a1', agent_type: '',
      agent_transcript_path: transcript, last_assistant_message: 'Done.',
    };
    const request = (uuid, text) => ({ type: 'user', uuid, message: { role: 'user', content: text } });
    const handback = (uuid, message) => ({ type: 'assistant', uuid, message: { role: 'assistant', content: [
      { type: 'text', text: 'Reporting back.' },
      { type: 'tool_use', name: 'SubagentHandback', input: { message } },
    ] } });
    const closing = { type: 'assistant', uuid: 'closing', message: { role: 'assistant', content: [{ type: 'text', text: 'Done.' }] } };
    write([request('r1', 'Audit the module.'), handback('h1', 'Full report: 3 findings.'), closing]);
    const reported = capture.recordForInput(input, [], 'claude');
    assert.strictEqual(reported.content, 'Full report: 3 findings.');
    assert.deepStrictEqual([reported.role, reported.kind, reported.request], ['subagent', 'subagent_result', 'Audit the module.']);
    // The report is what the hook sees even when its closing line is not flushed to the transcript yet.
    write([request('r1', 'Audit the module.'), handback('h1', 'Full report: 3 findings.')]);
    assert.strictEqual(capture.recordForInput(input, [], 'claude').content, 'Full report: 3 findings.');
    // A handback from an earlier request is never attributed to a later run: fall back to the closing text.
    write([request('r1', 'Audit the module.'), handback('h1', 'Full report: 3 findings.'), closing, request('r2', 'Say done.'), closing]);
    const later = capture.recordForInput(input, [reported], 'claude');
    assert.strictEqual(later.content, 'Done.');
    assert.strictEqual(later.request, 'Say done.');
    // No transcript, or no handback record: existing behaviour (closing message), still a subagent, never a user.
    for (const missing of [{ ...input, agent_transcript_path: path.join(temp, 'missing') }, { ...input, agent_transcript_path: undefined }]) {
      const fallback = capture.recordForInput(missing, [], 'codex');
      assert.deepStrictEqual([fallback.content, fallback.role, fallback.agent_type], ['Done.', 'subagent', 'subagent']);
    }
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

function configBomTest() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-bom-'));
  const oldAppData = process.env.APPDATA;
  try {
    process.env.APPDATA = temp;
    const dir = path.join(temp, 'persona-vault-gateway');
    fs.mkdirSync(dir);
    const expected = { url: 'https://vault.example.com', token: 'pvg_bom' };
    fs.writeFileSync(path.join(dir, 'env.json'), `﻿${JSON.stringify({
      PERSONA_VAULT_GATEWAY_URL: 'https://vault.example.com/', PERSONA_VAULT_TOKEN: 'pvg_bom',
    }, null, 2).replace(/\n/g, '\r\n')}`);
    assert.deepStrictEqual(capture.gatewayConfig(), expected);
    fs.unlinkSync(path.join(dir, 'env.json'));
    fs.writeFileSync(path.join(dir, 'env'), "﻿export PERSONA_VAULT_GATEWAY_URL='https://vault.example.com'\r\nexport PERSONA_VAULT_TOKEN='pvg_bom'\r\n");
    assert.deepStrictEqual(capture.gatewayConfig(), expected);
  } finally {
    if (oldAppData === undefined) delete process.env.APPDATA;
    else process.env.APPDATA = oldAppData;
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

// All credentials below are synthetic and assembled at runtime.
const FENCE = '```';
const SYNTH = {
  github: `ghp_${'a1'.repeat(18)}`,
  githubPat: `github_pat_${'B2'.repeat(15)}`,
  awsId: `AKIA${'Q7'.repeat(8)}`,
  awsSecret: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYsynthetic',
  anthropic: `sk-ant-${'z9'.repeat(14)}`,
  slack: `xoxb-${'1234567890-'.repeat(2)}abcdef`,
  pem: ['MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7', 'Zx3Qk9v1w8y2t5r7u0i4o6p8a1s3d5f7g9h1j3k5l7m9n1b3v5c7x9z1'],
};
const pemBlock = (eol, lines = SYNTH.pem, footer = true) => [
  '-----BEGIN PRIVATE KEY-----', ...lines, ...(footer ? ['-----END PRIVATE KEY-----'] : []),
].join(eol);

function privacyFilterTest() {
  const clean = capture.cleanText;
  const redacted = [
    ['{"password": "correct horse battery", "user": "bob"}', ['horse', 'battery'], ['"user": "bob"']],
    [`export AWS_SECRET_ACCESS_KEY=${SYNTH.awsSecret}\r\nregion=ap-northeast-2`, [SYNTH.awsSecret], ['\r\nregion=ap-northeast-2']],
    [`{"AWS_SECRET_ACCESS_KEY":"${SYNTH.awsSecret}","AWS_ACCESS_KEY_ID":"${SYNTH.awsId}"}`, [SYNTH.awsSecret, SYNTH.awsId], []],
    ["db_password='p@ss word here' next", ['p@ss', 'word here'], [' next']],
    ['client_secret: "two words" end', ['two words'], [' end']],
    ['password="unterminated value with spaces', ['unterminated', 'spaces'], []],
    ['git clone https://deploy:s3cr3tvalue@git.example.com/repo.git now', ['s3cr3tvalue'], ['git.example.com/repo.git now']],
    ['GET /v1/x?access_token=abc123xyz&page=2', ['abc123xyz'], ['&page=2']],
    ['curl -H "Authorization: Bearer abc.def.ghi-1234" https://x.example', ['abc.def.ghi-1234'], ['https://x.example']],
    ['Authorization: Basic dXNlcjpwYXNzd29yZA==\nnext line', ['dXNlcjpwYXNzd29yZA'], ['next line']],
    ['{"Authorization": "Bearer abcdef123456"}', ['abcdef123456'], []],
    ['mysql --password hunter2 --host db', ['hunter2'], ['--host db']],
    [`${SYNTH.github} ${SYNTH.githubPat} ${SYNTH.anthropic} ${SYNTH.slack}`, [SYNTH.github, SYNTH.githubPat, SYNTH.anthropic, SYNTH.slack], []],
    [`id ${SYNTH.awsId} done`, [SYNTH.awsId], ['done']],
    [`before\n${pemBlock('\n')}\nafter text`, SYNTH.pem, ['before\n[REDACTED PRIVATE KEY]\nafter text']],
    [`before\r\n${pemBlock('\r\n')}\r\nafter text`, SYNTH.pem, ['after text']],
    [`Then I pasted:\n${pemBlock('\n', SYNTH.pem, false)}\nThanks, that is all.`, SYNTH.pem, ['Thanks, that is all.']],
    [`key "-----BEGIN PRIVATE KEY-----\\n${SYNTH.pem[0]}\\n-----END PRIVATE KEY-----\\n" end`, [SYNTH.pem[0]], [' end']],
    ['🚀 password="한글 비밀 🚀" 끝', ['한글 비밀'], ['🚀 password="[REDACTED]" 끝']],
    [`password=${'Z'.repeat(600)} tail`, ['ZZ'], ['password=[REDACTED] tail']],
    [`password="${'Z y'.repeat(2_000)}" tail`, ['ZZ', 'Z y'], ['password="[REDACTED]" tail']],
    [`password="${'Z y'.repeat(2_000)}`, ['Z y'], ['password="[REDACTED]']],
    ['command --password "synthetic private credential" --next', ['synthetic', 'private credential'], ['--next']],
    ["command --token 'two words here", ['two words'], []],
    [`k\n-----BEGIN PRIVATE KEY-----\n${SYNTH.pem[0]}\nabcd\n-----END PRIVATE KEY-----\nafter`, ['abcd', SYNTH.pem[0]], ['k\n[REDACTED PRIVATE KEY]\nafter']],
    [`k\r\n${pemBlock('\r\n', [SYNTH.pem[0], 'abcd'])}\r\nafter`, ['abcd'], ['k\r\n[REDACTED PRIVATE KEY]\r\nafter']],
    [`"-----BEGIN PRIVATE KEY-----\\n${SYNTH.pem[0]}\\nabcd\\n-----END PRIVATE KEY-----\\n" end`, ['abcd'], [' end']],
    [`Pasted:\n${pemBlock('\n', [SYNTH.pem[0], SYNTH.pem[1], 'abcd'], false)}\nWhy does this fail?`, [SYNTH.pem[1]], ['Why does this fail?']],
    [`https://user:${'Z'.repeat(300)}@example.invalid/p?x=1`, ['ZZZZZZ'], ['https://[REDACTED]@example.invalid/p?x=1']],
    ['The password: swordfish must not be shared.', ['swordfish'], ['The password: [REDACTED]']],
    ['Use secret: huntertwo only locally.', ['huntertwo'], ['Use secret: [REDACTED]']],
    ['Then token: huntertwo is mine.', ['huntertwo'], ['token: [REDACTED]']],
    ['Set db_password: swordfish here', ['swordfish'], []],
    [`${pemBlock('\n', [`  ${'Q'.repeat(64)}`, '  abcd'])}\nafter`, ['QQQQ', 'abcd'], ['[REDACTED PRIVATE KEY]\nafter']],
    [pemBlock('\n', ['Comment here', 'Q'.repeat(64)]), ['QQQQ', 'Comment'], ['[REDACTED PRIVATE KEY]']],
    [`${pemBlock('\r\n', ['Comment here', `  ${'Q'.repeat(64)}`, '  abcd'])}\r\nafter`, ['QQQQ', 'abcd'], ['[REDACTED PRIVATE KEY]\r\nafter']],
    [`${pemBlock('\n', ['Q'.repeat(64)], false)}\nThanks, done.`, ['QQQQ'], ['Thanks, done.']],
  ];
  for (const [input, forbidden, required] of redacted) {
    const output = clean(input);
    for (const secret of forbidden) assert(!output.includes(secret), `leaked ${secret} in ${JSON.stringify(output)}`);
    for (const text of required) assert(output.includes(text), `lost ${JSON.stringify(text)} in ${JSON.stringify(output)}`);
    assert.strictEqual(clean(output), output, `idempotent: ${JSON.stringify(input)}`);
  }

  const preserved = [
    'Please rotate the token: use an env var, and never print the password in logs.',
    'Error ERR_TOKEN_EXPIRED came from auth_token_refresh() in api.ts, not from max_tokens: 4096.',
    "No, don't commit .env. I said keep API_KEY out of the repo and say which TOKEN you read.",
    '다음부터는 토큰 값을 로그에 남기지 말고, 비밀번호는 환경 변수로만 읽어줘. 틀린 부분은 고쳐줘.',
    '- Keep answers short\n- Never delete files\n- 한국어로 요약해줘\n- Ask before running migrations',
    `${FENCE}markdown\n# Rules\n- Keep answers short\n- Never delete files\n${FENCE}`,
    `${FENCE}text\nstep one\nstep two\nstep three\nstep four\n${FENCE}`,
    `${FENCE}\nFirst do A.\nThen do B.\nFinally report back in Korean.\n${FENCE}`,
    'Run `npm test` (inline code) then compare A=1 with B=2.',
  ];
  for (const text of preserved) assert.strictEqual(clean(text), text);

  const omitted = [
    [`${FENCE}bash\nnpm test\nnpm run lint\n${FENCE}`, '[omitted bash block, 2 lines]', 'npm run lint'],
    [`Run:\n${FENCE}sh\nrm -rf build\n${FENCE}\nthen retry.`, 'Run:\n[omitted sh block, 1 line]\nthen retry.', 'rm -rf'],
    [`Fix:\n${FENCE}js\nconst internalSecretFn = () => 1;\nconst b = 2;\nconsole.log(b);\n${FENCE}\nDone.`, 'Fix:\n[omitted js block, 3 lines]\nDone.', 'internalSecretFn'],
    [`${FENCE}json\n{\n  "name": "svc-internal",\n  "port": 1\n}\n${FENCE}`, '[omitted json block, 4 lines]', 'svc-internal'],
    [`${FENCE}yaml\nservices:\n  web-internal:\n    image: x\n${FENCE}`, '[omitted yaml block, 3 lines]', 'web-internal'],
    [`~~~env\nFOO_URL=x\nBAR_URL=y\nBAZ_URL=z\n~~~`, '[omitted env block, 3 lines]', 'FOO_URL'],
    [`${FENCE}\nFOO_URL=x\nBAR_URL=y\nBAZ_URL=z\n${FENCE}`, '[omitted env block, 3 lines]', 'FOO_URL'],
    [`${FENCE}\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-oldline\n+newline\n${FENCE}`, '[omitted diff block, 5 lines]', 'newline'],
    [`${FENCE}\n${Array.from({ length: 8 }, (_, i) => `2026-01-01 10:00:0${i} INFO request ${i} ok`).join('\n')}\n${FENCE}`, '[omitted log block, 8 lines]', 'request 3'],
    [`${FENCE}\n${Array.from({ length: 6 }, (_, i) => `const v${i} = call${i}();`).join('\n')}\n${FENCE}`, '[omitted code block, 6 lines]', 'call3'],
    [`Before\n${FENCE}python\nprint(1)\nprint(2)\nprint(3)\nprint(4)`, 'Before\n[omitted python block, 4 lines]', 'print(3)'],
    [`See the patch:\ndiff --git a/x b/x\nindex 111..222 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-oldline\n+newline\n\nThat is why.`, 'See the patch:\n[omitted diff block, 7 lines]\n\nThat is why.', 'newline'],
    ['Env was:\nAPI_URL=x\nDB_HOST=y\nCACHE_DIR=z\nAnd then it failed.', 'Env was:\n[omitted env block, 3 lines]\nAnd then it failed.', 'DB_HOST'],
    [`Log:\n${Array.from({ length: 9 }, (_, i) => `[ERROR] worker ${i} crashed`).join('\n')}\nWhy?`, 'Log:\n[omitted log block, 9 lines]\nWhy?', 'worker 4'],
  ];
  for (const [input, expected, hidden] of omitted) {
    const output = clean(input);
    assert(output.includes(expected), `${JSON.stringify(output)} lacks ${JSON.stringify(expected)}`);
    if (hidden) assert(!output.includes(hidden), `${hidden} survived in ${JSON.stringify(output)}`);
    assert.strictEqual(clean(output), output);
    assert.strictEqual(clean(input), output, 'markers are stable');
  }
  // Fewer than the log threshold stays: an error excerpt is evidence, not a dump.
  const excerpt = 'Failed:\n[ERROR] a\n[ERROR] b\n[ERROR] c';
  assert.strictEqual(clean(excerpt), excerpt);
  // CRLF text keeps its line endings outside omitted blocks.
  assert.strictEqual(clean(`a\r\n${FENCE}js\r\n1\r\n2\r\n3\r\n${FENCE}\r\nb`), 'a\r\n[omitted js block, 3 lines]\r\nb');

  // Bounded size: markers never push a message over the cap.
  const long = clean(`${'x'.repeat(63_990)}\n${FENCE}js\na\nb\nc\n${FENCE}\n${'y'.repeat(100)}`);
  assert(long.length <= 64_000 && long.endsWith('[truncated by PersonaVault hook]'));

  // No quadratic/catastrophic behaviour on adversarial input.
  const started = Date.now();
  for (const hostile of [
    'token'.repeat(13_000), 'token: '.repeat(9_000), 'password=x '.repeat(6_000), 'password="'.repeat(6_000),
    '-----BEGIN PRIVATE KEY-----\n'.repeat(3_000), 'Authorization: Bearer '.repeat(3_000), 'https://a:b'.repeat(8_000),
    `${FENCE}\n`.repeat(20_000), ' '.repeat(64_000) + 'password', 'A'.repeat(64_000),
  ]) clean(hostile);
  assert(Date.now() - started < 2_000, 'privacy filters must stay linear-time');

  // The shared diagnostic masking uses the same redaction.
  assert(!pvgClient.redactSecrets(`failed password=${SYNTH.awsSecret}`).includes(SYNTH.awsSecret));
}

function privacyCaptureRecordsTest() {
  const password = 'hunter2-synthetic';
  const prompt = `Deploy with password=${password} from /Users/alice/work/app\n${FENCE}py\nprint(1)\nprint(2)\nprint(3)\n${FENCE}\nPlease keep going.`;
  const stamp = '2026-02-03T04:05:06+09:00';
  for (const client of ['claude', 'codex']) {
    const events = [];
    const user = capture.recordForInput({
      hook_event_name: 'UserPromptSubmit', session_id: 'privacy', turn_id: 't1', prompt, cwd: '/Users/alice/work/app',
    }, events, client, stamp);
    events.push(user);
    const plain = capture.recordForInput({
      hook_event_name: 'UserPromptSubmit', session_id: 'privacy', turn_id: 't1', prompt: 'x',
    }, [], client, stamp);
    assert.strictEqual(user.event_id, plain.event_id, 'event identity does not depend on filtered text');
    assert.deepStrictEqual([user.role, user.kind, user.timestamp, user.turn_id], ['user', 'main_request', stamp, 't1']);
    assert.strictEqual(user.content, 'Deploy with password=[REDACTED] from /Users/alice/work/app\n[omitted py block, 3 lines]\nPlease keep going.');
    assert.strictEqual(user.cwd, 'app');
    // New records never keep a path or a token-shaped directory name.
    const tokenDir = `pvg_${'A'.repeat(43)}`;
    const tokenRecord = capture.recordForInput({
      hook_event_name: 'UserPromptSubmit', session_id: 'privacy-cwd', turn_id: 't1', prompt: 'hi', cwd: `/work/${tokenDir}`,
    }, [], client, stamp);
    assert(!JSON.stringify(tokenRecord).includes(tokenDir) && !tokenRecord.cwd.includes('/'));
    const fallback = capture.recordForInput({
      hook_event_name: 'Stop', session_id: 'privacy-cwd', last_assistant_message: 'ok',
    }, [{ ...tokenRecord, cwd: `/Users/alice/${tokenDir}`, kind: 'main_request', turn_id: 'legacy' }], client, stamp);
    assert.strictEqual(fallback.cwd, '[REDACTED PVG TOKEN]');
    const answer = capture.recordForInput({
      hook_event_name: 'Stop', session_id: 'privacy', turn_id: 't1', cwd: 'C:\\Users\\alice\\proj',
      last_assistant_message: `I will not use ${password}. token=${SYNTH.github}\n${FENCE}sh\nls\nls\nls\n${FENCE}`,
    }, events, client, stamp);
    assert.deepStrictEqual([answer.role, answer.kind], ['assistant', 'main_response']);
    assert.strictEqual(answer.cwd, 'proj');

    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-privacy-'));
    try {
      const transcript = path.join(temp, 'agent.jsonl');
      fs.writeFileSync(transcript, `${JSON.stringify({
        type: 'user', uuid: 'r1', message: { role: 'user', content: `Audit it, secret: "two words ${password}"\n${FENCE}json\n{\n"a": 1\n}\n${FENCE}` },
      })}\n${JSON.stringify({
        type: 'assistant', uuid: 'h1', message: { role: 'assistant', content: [
          { type: 'tool_use', name: 'SubagentHandback', input: { message: `Found Authorization: Bearer ${SYNTH.github.slice(0, 20)}abcdef in config.` } },
        ] },
      })}\n`);
      const agent = capture.recordForInput({
        hook_event_name: 'SubagentStop', session_id: 'privacy', agent_id: 'sub-1', agent_type: `token=${password}`,
        agent_transcript_path: transcript, last_assistant_message: 'Done.', cwd: '/Users/alice/work/app',
      }, [], client, stamp);
      assert.deepStrictEqual([agent.role, agent.kind], ['subagent', 'subagent_result']);
      assert.strictEqual(agent.cwd, 'app');
      assert.strictEqual(agent.request, 'Audit it, secret: "[REDACTED]"\n[omitted json block, 3 lines]');
      assert(agent.content.includes('Authorization: Bearer [REDACTED]'));
      assert.strictEqual(agent.agent_type, 'token=[REDACTED]');
      // A delegated agent's own prompt is still not human speech.
      assert.strictEqual(capture.recordForInput({ session_id: 'privacy', agent_id: 'sub-1', hook_event_name: 'UserPromptSubmit', prompt }, [], client), null);
      events.push(agent);
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
    const everything = JSON.stringify(events) + JSON.stringify(capture.payloadFromRecords(events, { session_id: 'privacy', cwd: '/Users/alice/work/app' }));
    for (const leak of [password, SYNTH.github, 'print(2)', 'two words']) assert(!everything.includes(leak), leak);
    assert(!JSON.stringify(events).includes('Users/alice/work/app"'), 'records keep no absolute cwd');
  }

  // A filter failure stores nothing and carries no text.
  const original = pvgClient.minimizeText;
  pvgClient.minimizeText = () => { throw new Error(`boom ${password}`); };
  try {
    assert.throws(() => capture.recordForInput({
      hook_event_name: 'UserPromptSubmit', session_id: 'privacy', turn_id: 't9', prompt,
    }, [], 'codex', stamp));
  } finally {
    pvgClient.minimizeText = original;
  }
}

async function gatewayDiagnosticsTest() {
  const token = 'pvg_diagnostic_token_value';
  const server = http.createServer((request, response) => {
    const status = Number(request.url.split('/').pop());
    const detail = status === 410
      ? { code: 'client_upgrade_required', plugin: { name: 'persona-vault', marketplace: 'm/p' } }
      : `${token} ${'echoed input '.repeat(1_000)}\u0000\u001b[31m`;
    response.writeHead(status, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ detail }));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  try {
    const config = { url: `http://127.0.0.1:${server.address().port}`, token };
    for (const [status, kind] of [
      [401, 'auth'], [403, 'auth'], [409, 'conflict'], [410, 'upgrade'], [413, 'payload'], [422, 'payload'],
      [429, 'transient'], [500, 'transient'], [503, 'transient'],
    ]) {
      const result = await capture.gatewayRequest(config, `capture/${status}`, { any: 'payload' }, 1_000);
      assert.strictEqual(result.ok, false);
      assert.strictEqual(result.status, status);
      assert.strictEqual(result.kind, kind, `HTTP ${status}`);
      assert(result.error.length <= 200 && !result.error.includes(token) && !/[\u0000-\u001f]/.test(result.error), result.error);
    }
    const dead = await capture.gatewayRequest({ url: 'http://127.0.0.1:9', token }, 'capture', {}, 500);
    assert.deepStrictEqual([dead.ok, dead.kind, dead.status], [false, 'transient', 0]);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

function captureNoticeTest() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-notice-'));
  try {
    const run = () => childProcess.spawnSync(
      process.execPath,
      [path.join(__dirname, '../plugins/persona-vault/hooks/persona-vault-session-start.js')],
      { encoding: 'utf8', env: { ...process.env, PLUGIN_DATA: temp }, input: '{}' },
    );
    assert(!run().stdout.includes('CAPTURE NOTICE'));
    for (const [kind, text] of [['auth', '--replace-token'], ['unsupported', 'conversation-merge-v1'], ['upgrade', 'newer persona-vault']]) {
      fs.writeFileSync(path.join(temp, 'status.json'), JSON.stringify({ kind, status: 401, error: 'x' }));
      const output = run().stdout;
      assert(output.includes('PERSONAVAULT CAPTURE NOTICE') && output.includes(text), kind);
    }
    fs.writeFileSync(path.join(temp, 'status.json'), JSON.stringify({ kind: 'transient' }));
    assert(!run().stdout.includes('CAPTURE NOTICE'), 'transient failures are retried silently');
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

// Readiness gates on the Gateway config only; missing helpers are a hint because the MCP tools need none.
function sessionStartReadinessTest() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-ready-'));
  try {
    const root = path.join(temp, 'config');
    const configFile = path.join(root, 'persona-vault-gateway', 'env.json');
    fs.mkdirSync(path.dirname(configFile), { recursive: true });
    // A refused port: the working-agreement fetch fails fast and the hook still answers.
    fs.writeFileSync(configFile, JSON.stringify({ PERSONA_VAULT_GATEWAY_URL: 'http://127.0.0.1:9', PERSONA_VAULT_TOKEN: 'pvg_test_token' }));
    const run = (extra = {}) => {
      const env = { ...process.env, APPDATA: root, XDG_CONFIG_HOME: root, HOME: temp, USERPROFILE: temp, PATH: '', ...extra };
      if (!extra.PLUGIN_DATA) delete env.PLUGIN_DATA;
      delete env.CLAUDE_PLUGIN_DATA;
      return childProcess.spawnSync(
        process.execPath,
        [path.join(__dirname, '../plugins/persona-vault/hooks/persona-vault-session-start.js')],
        { encoding: 'utf8', env, input: '{}' },
      ).stdout;
    };
    const hint = 'Helper commands not found: pvg-agent-memo, pvg-rag-search';

    const claude = run();
    assert(claude.startsWith('PERSONAVAULT:') && claude.includes('MCP tools are listed, use them'));
    assert(claude.includes(hint) && !claude.includes('SETUP NEEDED'));

    const codex = JSON.parse(run({ PLUGIN_DATA: path.join(temp, 'plugin-data') }));
    assert.strictEqual(codex.systemMessage, 'PERSONAVAULT');
    assert.strictEqual(codex.hookSpecificOutput.hookEventName, 'SessionStart');
    const { additionalContext } = codex.hookSpecificOutput;
    assert(additionalContext.includes('MCP tools are listed, use them') && additionalContext.includes(hint));
    assert(!additionalContext.includes('SETUP NEEDED'));

    const bin = path.join(temp, '.local', 'bin');
    const extension = process.platform === 'win32' ? '.ps1' : '';
    fs.mkdirSync(bin, { recursive: true });
    fs.writeFileSync(path.join(bin, `pvg-agent-memo${extension}`), '');
    const partial = run();
    assert(partial.includes('Helper commands not found: pvg-rag-search') && !partial.includes('not found: pvg-agent-memo'));
    fs.writeFileSync(path.join(bin, `pvg-rag-search${extension}`), '');
    const installed = run();
    assert(!installed.includes('Helper commands not found') && !installed.includes('SETUP NEEDED'));

    fs.unlinkSync(configFile);
    assert(run().includes('PERSONAVAULT SETUP NEEDED'));
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

async function main() {
  assert.match(capture.localTimestamp(), /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$/);

  const config = capture.parseEnvFile(
    "export PERSONA_VAULT_GATEWAY_URL='https://vault.example.com'\n"
    + "export PERSONA_VAULT_TOKEN='pvg_example'\n",
  );
  assert.strictEqual(config.PERSONA_VAULT_GATEWAY_URL, 'https://vault.example.com');
  assert.strictEqual(config.PERSONA_VAULT_TOKEN, 'pvg_example');

  const syntheticToken = 'pvg_' + 'A'.repeat(43);
  const redacted = capture.cleanText(
    `token=${syntheticToken}`,
  );
  assert(!redacted.includes(syntheticToken));
  assert(redacted.includes('[REDACTED]'));

  const truncated = capture.cleanText('x'.repeat(64_001));
  assert.strictEqual(truncated.length, 64_000);
  assert(truncated.endsWith('[truncated by PersonaVault hook]'));

  const records = [];
  const user = capture.recordForInput({
    hook_event_name: 'UserPromptSubmit',
    session_id: 'session-1',
    prompt: 'Implement the capture hook',
    cwd: '/workspace/persona-vault',
  }, records, 'claude', '2026-07-30T10:00:00Z');
  assert(user);
  records.push(user);

  const assistant = capture.recordForInput({
    hook_event_name: 'Stop',
    session_id: 'session-1',
    last_assistant_message: 'Implemented and tested the hook.',
    cwd: '/workspace/persona-vault',
  }, records, 'claude', '2026-07-30T10:01:00Z');
  assert(assistant);
  assert.strictEqual(assistant.turn_id, user.turn_id);
  records.push(assistant);
  assert.strictEqual(capture.recordForInput({
    hook_event_name: 'Stop',
    session_id: 'session-1',
    last_assistant_message: assistant.content,
  }, records, 'claude'), null);

  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-hook-'));
  try {
    const transcript = path.join(temp, 'subagent.jsonl');
    fs.writeFileSync(
      transcript,
      `${JSON.stringify({
        type: 'user',
        message: {
          role: 'user',
          content: [{
            type: 'text',
            text: 'Inspect the plugin manifest\n<recommended_plugins>\n- unrelated-plugin\n</recommended_plugins>',
          }],
        },
      })}\n`,
    );
    const subagent = capture.recordForInput({
      hook_event_name: 'SubagentStop',
      session_id: 'session-1',
      agent_id: 'agent-42',
      agent_type: 'Explore',
      agent_transcript_path: transcript,
      last_assistant_message: 'The Codex hook declaration was missing.',
      cwd: '/workspace/persona-vault',
    }, records, 'claude', '2026-07-30T10:00:30Z');
    assert(subagent);
    assert.strictEqual(subagent.request, 'Inspect the plugin manifest');
    records.splice(1, 0, subagent);
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }

  assert.strictEqual(
    capture.transcriptUserText({
      type: 'event_msg',
      payload: { type: 'user_message', message: 'Check Codex hooks' },
    }),
    'Check Codex hooks',
  );

  const payload = capture.payloadFromRecords(records, { session_id: 'session-1' });
  assert.strictEqual(payload.kind, 'conversation');
  assert.strictEqual(payload.session_id, 'session-1');
  assert.strictEqual(payload.project, 'persona-vault');
  assert.strictEqual(payload.messages.length, 3);
  assert.strictEqual(payload.messages[1].role, 'subagent');
  assert.strictEqual(payload.messages[1].request, 'Inspect the plugin manifest');
  assert.strictEqual(payload.messages[0].timestamp, '2026-07-30T10:00:00Z');
  assert.deepStrictEqual(payload.tags, ['agent-session', 'claude']);

  const nextDay = { ...records[0], event_id: 'next-day', timestamp: '2026-07-31T01:00:00Z' };
  const dailyPayloads = capture.payloadsFromRecords([...records, nextDay], { session_id: 'session-1' });
  assert.deepStrictEqual(dailyPayloads.map((daily) => daily.messages.length), [3, 1]);

  await curatorExclusionTest();
  subagentTranscriptTest();
  sharedHostBehaviorTest();
  explicitTurnAndHandbackTest();
  privacyFilterTest();
  privacyCaptureRecordsTest();
  configBomTest();
  await gatewayDiagnosticsTest();
  captureNoticeTest();
  sessionStartReadinessTest();
  await workingAgreementIntegrationTest();
  await captureIntegrationTest();
  await concurrentCaptureIntegrationTest();
  await helperIntegrationTest();
  pluginContractTest();
  console.log('hook tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
