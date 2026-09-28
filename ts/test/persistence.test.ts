import { ConfigProvider, Effect, Layer, ManagedRuntime, Redacted } from "effect"
import { MongoClient } from "mongodb"
import { HttpApiBuilder } from "@effect/platform"
import { createClient } from "redis"
import { randomUUID } from "node:crypto"
import { beforeAll, afterAll, describe, expect, it } from "vitest"
import { configFromEnv } from "../src/Config.js"
import { configOf, webHandlerLayer } from "../src/server/Composition.js"
import { FeedbackStoreTag } from "../src/ports/FeedbackStore.js"
import { FeedbackStoreLive } from "../src/ports/FeedbackStoreMongo.js"
import { MongoLive } from "../src/ports/Mongo.js"
import { McpRegistryLive, McpRegistryTag } from "../src/ports/McpRegistry.js"
import { FeedbackLimiter } from "../src/ports/RateLimiter.js"
import { RedisLive, redisLimiterLayer } from "../src/ports/RedisLimiter.js"

const mongoUri = process.env["MHB_TEST_MONGO_URI"] ?? ""
const redisUri = process.env["MHB_TEST_REDIS_URL"] ?? ""
const enabled = mongoUri !== "" && redisUri !== ""
if (process.env["MHB_REQUIRE_PERSISTENCE_TESTS"] === "1" && !enabled) throw new Error("explicit disposable Mongo and Redis test endpoints are required")

describe.skipIf(!enabled)("real disposable persistence (no production configuration)", () => {
  const dbName = `mhb_test_${randomUUID().replaceAll("-", "")}`
  const cfg = { ...Effect.runSync(configFromEnv.pipe(Effect.withConfigProvider(ConfigProvider.fromMap(new Map())))),
    mongoUri: Redacted.make(mongoUri), mongoDb: dbName, redisUrl: Redacted.make(redisUri), feedbackRequireDurable: true }
  const config = configOf(cfg)
  const storageLayer = Layer.mergeAll(FeedbackStoreLive, McpRegistryLive).pipe(
    Layer.provide(MongoLive), Layer.provide(config)
  )
  let inspector: MongoClient
  beforeAll(async () => { inspector = await new MongoClient(mongoUri).connect() })
  afterAll(async () => { await inspector.db(dbName).dropDatabase(); await inspector.close() })

  it("reports actual configured Mongo and Redis readiness through the shared HTTP composition", async () => {
    const web = HttpApiBuilder.toWebHandler(webHandlerLayer({ config }))
    try {
      let body: unknown
      let status = 0
      for (let attempt = 0; attempt < 50; attempt++) {
        const response = await web.handler(new Request("http://localhost/ready"))
        status = response.status
        body = await response.json()
        if (status === 200) break
        await new Promise((resolve) => setTimeout(resolve, 20))
      }
      expect(status).toBe(200)
      expect(body).toMatchObject({ mongo_required: true, mongo_live: true, redis_required: true, redis_live: true, degraded: false })
    } finally { await web.dispose() }
  })

  it("reads Mongo independently, survives a new runtime, performs CAS triage, and erases contact data", async () => {
    const id = randomUUID().replaceAll("-", "")
    const first = ManagedRuntime.make(storageLayer)
    try {
      const result = await first.runPromise(Effect.flatMap(FeedbackStoreTag, (store) => store.save({
        type: "general", subject: "Persistence", body: "Read independently", email: "test@example.invalid", source_path: "/", contact_consent: true,
        honeypot: "", turnstile_token: ""
      }, { id, now: new Date().toISOString() })))
      expect(result).toEqual({ id, status: "stored" })
      const stored = await inspector.db(dbName).collection<{ _id: string; email: string }>(cfg.mongoFeedbackCollection).findOne({ _id: id })
      expect(stored).toMatchObject({ email: "test@example.invalid" })
      expect(stored).not.toHaveProperty("turnstile_token")
      expect(stored).not.toHaveProperty("honeypot")
    } finally { await first.dispose() }
    const second = ManagedRuntime.make(storageLayer)
    try {
      expect(await second.runPromise(Effect.flatMap(FeedbackStoreTag, (store) => store.list({ limit: 10 })))).toMatchObject({ items: [{ id, email: "test@example.invalid" }] })
      const outcomes = await second.runPromise(Effect.gen(function* () {
        const store = yield* FeedbackStoreTag
        return yield* Effect.all(["one", "two"].map((operatorNote) => Effect.either(store.triage(id, { status: "reviewed", operatorNote, now: new Date().toISOString() }))), { concurrency: 2 })
      }))
      expect(outcomes.filter((outcome) => outcome._tag === "Right")).toHaveLength(1)
      expect(outcomes.filter((outcome) => outcome._tag === "Left")).toHaveLength(1)
      expect(await second.runPromise(Effect.flatMap(FeedbackStoreTag, (store) => store.erase(id)))).toBe(true)
      expect(await inspector.db(dbName).collection<{ _id: string; email: string }>(cfg.mongoFeedbackCollection).findOne({ _id: id })).toBeNull()
      const indexes = await inspector.db(dbName).collection(cfg.mongoFeedbackCollection).listIndexes().toArray()
      expect(indexes.some((index) => index.expireAfterSeconds === cfg.feedbackTtlDays * 86400)).toBe(true)
    } finally { await second.dispose() }
  })

  it("reads the existing Python registry collection and exposes only the vault ciphertext contract", async () => {
    const collection = inspector.db(dbName).collection<Record<string, unknown> & { _id: string }>(cfg.mcpRegistryCollection)
    await collection.insertMany([
      { _id: "fixture", kind: "server", name: "fixture", category: "document", status: "verified", verified_at: new Date().toISOString() },
      { _id: "manifest_meta", kind: "meta", notes: ["legacy-compatible"], updated: "2026-09-27" },
      { _id: "credential_vault", kind: "vault", blob: "ciphertext", kdf: { iterations: 600000, salt: "salt" }, services: ["fixture"], plaintext: "MUST-NOT-LEAK" }
    ])
    const runtime = ManagedRuntime.make(storageLayer)
    try {
      const result = await runtime.runPromise(Effect.flatMap(McpRegistryTag, (registry) => registry.read("manifest")))
      expect(result).toMatchObject({ source: "live", notes: ["legacy-compatible"], servers: [{ name: "fixture" }] })
      const vault = await runtime.runPromise(Effect.flatMap(McpRegistryTag, (registry) => registry.read("vault")))
      expect(JSON.stringify(vault)).not.toContain("MUST-NOT-LEAK")
      expect(vault).toMatchObject({ source: "live", vault: { blob: "ciphertext" } })
    } finally { await runtime.dispose() }
  })

  it("does not acknowledge a failed Mongo write as durable", async () => {
    const unavailable = configOf({ ...cfg, mongoUri: Redacted.make("mongodb://127.0.0.1:1") })
    const runtime = ManagedRuntime.make(FeedbackStoreLive.pipe(Layer.provide(MongoLive), Layer.provide(unavailable)))
    try {
      const result = await runtime.runPromise(Effect.either(Effect.flatMap(FeedbackStoreTag, (store) => store.save({
        type: "general", subject: "Outage", body: "Must fail", email: "", source_path: "/", contact_consent: false, honeypot: "", turnstile_token: ""
      }, { id: randomUUID().replaceAll("-", ""), now: new Date().toISOString() }))))
      expect(result).toMatchObject({ _tag: "Left", left: { _tag: "Unavailable" } })
    } finally { await runtime.dispose() }
  }, 15000)

  it("shares Python's Redis window across independent TS replicas and survives their restart", async () => {
    const clientKey = `test-${randomUUID()}`
    const inspector = await createClient({ url: redisUri }).connect()
    const legacyKey = `mhb:rl:${clientKey}`
    await inspector.zAdd(legacyKey, [
      { score: Date.now(), value: "python-request-one" },
      { score: Date.now(), value: "python-request-two" }
    ])
    await inspector.expire(legacyKey, 60)
    const layer = redisLimiterLayer(FeedbackLimiter, "feedback", { maxEvents: 3, windowSeconds: 60, failClosed: true })
      .pipe(Layer.provide(RedisLive), Layer.provide(config))
    const runtimes = [ManagedRuntime.make(layer), ManagedRuntime.make(layer)]
    const ready = async (runtime: typeof runtimes[number]) => {
      for (let attempt = 0; attempt < 50; attempt++) {
        if (await runtime.runPromise(Effect.flatMap(FeedbackLimiter, (limiter) => limiter.ready))) return
        await new Promise((resolve) => setTimeout(resolve, 20))
      }
      throw new Error("disposable Redis not ready")
    }
    try {
      await Promise.all(runtimes.map(ready))
      const outcomes = await Promise.all(Array.from({ length: 20 }, (_, index) => runtimes[index % 2]!.runPromise(Effect.flatMap(FeedbackLimiter, (limiter) => limiter.check(clientKey)))))
      expect(outcomes.filter((outcome) => outcome.allowed)).toHaveLength(1)
      expect(await inspector.zCard(legacyKey)).toBe(3)
      await Promise.all(runtimes.map((runtime) => runtime.dispose()))
      const fresh = ManagedRuntime.make(layer)
      try {
        await ready(fresh)
        expect(await fresh.runPromise(Effect.flatMap(FeedbackLimiter, (limiter) => limiter.check(clientKey)))).toMatchObject({ allowed: false })
      } finally { await fresh.dispose() }
    } finally {
      await Promise.all(runtimes.map((runtime) => runtime.dispose()))
      await inspector.del(legacyKey)
      await inspector.quit()
    }
  })
})
