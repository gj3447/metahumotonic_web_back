// Read-only live integration check. URLs/tokens come only from explicit process
// environment; never from catalog edges or automatic config/credential discovery.
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { boundedFetch } from '../dist/src/platform/BoundedHttp.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const receiptPath = process.argv[2];
if (!receiptPath || process.argv.length !== 3) throw new Error('Usage: node scripts/check-mcp-readback.mjs NEW_RECEIPT.json');
const profiles = [
  { id: 'ontology', tool: 'ontology_get', args: { uid: 'sym:Concept:hswm', text_limit: 300, text_offset: 0 } },
  { id: 'hspine', tool: 'hspine_capabilities', args: {} },
];
const sorted = (x) => Array.isArray(x) ? x.map(sorted) : x && typeof x === 'object'
  ? Object.fromEntries(Object.entries(x).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, sorted(v)])) : x;
const canonical = (value) => JSON.stringify(sorted(value));
const digest = (value) => createHash('sha256').update(typeof value === 'string' || Buffer.isBuffer(value) ? value : canonical(value)).digest('hex');
const treeHashes = async (directory, hashes = {}) => {
  for (const entry of await readdir(resolve(root, directory), { withFileTypes: true })) {
    const path = `${directory}/${entry.name}`;
    if (entry.isDirectory()) await treeHashes(path, hashes);
    else if (entry.isFile()) hashes[path] = digest(await readFile(resolve(root, path)));
    else throw new Error('compiled artifacts must be regular files');
  }
  return hashes;
};
const decode = (result) => {
  assert.notEqual(result.isError, true, 'tool returned an error');
  const text = result.content.filter((item) => item.type === 'text').map((item) => JSON.parse(item.text));
  assert.ok(text.length > 0, 'missing structured tool content');
  return text;
};
const hasSubject = (value) => Boolean(value && typeof value === 'object' &&
  (value.uid === 'sym:Concept:hswm' || Object.values(value).some(hasSubject)));
const receipt = { schema: 'metahumotonic/mcp-readback@1', observedAt: new Date().toISOString(),
  scope: 'Disposable compiled TS entrypoint; explicit read-only calls to owner MCP services; no deployment',
  status: 'FAIL', checks: [], servers: [] };
let stage = 'preflight', child, scratch, reserved = false;
const running = () => child && child.exitCode === null && child.signalCode === null;
const clients = [];
const key = randomBytes(32).toString('hex');
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
const connect = async (url, token) => {
  const client = new Client({ name: 'mhb-owner-readback', version: '1' });
  clients.push(client);
  await client.connect(new StreamableHTTPClientTransport(new URL(url), {
    requestInit: { headers: token ? { Authorization: `Bearer ${token}` } : {} },
    fetch: boundedFetch(10_000, 1_048_576),
  }), { timeout: 10_000 });
  return client;
};
try {
  // Reserve receipt exclusively before any remote read or process creation.
  await writeFile(receiptPath, JSON.stringify({ ...receipt, status: 'RUNNING' }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  reserved = true;
  receipt.sourceSha256 = {};
  for (const path of ['scripts/check-mcp-readback.mjs', 'dist/src/main.js', 'dist/src/ports/McpFederation.js', 'dist/src/platform/BoundedHttp.js', 'config/mcp-bindings.json'])
    receipt.sourceSha256[path] = digest(await readFile(resolve(root, path)));
  receipt.compiledTreeSha256 = digest({ ...await treeHashes('dist/src'), ...await treeHashes('dist/config'),
    'package-lock.json': digest(await readFile(resolve(root, 'package-lock.json'))) });
  const bindingsText = await readFile(resolve(root, 'dist/config/mcp-bindings.json'), 'utf8');
  assert.equal(bindingsText, await readFile(resolve(root, 'config/mcp-bindings.json'), 'utf8'), 'compiled bindings are stale');
  const bindings = JSON.parse(bindingsText);
  const env = { PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? '',
    MHB_HOST: '127.0.0.1', MHB_NEO4J_LIVE: 'false', MHB_LOG_JSON: 'false',
    MHB_MONGO_URI: '', MHB_REDIS_URL: '', MHB_PLATFORM_READ_KEY: key };
  for (const profile of profiles) {
    const binding = bindings.servers.find((item) => item.id === profile.id);
    assert.ok(binding && binding.tools.some((tool) => tool.name === profile.tool && tool.access === 'read'));
    assert.ok(process.env[binding.urlEnv], `missing endpoint for ${profile.id}`);
    env[binding.urlEnv] = process.env[binding.urlEnv];
    if (binding.tokenEnv) env[binding.tokenEnv] = process.env[binding.tokenEnv] ?? '';
    profile.binding = binding;
  }
  const socket = createServer();
  await new Promise((done) => socket.listen(0, '127.0.0.1', done));
  env.MHB_PORT = String(socket.address().port);
  await new Promise((done) => socket.close(done));
  env.MHB_VERSION = `mcp-readback-${randomBytes(12).toString('hex')}`;
  scratch = await mkdtemp(resolve(tmpdir(), 'mhb-mcp-readback-'));
  // Discard raw process output: diagnostics/URLs may contain capability secrets.
  child = spawn(process.execPath, [resolve(root, 'dist/src/main.js')], { env, cwd: scratch, stdio: 'ignore' });
  const origin = `http://127.0.0.1:${env.MHB_PORT}`;
  const request = (path, options = {}, authenticated = true) => fetch(origin + path, {
    ...options, signal: AbortSignal.timeout(12_000), redirect: 'error',
    headers: { ...(authenticated ? { Authorization: `Bearer ${key}` } : {}), ...options.headers },
  });
  stage = 'compiled-entrypoint-start';
  let ready = false;
  for (let i = 0; i < 100; i++) {
    assert.equal(child.exitCode, null, 'entrypoint exited');
    try {
      const health = await (await request('/health')).json();
      if (health.version === env.MHB_VERSION) { ready = true; break; }
    } catch { /* bounded startup polling, never retry an upstream tool call */ }
    await wait(100);
  }
  assert.ok(ready);
  assert.equal((await request('/ready')).status, 200);
  assert.equal((await request('/api/platform/v1/services', {}, false)).status, 401);
  const services = await (await request('/api/platform/v1/services')).json();
  const serialized = JSON.stringify(services);
  for (const profile of profiles) {
    assert.equal(services.services.find((item) => item.id === profile.id)?.configured, true);
    const binding = profile.binding;
    assert.ok(!serialized.includes(env[binding.urlEnv]));
    if (env[binding.tokenEnv]) assert.ok(!serialized.includes(env[binding.tokenEnv]));
  }
  receipt.checks.push('own-process-health', 'anonymous-denied', 'configured-bindings', 'discovery-excludes-upstream-secrets');
  const gateway = await connect(origin + '/mcp', key);
  assert.equal(gateway.getServerVersion().name, 'metahumotonic-platform');
  for (const profile of profiles) {
    stage = `owner-readback:${profile.id}`;
    const { binding } = profile;
    const direct = await connect(env[binding.urlEnv], env[binding.tokenEnv]);
    stage = `tool-inventory:${profile.id}`;
    const allowed = binding.tools.filter((tool) => tool.access === 'read').map((tool) => tool.name).sort();
    const directTools = (await direct.listTools()).tools;
    assert.ok(allowed.every((name) => directTools.some((tool) => tool.name === name)));
    const listed = await (await request(`/api/platform/v1/mcp/${profile.id}/tools`)).json();
    assert.deepEqual(listed.tools.map((tool) => tool.name).sort(), allowed);
    stage = `direct-call:${profile.id}`;
    const directResult = decode(await direct.callTool({ name: profile.tool, arguments: profile.args }, undefined, { timeout: 10_000 }));
    stage = `rest-call:${profile.id}`;
    const restResponse = await request(`/api/platform/v1/mcp/${profile.id}/call`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool: profile.tool, arguments: profile.args }),
    });
    assert.equal(restResponse.status, 200);
    const restResult = decode(await restResponse.json());
    stage = `mcp-call:${profile.id}`;
    const mcpResult = decode(await gateway.callTool({ name: 'platform_call', arguments: {
      server: profile.id, tool: profile.tool, arguments: profile.args,
    } }, undefined, { timeout: 15_000 }));
    stage = `result-parity:${profile.id}`;
    assert.equal(canonical(restResult), canonical(directResult));
    assert.equal(canonical(mcpResult), canonical(directResult));
    stage = `owner-subject:${profile.id}`;
    if (profile.id === 'ontology') assert.ok(hasSubject(directResult));
    if (profile.id === 'hspine') assert.ok(directResult.some((item) => item.ok === true &&
      typeof item.data?.mode === 'string' && typeof item.data?.execution_enabled === 'boolean' &&
      item.data?.mcp_tools?.includes('hspine_capabilities')));
    // Deliberately absent name: cannot become a real write even if upstream grows.
    const refused = await request(`/api/platform/v1/mcp/${profile.id}/call`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool: '__mhb_unconfigured_probe__', arguments: {} }),
    });
    assert.equal(refused.status, 403);
    receipt.servers.push({ id: profile.id, endpointEnv: binding.urlEnv, owner: direct.getServerVersion(),
      allowedTools: allowed, probeTool: profile.tool, arguments: profile.args,
      ownerResultSha256: digest(directResult), restResultSha256: digest(restResult), mcpResultSha256: digest(mcpResult),
      ...(profile.id === 'hspine' ? { observedBoundary: {
        mode: directResult[0].data.mode, executionEnabled: directResult[0].data.execution_enabled,
        revision: directResult[0].data.revision,
      } } : {}),
      checks: ['owner-subject-present', 'tool-allowlist', 'direct-rest-mcp-parity', 'non-allowlisted-call-denied'] });
  }
  receipt.status = 'PASS';
} catch (error) {
  // No raw transport message or result enters this public engineering record.
  receipt.failure = { stage, type: error?.constructor?.name ?? 'Error' };
} finally {
  for (const client of clients.reverse()) await client.close().catch(() => undefined);
  if (running()) {
    child.kill('SIGTERM');
    for (let i = 0; i < 50 && running(); i++) await wait(100);
    if (running()) { child.kill('SIGKILL'); await new Promise((done) => child.once('exit', done)); }
  }
  if (scratch) await rm(scratch, { recursive: true });
  receipt.cleanup = { childStopped: !child || child.exitCode !== null || child.signalCode !== null, temporaryDirectoryRemoved: true };
}
// Never overwrite a receipt supplied by a previous invocation if reservation failed.
if (reserved)
  await writeFile(receiptPath, JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600 });
console.log(JSON.stringify(receipt));
process.exitCode = receipt.status === 'PASS' ? 0 : 1;
