import { MongoMemoryServer } from "mongodb-memory-server-core"
import { spawn } from "node:child_process"
import { createServer } from "node:net"
import { createClient } from "redis"

// Standalone verification provisions disposable, loopback-only databases.
// Neither application .env nor production addresses are consulted.
const socket = createServer()
await new Promise((resolve) => socket.listen(0, "127.0.0.1", resolve))
const port = socket.address().port
await new Promise((resolve) => socket.close(resolve))
const redis = spawn(process.env.MHB_TEST_REDIS_BINARY || "redis-server", ["--bind", "127.0.0.1", "--port", String(port), "--save", "", "--appendonly", "no"], { stdio: ["ignore", "ignore", "pipe"] })
let redisError
redis.on("error", (error) => { redisError = error })
let mongo
try {
  const client = createClient({ url: `redis://127.0.0.1:${port}`, socket: { connectTimeout: 300, reconnectStrategy: false } })
  client.on("error", () => {})
  let ready = false
  for (let attempt = 0; attempt < 50; attempt++) {
    if (redisError || redis.exitCode !== null) throw new Error("disposable Redis failed to start; set MHB_TEST_REDIS_BINARY")
    try { await client.connect(); await client.ping(); ready = true; break } catch { await new Promise((resolve) => setTimeout(resolve, 100)) }
  }
  if (!ready) throw new Error("disposable Redis startup timed out")
  client.destroy()
  mongo = await MongoMemoryServer.create({ binary: { version: "7.0.24" }, instance: { ip: "127.0.0.1" } })
  const test = spawn(process.execPath, ["node_modules/vitest/vitest.mjs", "run", "test/persistence.test.ts"], {
    stdio: "inherit", env: { ...process.env, MHB_TEST_MONGO_URI: mongo.getUri(), MHB_TEST_REDIS_URL: `redis://127.0.0.1:${port}`, MHB_REQUIRE_PERSISTENCE_TESTS: "1" }
  })
  process.exitCode = await new Promise((resolve) => test.on("exit", (code) => resolve(code ?? 1)))
} finally {
  if (mongo) await mongo.stop()
  redis.kill("SIGTERM")
}
