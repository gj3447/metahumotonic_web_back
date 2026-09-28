// Fault injection only into stores/processes created by this invocation.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MongoMemoryServer } from 'mongodb-memory-server-core';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const destination = process.argv[2];
if (!destination || process.argv.length !== 3) throw new Error('Usage: node scripts/check-native-runtime.mjs NEW_RECEIPT.json');
const receipt = { schema: 'metahumotonic/native-runtime-readback@1', observedAt: new Date().toISOString(), status: 'RUNNING',
  scope: 'Compiled TS process with owned disposable loopback Mongo/Redis; no containers or production configuration', checks: [] };
await writeFile(destination, JSON.stringify(receipt), { flag: 'wx', mode: 0o600 });
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const alive = (child) => child && child.exitCode === null && child.signalCode === null;
const stop = async (child) => {
  if (!alive(child)) return;
  child.kill('SIGCONT'); // Redis may still be paused after a failed assertion.
  child.kill('SIGTERM');
  for (let i = 0; i < 50 && alive(child); i++) await sleep(100);
  if (alive(child)) { child.kill('SIGKILL'); await new Promise((done) => child.once('exit', done)); }
};
const port = async () => {
  const socket = createServer();
  await new Promise((done) => socket.listen(0, '127.0.0.1', done));
  const value = socket.address().port;
  await new Promise((done) => socket.close(done));
  return value;
};
let mongo, redis, gateway, scratch, stage = 'setup';
try {
  const redisPort = await port();
  scratch = await mkdtemp(resolve(tmpdir(), 'mhb-native-runtime-'));
  let redisError;
  redis = spawn(process.env.MHB_TEST_REDIS_BINARY || 'redis-server', ['--bind', '127.0.0.1', '--port', String(redisPort), '--save', '', '--appendonly', 'no'], {
    cwd: scratch, stdio: 'ignore', env: { PATH: process.env.PATH ?? '', LD_LIBRARY_PATH: process.env.LD_LIBRARY_PATH ?? '' },
  });
  redis.on('error', (error) => { redisError = error; });
  mongo = await MongoMemoryServer.create({ binary: { version: '7.0.24' }, instance: { ip: '127.0.0.1' } });
  assert.ok(!redisError && alive(redis), 'disposable Redis did not start');
  const gatewayPort = await port();
  const marker = randomUUID();
  gateway = spawn(process.execPath, [resolve(root, 'dist/src/main.js')], { cwd: scratch, stdio: 'ignore', env: {
    PATH: process.env.PATH ?? '', MHB_HOST: '127.0.0.1', MHB_PORT: String(gatewayPort), MHB_VERSION: marker,
    MHB_MONGO_URI: mongo.getUri(), MHB_MONGO_DB: 'disposable_readiness', MHB_REDIS_URL: `redis://127.0.0.1:${redisPort}`,
    MHB_NEO4J_LIVE: 'false', MHB_FEEDBACK_REQUIRE_DURABLE: 'true',
  } });
  const get = (path) => fetch(`http://127.0.0.1:${gatewayPort}${path}`, { signal: AbortSignal.timeout(4000) });
  const check = async (name, status, expected) => {
    stage = name;
    let observed;
    for (let i = 0; i < 60; i++) {
      assert.ok(alive(gateway), 'owned backend exited');
      try {
        const response = await get('/ready');
        const body = await response.json();
        if (response.status === status && Object.entries(expected).every(([key, value]) => body[key] === value)) { observed = body; break; }
      } catch { /* bounded reads; no write retry */ }
      await sleep(100);
    }
    assert.ok(observed, 'readiness did not reach expected state');
    assert.equal((await (await get('/health')).json()).version, marker);
    receipt.checks.push({ name, status, mongoLive: observed.mongo_live, redisLive: observed.redis_live, ownProcessLive: true });
  };
  await check('healthy', 200, { mongo_required: true, mongo_live: true, redis_required: true, redis_live: true });
  await mongo.stop({ doCleanup: false, force: false });
  await check('mongo-down', 503, { mongo_live: false, redis_live: true });
  await mongo.start(true);
  await check('mongo-recovered', 200, { mongo_live: true, redis_live: true });
  redis.kill('SIGSTOP');
  await check('redis-unresponsive', 503, { mongo_live: true, redis_live: false });
  redis.kill('SIGCONT');
  await check('redis-recovered', 200, { mongo_live: true, redis_live: true });
  receipt.sourceSha256 = {};
  for (const path of ['scripts/check-native-runtime.mjs', 'dist/src/ports/NativeReadiness.js', 'dist/src/server/Composition.js', 'dist/src/server/Handlers.js'])
    receipt.sourceSha256[path] = createHash('sha256').update(await readFile(resolve(root, path))).digest('hex');
  receipt.status = 'PASS';
} catch (error) {
  receipt.status = 'FAIL';
  receipt.failure = { stage, type: error?.constructor?.name ?? 'Error' };
} finally {
  try {
    await stop(gateway);
    await stop(redis);
    if (mongo) await mongo.stop();
    if (scratch) await rm(scratch, { recursive: true });
    receipt.cleanup = { processesStopped: !alive(gateway) && !alive(redis) && (!mongo || mongo.state === 'new' || mongo.state === 'stopped'), temporaryDirectoryRemoved: true };
    assert.equal(receipt.cleanup.processesStopped, true);
  } catch { receipt.status = 'FAIL_CLEANUP'; }
  await writeFile(destination, JSON.stringify(receipt, null, 2) + '\n');
  console.log(JSON.stringify(receipt));
}
process.exitCode = receipt.status === 'PASS' ? 0 : 1;
