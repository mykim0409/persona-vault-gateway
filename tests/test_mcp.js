// MCP server (plugins/persona-vault/scripts/pvg-mcp.js): real stdio JSON-RPC against a fake loopback Gateway,
// with a temp HOME/APPDATA/XDG_CONFIG_HOME so the real user config is never read. Also checks the plugin
// manifests that register the server for Claude Code and Codex.
const assert = require('assert');
const childProcess = require('child_process');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

const PLUGIN = path.join(__dirname, '../plugins/persona-vault');
const SERVER = path.join(PLUGIN, 'scripts/pvg-mcp.js');
const pvgClient = require(path.join(PLUGIN, 'scripts/pvg-client.js'));
const readJson = (file) => JSON.parse(fs.readFileSync(path.join(PLUGIN, file), 'utf8'));
const TOKEN = 'pvg_mcp_token';

function manifestTest() {
  const claude = readJson('.claude-plugin/plugin.json');
  const codex = readJson('.codex-plugin/plugin.json');
  // Claude auto-loads the root .mcp.json (so its manifest needs no pointer); Codex is pointed at its own file.
  assert.deepStrictEqual(readJson('.mcp.json'), {
    mcpServers: { pvg: { command: 'node', args: ['${CLAUDE_PLUGIN_ROOT}/scripts/pvg-mcp.js'] } },
  });
  assert(!Object.hasOwn(claude, 'mcpServers'));
  assert.strictEqual(codex.mcpServers, './.codex-mcp.json');
  const { pvg } = readJson('.codex-mcp.json').mcpServers;
  assert.deepStrictEqual([pvg.command, pvg.args, pvg.cwd], ['node', ['./scripts/pvg-mcp.js'], '.']);
  assert(pvg.tool_timeout_sec > 60, 'longer than the helper search timeout, so the helper reports its own error');
  assert(fs.existsSync(path.join(PLUGIN, pvg.args[0])));
  // The skill names the tools exactly as Claude Code derives them from plugin name + server key + tool.
  const skill = fs.readFileSync(path.join(PLUGIN, 'skills/persona-vault/SKILL.md'), 'utf8');
  assert(skill.includes(`mcp__plugin_${claude.name}_pvg__pvg_search`));
  assert(skill.includes('`pvg_memo` (or `pvg-agent-memo`) only when the current user explicitly asks'));
  assert(skill.includes('`pvg_search` (or `pvg-rag-search`)'));
  for (const reference of ['posix', 'windows']) {
    const text = fs.readFileSync(path.join(PLUGIN, `skills/persona-vault/references/${reference}.md`), 'utf8');
    assert(text.includes('`pvg_search` and `pvg_memo` MCP tools are not listed'), reference);
  }
}

// One server process; rpc() resolves with the reply whose id matches, lines keeps every raw stdout line.
function startServer(env) {
  const child = childProcess.spawn(process.execPath, [SERVER], { env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  const server = { child, lines: [], stderr: '', waiting: new Map(), nextId: 1 };
  let buffer = '';
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    buffer += chunk;
    for (let eol = buffer.indexOf('\n'); eol >= 0; eol = buffer.indexOf('\n')) {
      const line = buffer.slice(0, eol);
      buffer = buffer.slice(eol + 1);
      server.lines.push(line);
      const message = JSON.parse(line); // every stdout line must be one JSON message
      server.waiting.get(message.id)?.(message);
    }
  });
  child.stderr.on('data', (chunk) => { server.stderr += chunk; });
  server.exited = new Promise((resolve) => child.once('close', (code) => resolve(code)));
  server.reply = (id) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`no reply to ${id}; stderr: ${server.stderr}`)), 20_000);
    server.waiting.set(id, (message) => { clearTimeout(timer); resolve(message); });
  });
  server.rpc = (method, params) => {
    const id = server.nextId++;
    const reply = server.reply(id);
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    return reply;
  };
  server.notify = (method, params) => child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`);
  server.call = (name, args) => server.rpc('tools/call', { name, arguments: args });
  return server;
}

async function serverTest() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pvg-mcp-'));
  const requests = [];
  let failWith = null;
  const gateway = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      requests.push({ path: request.url, authorization: request.headers.authorization, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) });
      response.writeHead(failWith ? failWith.status : 200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(failWith ? failWith.body : request.url.endsWith('/search')
        ? { answer_state: { state: 'supported', reason: 'r' }, context: 'ctx 한글' } : { status: 'ok', note_id: 'n1' }));
    });
  });
  await new Promise((resolve, reject) => {
    gateway.once('error', reject);
    gateway.listen(0, '127.0.0.1', resolve);
  });

  // APPDATA wins over XDG_CONFIG_HOME on every platform, so both name the same directory.
  const configRoot = path.join(temp, 'config');
  const configDir = path.join(configRoot, 'persona-vault-gateway');
  fs.mkdirSync(configDir, { recursive: true });
  const configFile = path.join(configDir, 'env.json');
  fs.writeFileSync(configFile, JSON.stringify({ PERSONA_VAULT_GATEWAY_URL: `http://127.0.0.1:${gateway.address().port}`, PERSONA_VAULT_TOKEN: TOKEN }));
  // The helper's env switches are set on purpose: the server must pin them off.
  const server = startServer({
    ...process.env, HOME: temp, USERPROFILE: temp, APPDATA: configRoot, XDG_CONFIG_HOME: configRoot, NO_PROXY: '127.0.0.1',
    PVG_RAG_REFRESH: '1', PVG_RAG_JSON: '1', PVG_RAG_LIMIT: '9', PVG_RAG_VIEW: 'history',
  });
  const text = (reply) => reply.result.content[0].text;
  const last = () => requests.at(-1);

  try {
    // Handshake: the version is echoed when known, the newest supported one is offered otherwise.
    let reply = await server.rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '1' } });
    assert.strictEqual(reply.jsonrpc, '2.0');
    assert.strictEqual(reply.result.protocolVersion, '2025-06-18');
    assert.deepStrictEqual(reply.result.capabilities, { tools: {} });
    assert.deepStrictEqual(reply.result.serverInfo, { name: 'persona-vault', version: readJson('.claude-plugin/plugin.json').version });
    assert.strictEqual((await server.rpc('initialize', { protocolVersion: '2025-11-25' })).result.protocolVersion, '2025-11-25');
    assert.strictEqual((await server.rpc('initialize', { protocolVersion: '1999-01-01' })).result.protocolVersion, '2025-11-25');
    // Notifications are never answered: the next line on stdout is the ping reply.
    server.notify('notifications/initialized');
    server.notify('notifications/cancelled', { requestId: 99 });
    const lines = server.lines.length;
    assert.deepStrictEqual((await server.rpc('ping')).result, {});
    assert.strictEqual(server.lines.length, lines + 1);

    reply = await server.rpc('tools/list');
    const [search, memo] = reply.result.tools;
    assert.deepStrictEqual(reply.result.tools.map((tool) => tool.name), ['pvg_search', 'pvg_memo']);
    assert.deepStrictEqual([search.inputSchema.required, memo.inputSchema.required], [['query'], ['title', 'body']]);
    assert.deepStrictEqual(search.inputSchema.properties.view.enum, pvgClient.VIEWS);
    assert.deepStrictEqual(memo.inputSchema.properties.outcome.enum, pvgClient.OUTCOMES);
    assert.deepStrictEqual(memo.inputSchema.properties.provenance.enum, pvgClient.PROVENANCE);
    assert.deepStrictEqual(memo.inputSchema.properties.note_type.enum, ['observation', 'proposal', 'handoff']);
    assert(!Object.hasOwn(search.inputSchema.properties, 'refresh'));
    // The server validates against these bounds, so they must stay in the published schema.
    assert.deepStrictEqual(
      [search.inputSchema.properties.query.minLength, memo.inputSchema.properties.title.minLength, memo.inputSchema.properties.body.minLength],
      [1, 1, 1],
    );
    assert.deepStrictEqual([search.annotations.readOnlyHint, memo.annotations.readOnlyHint], [true, false]);

    // Unsupported methods get -32601 promptly (this also answers the stateless server/discover probe).
    for (const method of ['server/discover', 'resources/list', 'prompts/list']) {
      reply = await server.rpc(method);
      assert.strictEqual(reply.error.code, -32601, method);
    }
    reply = await server.call('nope', {});
    assert.strictEqual(reply.error.code, -32602);
    reply = await server.call('pvg_search');
    assert(reply.result.isError && text(reply).includes('query is required'), text(reply));
    server.child.stdin.write('not json\n');
    reply = await server.reply(null);
    assert.strictEqual(reply.error.code, -32700);
    // A request without a string method, or a batch, is an Invalid Request; JSON without an id stays silent.
    const rejected = [server.reply(950), server.reply(951)];
    server.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 950 })}\n${JSON.stringify({ jsonrpc: '2.0', id: 951, method: 5 })}\n`);
    for (const message of await Promise.all(rejected)) assert.deepStrictEqual(message.error, { code: -32600, message: 'Invalid Request' });
    const batch = server.reply(null);
    server.child.stdin.write(`${JSON.stringify([{ jsonrpc: '2.0', id: 952, method: 'ping' }])}\n`);
    assert.deepStrictEqual((await batch).error, { code: -32600, message: 'Invalid Request' });
    const quiet = server.lines.length;
    server.child.stdin.write('5\nnull\n"x"\n');
    assert.deepStrictEqual((await server.rpc('ping')).result, {});
    assert.strictEqual(server.lines.length, quiet + 1);
    assert.strictEqual(requests.length, 0, 'nothing reached the Gateway yet');

    // pvg_search: same text as the CLI helper, refresh/JSON/limit/view env pinned by the server.
    reply = await server.call('pvg_search', { query: 'raw claim' });
    assert.strictEqual(reply.result.isError, false);
    assert.strictEqual(text(reply), 'Answer state: supported (r)\nctx 한글');
    assert.strictEqual(last().path, '/gateway/v3/search');
    assert.strictEqual(last().authorization, `Bearer ${TOKEN}`);
    assert.deepStrictEqual(last().body, { query: 'raw claim', limit: 5, refresh: false, view: 'all' });
    await server.call('pvg_search', { query: '-x --view', view: 'evidence', limit: 3 });
    assert.deepStrictEqual(last().body, { query: '-x --view', limit: 3, refresh: false, view: 'evidence' });
    reply = await server.call('pvg_search', { query: 'q', view: 'bogus' });
    assert(reply.result.isError && text(reply).includes('--view requires'), text(reply));

    // pvg_memo: arguments map onto the helper's payload; the body arrives byte for byte.
    const body = 'Situation: 한글 🚀\nline two after separator\n\nUncertainty: none';
    const fullMemo = {
      title: '-t', body, note_type: 'proposal', kind: 'debugging', outcome: 'success', project: 'Example',
      provenance: 'derived', session_id: 'run-1', subject: 'subj', evidence: ['run_1', 'run_2'], tags: ['x', 'y'],
    };
    reply = await server.call('pvg_memo', fullMemo);
    assert.strictEqual(reply.result.isError, false, text(reply));
    assert.deepStrictEqual(JSON.parse(text(reply)), { status: 'ok', note_id: 'n1' });
    assert.strictEqual(last().path, '/gateway/v3/capture');
    assert.strictEqual(last().authorization, `Bearer ${TOKEN}`);
    assert.deepStrictEqual(
      Object.fromEntries(['title', 'body', 'note_type', 'note_kind', 'outcome', 'project', 'session_id', 'subject_id', 'tags'].map((key) => [key, last().body[key]])),
      { title: '-t', body, note_type: 'proposal', note_kind: 'debugging', outcome: 'success', project: 'Example', session_id: 'run-1', subject_id: 'subj', tags: ['x', 'y'] },
    );
    assert.deepStrictEqual(last().body.provenance, { mode: 'derived', evidence_refs: ['run_1', 'run_2'], method_refs: [], derived_from: [] });
    const fullMemoBody = last().body;
    await server.call('pvg_memo', { title: 'defaults', body: 'b' });
    assert.deepStrictEqual([last().body.note_type, last().body.outcome, last().body.provenance.mode], ['observation', 'unknown', 'reported']);

    // Invalid input is a tool error from the shared helper and never reaches the Gateway.
    const before = requests.length;
    for (const [args, message] of [
      [{ title: 't', body: 'b', outcome: 'bogus' }, '--outcome requires'],
      [{ title: 't', body: 'b', provenance: 'guess' }, '--provenance requires'],
      [{ title: 't', body: 'b', note_type: 'note' }, '--type requires'],
      [{ title: 't', body: '  \n' }, 'memo body is required'],
      [{ title: ' ', body: 'b' }, 'memo title is required'],
    ]) {
      reply = await server.call('pvg_memo', args);
      assert(reply.result.isError && text(reply).includes(message), `${message}: ${text(reply)}`);
    }
    assert.strictEqual(requests.length, before);

    // The server rejects malformed arguments itself. These texts exist nowhere else, so an exact match proves the
    // helper never ran; nothing reaches the Gateway either.
    const sent = requests.length;
    for (const [name, args, expected] of [
      ['pvg_memo', { title: 't', body: { text: 'x' } }, 'body must be a string'],
      ['pvg_memo', { body: 'b' }, 'title is required'],
      ['pvg_memo', { title: 't' }, 'body is required'],
      ['pvg_memo', { title: '', body: 'b' }, 'title must not be empty'],
      ['pvg_memo', { title: 't', body: '' }, 'body must not be empty'],
      ['pvg_memo', { title: 't', body: 'b', tags: 'x' }, 'tags must be an array of strings'],
      ['pvg_memo', { title: 't', body: 'b', evidence: ['ok', 1] }, 'evidence must be an array of strings'],
      ['pvg_memo', { title: 't', body: 'b', kind: 5 }, 'kind must be a string'],
      ['pvg_search', { query: 5 }, 'query must be a string'],
      ['pvg_search', { query: '' }, 'query must not be empty'],
      ['pvg_search', { query: 'q', limit: 2.5 }, 'limit must be an integer in 1..20'],
      ['pvg_search', { query: 'q', limit: '3' }, 'limit must be an integer in 1..20'],
      ['pvg_search', { query: 'q', limit: 0 }, 'limit must be an integer in 1..20'],
      ['pvg_search', { query: 'q', limit: 21 }, 'limit must be an integer in 1..20'],
      ['pvg_search', { query: 'x'.repeat(501) }, 'query must be at most 500 characters'],
      ['pvg_search', 'a string', 'arguments must be an object'],
      ['pvg_memo', ['title', 'body'], 'arguments must be an object'],
    ]) {
      reply = await server.call(name, args);
      assert(reply.result.isError === true && text(reply) === expected, `${expected}: ${JSON.stringify(reply)}`);
    }
    assert.strictEqual(requests.length, sent, 'a rejected call never reaches the Gateway');
    // Valid calls are unchanged: null optionals count as absent, and the full memo sends the same payload again.
    await server.call('pvg_search', { query: 'q', limit: null, view: null });
    assert.deepStrictEqual(last().body, { query: 'q', limit: 5, refresh: false, view: 'all' });
    await server.call('pvg_memo', fullMemo);
    assert.deepStrictEqual(last().body, fullMemoBody);

    // Gateway failures keep the helper's classified, token-free guidance.
    const echo = `${TOKEN} ${'x'.repeat(5_000)}`;
    for (const [failure, expected] of [
      [{ status: 401, body: { detail: echo } }, '--replace-token'],
      [{ status: 410, body: { detail: { code: 'client_upgrade_required', plugin: { name: 'persona-vault', marketplace: 'm/p' } } } }, 'client update required'],
      [{ status: 503, body: { detail: echo } }, 'temporarily unavailable'],
    ]) {
      failWith = failure;
      for (const [name, args] of [['pvg_search', { query: 'q' }], ['pvg_memo', { title: 't', body: 'b' }]]) {
        reply = await server.call(name, args);
        assert(reply.result.isError && text(reply).includes(expected), text(reply));
        assert(!text(reply).includes(TOKEN) && text(reply).length < 700, text(reply));
      }
    }
    failWith = null;

    // Framing: a request split inside a multibyte character, and two requests in one write.
    const split = Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', id: 900, method: 'tools/call', params: { name: 'pvg_search', arguments: { query: '한글 query' } } })}\n`);
    const cut = split.indexOf(Buffer.from('한')) + 1;
    const pending = server.reply(900);
    server.child.stdin.write(split.subarray(0, cut));
    await new Promise((resolve) => setTimeout(resolve, 50));
    server.child.stdin.write(split.subarray(cut));
    assert.strictEqual(text(await pending), 'Answer state: supported (r)\nctx 한글');
    assert.strictEqual(last().body.query, '한글 query');
    const both = [server.reply(901), server.reply(902)];
    server.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 901, method: 'ping' })}\n${JSON.stringify({ jsonrpc: '2.0', id: 902, method: 'ping' })}\n`);
    assert.deepStrictEqual((await Promise.all(both)).map((message) => message.result), [{}, {}]);

    // Missing config is reported as a tool error with the installer guidance, not a crash.
    fs.unlinkSync(configFile);
    reply = await server.call('pvg_search', { query: 'q' });
    assert(reply.result.isError && text(reply).includes('Missing or invalid PersonaVault config'), text(reply));
    assert(!text(reply).includes(TOKEN));

    // The server exits cleanly when the host closes stdin, and stdout/stderr stayed clean throughout.
    server.child.stdin.end();
    assert.strictEqual(await server.exited, 0);
    assert.strictEqual(server.stderr, '');
    assert(server.lines.every((line) => JSON.parse(line).jsonrpc === '2.0'));
  } finally {
    server.child.kill();
    await new Promise((resolve) => gateway.close(resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

async function main() {
  manifestTest();
  await serverTest();
  console.log('mcp tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
