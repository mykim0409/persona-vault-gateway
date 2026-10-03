// Native Windows regression for the installed PowerShell launchers (.ps1 -> shared pvg-client.js).
// Intended for Windows CI: it runs the real installer and launchers under Windows PowerShell 5.1
// (powershell.exe) and PowerShell 7 (pwsh) against a fake loopback Gateway, with a temp profile.
// On other platforms it only prints a skip notice; that is NOT a native pass.
const assert = require('assert');
const childProcess = require('child_process');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

const SCRIPTS = path.join(__dirname, '../plugins/persona-vault/scripts');
const TOKEN = 'pvg_win_token_value';
const REPLACED = 'pvg_win_replaced_value';

function run(command, args, env, input = '') {
  return new Promise((resolve, reject) => {
    const child = childProcess.spawn(command, args, { env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    const out = [];
    const err = [];
    const timer = setTimeout(() => child.kill(), 90_000);
    child.stdout.on('data', (chunk) => out.push(chunk));
    child.stderr.on('data', (chunk) => err.push(chunk));
    child.once('error', reject);
    child.once('close', (status) => {
      clearTimeout(timer);
      resolve({ status, stdout: Buffer.concat(out).toString('utf8'), stderr: Buffer.concat(err).toString('utf8') });
    });
    child.stdin.end(input);
  });
}

async function exerciseShell(shell, requests, setFailure, url) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `pvg 한글 ${path.basename(shell)}-`));
  try {
    const parsed = path.parse(root);
    const env = {
      ...process.env,
      HOME: root, USERPROFILE: root, HOMEDRIVE: parsed.root.slice(0, 2), HOMEPATH: root.slice(2),
      APPDATA: path.join(root, 'AppData', 'Roaming'), LOCALAPPDATA: path.join(root, 'AppData', 'Local'),
      XDG_CONFIG_HOME: path.join(root, 'xdg'), PLUGIN_DATA: path.join(root, 'plugin-data'),
      PERSONA_VAULT_GATEWAY_URL: url, PERSONA_VAULT_TOKEN: TOKEN, NO_PROXY: '127.0.0.1',
    };
    const base = ['-NoProfile', '-ExecutionPolicy', 'Bypass'];
    const label = (what, result) => `${shell} ${what}: exit ${result.status}\n${result.stdout}\n${result.stderr}`;
    const ok = (what, result) => assert.strictEqual(result.status, 0, label(what, result));

    let result = await run(shell, [...base, '-File', path.join(SCRIPTS, 'install-agent-config.ps1')], env);
    ok('install', result);
    assert(!`${result.stdout}${result.stderr}`.includes(TOKEN), 'installer must not echo the token');
    const configFile = path.join(root, 'AppData', 'Roaming', 'persona-vault-gateway', 'env.json');
    const raw = fs.readFileSync(configFile);
    assert(!(raw[0] === 0xEF && raw[1] === 0xBB && raw[2] === 0xBF), 'env.json must not carry a BOM');
    assert.strictEqual(JSON.parse(raw.toString('utf8')).PERSONA_VAULT_TOKEN, TOKEN);
    const bin = path.join(root, '.local', 'bin');
    const memo = path.join(bin, 'pvg-agent-memo.ps1');
    const search = path.join(bin, 'pvg-rag-search.ps1');
    for (const file of [memo, search, path.join(bin, 'pvg-agent-memo.cmd'), path.join(bin, 'pvg-rag-search.cmd')]) {
      assert(fs.existsSync(file), file);
    }

    // Explicit credentials replace the saved token on rerun, without deleting config and without echo.
    result = await run(shell, [...base, '-File', path.join(SCRIPTS, 'install-agent-config.ps1'), '-ReplaceToken'],
      { ...env, PERSONA_VAULT_TOKEN: REPLACED, PERSONA_VAULT_GATEWAY_URL: '' });
    ok('replace token', result);
    assert(!`${result.stdout}${result.stderr}`.includes(REPLACED));
    const saved = JSON.parse(fs.readFileSync(configFile, 'utf8'));
    assert.deepStrictEqual([saved.PERSONA_VAULT_TOKEN, saved.PERSONA_VAULT_GATEWAY_URL], [REPLACED, url]);

    const quote = (value) => `'${value.replace(/'/g, "''")}'`;
    // Direct -File: arguments reach node untouched by cmd.exe.
    result = await run(shell, [...base, '-File', memo, '--help'], env);
    ok('memo --help', result);
    assert(result.stdout.includes('--type observation|proposal|handoff'), label('help', result));
    const before = requests.length;
    result = await run(shell, [...base, '-File', search, '--view', 'current', 'plain query'], env);
    ok('search', result);
    assert(result.stdout.includes('windows search result'), label('search', result));
    assert.deepStrictEqual([requests.at(-1).path, requests.at(-1).body.view, requests.at(-1).authorization],
      ['/gateway/v3/search', 'current', `Bearer ${REPLACED}`]);
    assert.strictEqual(requests.length, before + 1);

    // Pipeline body through the wrapper: Korean + emoji survive, default note_type is observation.
    const body = '한글 본문 🚀 line2';
    const meta = '100% & | ^ < > ( ) ! $x `t` ; 한글';
    result = await run(shell, [...base, '-Command',
      `$env:PVG_TEST_BODY | & ${quote(memo)} --title ${quote(meta)} --project ${quote('Pro ject')} --evidence ${quote('ref & 1')}`],
    { ...env, PVG_TEST_BODY: body });
    ok('memo pipeline', result);
    const note = requests.at(-1).body;
    assert.deepStrictEqual([note.note_type, note.kind, note.body, note.title, note.project, note.provenance.evidence_refs],
      ['observation', 'note', body, meta, 'Pro ject', ['ref & 1']], label('memo', result));
    // Query on stdin and a metacharacter query as an argument (single quotes: no embedded double quotes on 5.1).
    result = await run(shell, [...base, '-Command', `$env:PVG_TEST_BODY | & ${quote(search)}`], { ...env, PVG_TEST_BODY: body });
    ok('search stdin', result);
    assert.strictEqual(requests.at(-1).body.query, body);
    result = await run(shell, [...base, '-Command', `& ${quote(search)} ${quote(meta)}`], env);
    ok('search metacharacters', result);
    assert.strictEqual(requests.at(-1).body.query, meta);

    // Non-2xx: nonzero exit codes propagate through the wrapper; the token is never printed.
    for (const [query, status, expected] of [['fail-auth', 1, '--replace-token'], ['fail-upgrade', 75, 'client update required'], ['fail-down', 1, 'temporarily unavailable']]) {
      setFailure(query);
      result = await run(shell, [...base, '-File', search, query], env);
      assert.strictEqual(result.status, status, label(query, result));
      assert(result.stderr.includes(expected), label(query, result));
      assert(!`${result.stdout}${result.stderr}`.includes(REPLACED));
    }
    setFailure('');
    console.log(`windows launchers (${shell}): ok`);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

async function main() {
  if (process.platform !== 'win32') {
    console.log('SKIPPED: test_client_windows.js needs native Windows (powershell.exe / pwsh); this is not a Windows pass.');
    return;
  }
  const requests = [];
  let failing = '';
  const failures = {
    'fail-auth': [401, { detail: 'bad token' }],
    'fail-upgrade': [410, { detail: { code: 'client_upgrade_required', plugin: { name: 'persona-vault', marketplace: 'm/p' } } }],
    'fail-down': [503, { detail: 'down' }],
  };
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      requests.push({ path: request.url, authorization: request.headers.authorization, body });
      const [status, payload] = failing && body.query === failing ? failures[failing] : [200, request.url.endsWith('/search')
        ? { answer_state: { state: 'answered', reason: 'test' }, context: 'windows search result' } : { status: 'ok' }];
      response.writeHead(status, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(payload));
    });
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  try {
    const url = `http://127.0.0.1:${server.address().port}`;
    const available = ['powershell.exe', 'pwsh'].filter((shell) => childProcess.spawnSync(
      shell, ['-NoProfile', '-Command', 'exit 0'], { windowsHide: true },
    ).status === 0);
    assert(available.includes('powershell.exe'), 'Windows PowerShell 5.1 (powershell.exe) is required on a Windows runner');
    if (!available.includes('pwsh')) console.log('SKIPPED: pwsh (PowerShell 7) not installed; only 5.1 was exercised.');
    for (const shell of available) await exerciseShell(shell, requests, (query) => { failing = query; }, url);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
