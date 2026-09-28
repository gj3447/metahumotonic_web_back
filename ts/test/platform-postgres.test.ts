import { HttpApiBuilder } from "@effect/platform"
import { ConfigProvider, Effect, Either, Layer, Redacted } from "effect"
import { randomBytes } from "node:crypto"
import { readFileSync } from "node:fs"
import { spawn, type ChildProcess } from "node:child_process"
import { createServer } from "node:net"
import { once } from "node:events"
import { afterAll, afterEach, beforeEach, describe, expect, it } from "vitest"
import type { Pool } from "pg"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js"
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js"
import { configFromEnv } from "../src/Config.js"
import { decodeCatalog, type PlatformObservation } from "../src/domain/PlatformGraph.js"
import { contentDigest } from "../src/platform/Digest.js"
import { PlatformConfigTag, type PlatformConfig } from "../src/platform/Config.js"
import { createPlatformPool, importPlatformCatalog, makePostgresInventory, migratePlatform } from "../src/ports/PlatformInventoryPostgres.js"
import { PlatformInventoryStoreTag } from "../src/ports/PlatformInventoryStore.js"
import { configOf, webHandlerLayer } from "../src/server/Composition.js"

const dsn = process.env["MHB_TEST_PLATFORM_DATABASE_URL"]
if (process.env["MHB_REQUIRE_PLATFORM_POSTGRES_TESTS"] === "1" && !dsn) throw new Error("disposable platform PostgreSQL URL is required")
if (dsn) {
  const url = new URL(dsn)
  if (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) || url.username !== "mhb_platform_test") throw new Error("only the explicit loopback test cluster is permitted")
}
const catalog = Either.getOrThrow(decodeCatalog(JSON.parse(readFileSync(new URL("../config/platform-catalog.json", import.meta.url), "utf8"))))
const appConfig = Effect.runSync(configFromEnv.pipe(Effect.withConfigProvider(ConfigProvider.fromMap(new Map()))))
const READ = "platform-postgres-test-read-credential", WRITE = "platform-postgres-test-write-credential"
const cfg: PlatformConfig = { catalog, catalogDigest: contentDigest(catalog), readKey: Redacted.make(READ), writeKey: Redacted.make(WRITE),
  legacyOrigin: null, legacyRequired: false, ontologyRequired: false, bindings: [], maxBodyBytes: 524288, timeoutMs: 1000, shadowReadOnly: false }
const observation = (id: string, patch: Partial<PlatformObservation> = {}): PlatformObservation => ({
  id: `obs:test:${id}`, subjectId: "program:usl", check: "readiness", outcome: "healthy",
  observedAt: new Date(Date.now() - 1000).toISOString(), expiresAt: new Date(Date.now() + 60000).toISOString(),
  evidence: [{ source: "test:disposable-postgres", authority: "SYSTEM_DERIVED", observedAt: new Date(Date.now() - 1000).toISOString(), note: "Test observation; not a production health assertion" }], ...patch
})
const batch = (id: string, observations = [observation(id)]) => ({ receiptId: `receipt:test:${id}`, observations })
const leftTag = async <A, E>(effect: Effect.Effect<A, E>) => {
  const either = await Effect.runPromise(Effect.either(effect))
  expect(either._tag).toBe("Left")
  return Either.isLeft(either) ? (either.left as { _tag: string })._tag : null
}

describe.skipIf(!dsn)("PostgreSQL inventory, independent readback and shared HTTP/MCP composition", () => {
  const admin = dsn ? createPlatformPool(dsn) : undefined
  let pool: Pool, uri: string, db: string
  const disposers: Array<() => Promise<unknown>> = []
  beforeEach(async () => {
    db = `mhb_platform_test_${randomBytes(6).toString("hex")}`
    await admin!.query(`CREATE DATABASE ${db}`)
    const url = new URL(dsn!); url.pathname = `/${db}`; uri = url.href
    pool = createPlatformPool(uri)
    disposers.push(() => pool.end())
  })
  afterEach(async () => {
    for (const dispose of disposers.splice(0).reverse()) await dispose()
    await admin!.query(`DROP DATABASE ${db} WITH (FORCE)`)
  })
  afterAll(async () => { await admin?.end() })
  const seed = async () => { await migratePlatform(pool); return importPlatformCatalog(pool, catalog) }
  const replica = () => { const next = createPlatformPool(uri); disposers.push(() => next.end()); return next }
  const web = (storagePool = pool) => {
    const service = HttpApiBuilder.toWebHandler(webHandlerLayer({ config: configOf(appConfig), platformConfig: Layer.succeed(PlatformConfigTag, cfg),
      inventoryStore: Layer.succeed(PlatformInventoryStoreTag, makePostgresInventory(storagePool, cfg)) }))
    disposers.push(service.dispose)
    return { ...service, request: (path: string, key: string | null = READ, init: RequestInit = {}) => service.handler(new Request(`http://localhost${path}`, {
      ...init, headers: { ...(key ? { "x-api-key": key } : {}), ...init.headers }
    })) }
  }

  it("does not migrate on read/startup, applies migrations once and refuses checksum drift", async () => {
    const store = makePostgresInventory(pool, cfg)
    expect(await Effect.runPromise(store.readiness)).toEqual({ platform_postgres_required: true, platform_postgres_live: false })
    expect(await leftTag(store.read)).toBe("Unavailable")
    expect((await pool.query("SELECT to_regnamespace('mhb_platform') AS namespace")).rows[0].namespace).toBeNull()
    expect((await migratePlatform(pool)).applied).toBe(true)
    expect((await migratePlatform(pool)).applied).toBe(false)
    // Merely migrating is not ready: this deployment's Git-pinned definition must be imported.
    expect(await leftTag(store.read)).toBe("Unavailable")
    await importPlatformCatalog(pool, catalog)
    expect((await Effect.runPromise(store.readiness)).platform_postgres_live).toBe(true)
    await pool.query("UPDATE mhb_platform.schema_migrations SET checksum='changed'")
    await expect(migratePlatform(pool)).rejects.toMatchObject({ _tag: "Unavailable" })
    expect(await leftTag(store.read)).toBe("Unavailable")
  })
  it("persists all assets and historical observations, preserves JSONB identity and rejects mutation in SQL", async () => {
    const receipt = await seed()
    expect(receipt).toMatchObject({ replayed: false, observationCount: 109, insertedCount: 109 })
    expect(await importPlatformCatalog(pool, catalog)).toMatchObject({ replayed: true, digest: receipt.digest })
    const independent = replica()
    expect((await independent.query("SELECT count(*)::int AS n FROM mhb_platform.asset_versions")).rows[0].n).toBe(197)
    expect((await independent.query("SELECT count(*)::int AS n FROM mhb_platform.observations")).rows[0].n).toBe(109)
    const stored = (await independent.query("SELECT document FROM mhb_platform.catalog_versions")).rows[0].document
    expect(contentDigest(stored)).toBe(contentDigest(catalog))
    expect(await Effect.runPromise(makePostgresInventory(independent, cfg).read)).toMatchObject({ source: "postgres", definitionDigest: contentDigest(catalog) })
    for (const sql of ["DELETE FROM mhb_platform.observations", "UPDATE mhb_platform.catalog_versions SET observed_at=now()", "DELETE FROM mhb_platform.ingest_receipts"]) {
      await expect(independent.query(sql)).rejects.toMatchObject({ code: "55000" })
    }
  })
  it("serializes concurrent replica retries and rejects changed receipt or observation IDs atomically", async () => {
    await seed()
    const a = makePostgresInventory(pool, cfg), b = makePostgresInventory(replica(), cfg), input = batch("concurrent")
    const results = await Promise.all(Array.from({ length: 12 }, (_, i) => Effect.runPromise((i % 2 ? a : b).append(input, Date.now()))))
    expect(results.filter((r) => !r.replayed)).toHaveLength(1)
    expect(new Set(results.map((r) => r.digest)).size).toBe(1)
    expect((await pool.query("SELECT count(*)::int AS n FROM mhb_platform.observations WHERE id=$1", [input.observations[0]!.id])).rows[0].n).toBe(1)
    const changed = { ...input.observations[0]!, outcome: "failed" as const }
    expect(await leftTag(a.append({ ...input, observations: [changed] }, Date.now()))).toBe("Conflict")
    expect(await leftTag(a.append(batch("conflicting-observation", [observation("rollback"), changed]), Date.now()))).toBe("Conflict")
    expect((await pool.query("SELECT count(*)::int AS n FROM mhb_platform.ingest_receipts WHERE id=$1", ["receipt:test:conflicting-observation"])).rows[0].n).toBe(0)
    expect((await pool.query("SELECT count(*)::int AS n FROM mhb_platform.observations WHERE id='obs:test:rollback'")).rows[0].n).toBe(0)
    expect(await Effect.runPromise(a.append(batch("same-observation", input.observations), Date.now()))).toMatchObject({ insertedCount: 0, replayed: false })
    expect(await Effect.runPromise(a.receipt(input.receiptId))).toMatchObject({ insertedCount: 1, digest: results[0]!.digest })
  })
  it("rejects unknown subjects, future times, invalid expiry and duplicate batch identities without saving receipts", async () => {
    await seed()
    const store = makePostgresInventory(pool, cfg)
    for (const patch of [{ subjectId: "program:missing" }, { observedAt: "9999-01-01T00:00:00Z", expiresAt: "9999-01-02T00:00:00Z" },
      { expiresAt: "2000-01-01T00:00:00Z" }, { observedAt: "2026-02-30T00:00:00Z" }]) {
      expect(await leftTag(store.append(batch("invalid", [observation("invalid", patch)]), Date.now()))).toBe("BadRequest")
    }
    const duplicate = observation("duplicate")
    expect(await leftTag(store.append(batch("duplicates", [duplicate, duplicate]), Date.now()))).toBe("BadRequest")
    expect(await leftTag(store.receipt("receipt:test:invalid"))).toBe("NotFound")
  })
  it("keeps immutable catalog versions pinned per deployment while selecting latest observations by event time", async () => {
    await seed()
    const store = makePostgresInventory(pool, cfg), latest = observation("latest"), older = observation("older", {
      observedAt: new Date(Date.now() - 10000).toISOString(), outcome: "failed" })
    const latestBatch = batch("latest", [latest])
    await Effect.runPromise(store.append(latestBatch, Date.now()))
    await Effect.runPromise(store.append(batch("older", [older]), Date.now()))
    const revised = { ...catalog, nodes: catalog.nodes.map((n) => n.id === "program:usl" ? { ...n, name: "USL revised test definition" } : n) }
    await importPlatformCatalog(pool, revised)
    const read = await Effect.runPromise(store.read), newer = await Effect.runPromise(makePostgresInventory(pool, { catalog: revised }).read)
    expect(read.catalog.nodes.find((n) => n.id === "program:usl")?.name).toBe("USL")
    expect(newer.catalog.nodes.find((n) => n.id === "program:usl")?.name).toBe("USL revised test definition")
    for (const view of [read, newer]) expect(view.catalog.observations?.find((o) => o.subjectId === "program:usl" && o.check === "readiness")?.id).toBe(latest.id)
    expect(newer.definitionDigest).not.toBe(read.definitionDigest)
    expect(await Effect.runPromise(makePostgresInventory(pool, { catalog: revised }).append(latestBatch, Date.now())))
      .toMatchObject({ replayed: true, definitionDigest: read.definitionDigest })
  })
  it("pages history without duplicates during new ingestion and retains receipts after replacing the pool", async () => {
    await seed()
    const store = makePostgresInventory(pool, cfg)
    await Effect.runPromise(store.append(batch("history", [observation("h1"), observation("h2"), observation("h3")]), Date.now()))
    const first = await Effect.runPromise(store.history({ subjectId: "program:usl", limit: 2 }))
    expect(first.items).toHaveLength(2)
    expect(first.nextBefore).not.toBeNull()
    await Effect.runPromise(store.append(batch("h4"), Date.now()))
    const reconnected = makePostgresInventory(replica(), cfg)
    const next = await Effect.runPromise(reconnected.history({ subjectId: "program:usl", limit: 2, before: first.nextBefore! }))
    expect(next.items.map((i) => i.observation.id)).toEqual(["obs:test:h1"])
    expect(await Effect.runPromise(reconnected.receipt("receipt:test:history"))).toMatchObject({ observationCount: 3 })
  })
  it("enforces HTTP write authority and gives REST and the official MCP client the same stored data", async () => {
    await seed()
    const service = web(), input = batch("http")
    const post = (key: string | null, value = input) => service.request("/api/platform/v1/observations", key, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(value) })
    expect((await post(null)).status).toBe(401)
    expect((await post(READ)).status).toBe(403)
    const saved = await post(WRITE)
    expect(saved.status).toBe(200)
    expect(await saved.json()).toMatchObject({ receiptId: input.receiptId, insertedCount: 1 })
    expect(await (await post(WRITE)).json()).toMatchObject({ replayed: true })
    expect((await post(WRITE, { ...input, observations: [observation("different")] })).status).toBe(409)
    expect((await service.request(`/api/platform/v1/receipts/${input.receiptId}`)).status).toBe(200)
    expect((await service.request("/api/platform/v1/observations?limit=101")).status).toBe(400)
    const http = await (await service.request("/api/platform/v1/inventory?q=usl&kind=program")).json() as Record<string, unknown>
    expect(http["source"]).toBe("postgres")
    const rdf = await service.request("/api/platform/v1/graph/jsonld")
    expect(rdf.headers.get("cache-control")).toBe("private, no-store")
    expect(await rdf.json()).toMatchObject({ "mh:source": "postgres" })
    const client = new Client({ name: "postgres-inventory-test", version: "1" })
    const transport = new StreamableHTTPClientTransport(new URL("http://localhost/mcp"), {
      fetch: (url, init) => service.handler(new Request(url, init)), requestInit: { headers: { Authorization: `Bearer ${READ}` } }
    })
    await client.connect(transport as Transport); disposers.push(() => client.close())
    const mcp = await client.callTool({ name: "platform_inventory", arguments: { q: "usl", kind: "program" } })
    const text = (mcp.content as Array<{ text: string }>)[0]!.text
    expect(JSON.parse(text)).toMatchObject({ digest: http["digest"], definitionDigest: http["definitionDigest"], items: http["items"] })
    const history = await client.callTool({ name: "platform_observations", arguments: { subjectId: "program:usl" } })
    expect(JSON.parse((history.content as Array<{ text: string }>)[0]!.text)).toEqual(await (await service.request("/api/platform/v1/observations?subjectId=program:usl")).json())
  })
  it("fails closed on PostgreSQL loss while public learning and process liveness remain available", async () => {
    await seed()
    const broken = createPlatformPool(uri), service = web(broken)
    expect((await service.request("/ready")).status).toBe(200)
    await broken.end()
    const response = await service.request("/ready")
    expect(response.status).toBe(503)
    expect(await response.json()).toMatchObject({ platform_postgres_required: true, platform_postgres_live: false })
    expect((await service.request("/api/platform/v1/inventory")).status).toBe(503)
    expect((await service.request("/api/platform/v1/observations", WRITE, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(batch("outage")) })).status).toBe(503)
    expect((await service.request("/health")).status).toBe(200)
    expect((await service.request("/api/public/v1/hub")).status).toBe(200)
  })
  it("supports a runtime role with no DDL, update, delete or catalog import privileges", async () => {
    await seed()
    const role = `mhb_platform_reader_${randomBytes(5).toString("hex")}`
    await admin!.query(`CREATE ROLE ${role} LOGIN PASSWORD 'disposable-only'`)
    try {
      await pool.query(`GRANT USAGE ON SCHEMA mhb_platform TO ${role}`)
      await pool.query(`GRANT SELECT ON ALL TABLES IN SCHEMA mhb_platform TO ${role}`)
      await pool.query(`GRANT INSERT ON mhb_platform.observations,mhb_platform.ingest_receipts TO ${role}`)
      await pool.query(`GRANT USAGE ON ALL SEQUENCES IN SCHEMA mhb_platform TO ${role}`)
      const url = new URL(uri); url.username = role; url.password = "disposable-only"
      const restricted = createPlatformPool(url.href)
      try {
        const store = makePostgresInventory(restricted, cfg)
        expect((await Effect.runPromise(store.readiness)).platform_postgres_live).toBe(true)
        expect(await Effect.runPromise(store.append(batch("restricted"), Date.now()))).toMatchObject({ insertedCount: 1 })
        await expect(importPlatformCatalog(restricted, catalog)).rejects.toMatchObject({ code: "42501" })
        await expect(restricted.query("DELETE FROM mhb_platform.observations")).rejects.toMatchObject({ code: "42501" })
      } finally { await restricted.end() }
    } finally { await pool.query(`DROP OWNED BY ${role}`); await admin!.query(`DROP ROLE ${role}`) }
  })
  it("uses the PostgreSQL Layer on real compiled sockets and reads persisted state after process restart", async () => {
    // Exercise the shipped administration CLI with the compiled catalog, then
    // start the production composition. No test-only seeding shortcut here.
    for (const command of ["migrate", "import"]) {
      const cli = spawn(process.execPath, [new URL("../scripts/platform-db.mjs", import.meta.url).pathname, command], {
        env: { PATH: process.env["PATH"] ?? "", MHB_PLATFORM_DATABASE_URL: uri }, stdio: ["ignore", "pipe", "pipe"]
      })
      let output = ""
      cli.stdout.on("data", (chunk) => { output += String(chunk) })
      const [code] = await once(cli, "close")
      expect(code).toBe(0)
      expect(JSON.parse(output)).toMatchObject({ status: "PASS", operation: command })
    }
    const start = async () => {
      const socket = createServer(); socket.listen(0, "127.0.0.1"); await once(socket, "listening")
      const port = (socket.address() as { port: number }).port; await new Promise<void>((resolve) => socket.close(() => resolve()))
      const child = spawn(process.execPath, [new URL("../dist/src/main.js", import.meta.url).pathname], { env: {
        PATH: process.env["PATH"] ?? "", MHB_HOST: "127.0.0.1", MHB_PORT: String(port),
        MHB_PLATFORM_DATABASE_URL: uri, MHB_PLATFORM_DATABASE_REQUIRED: "true", MHB_PLATFORM_READ_KEY: READ, MHB_PLATFORM_WRITE_KEY: WRITE
      }, stdio: "ignore" })
      const stop = async (process: ChildProcess) => { if (process.exitCode === null && process.signalCode === null) { const exited = once(process, "exit"); process.kill("SIGTERM"); await exited } }
      disposers.push(() => stop(child))
      const base = `http://127.0.0.1:${port}`
      for (let attempt = 0; attempt < 100; attempt++) {
        if (child.exitCode !== null) throw new Error("compiled server exited")
        try { if ((await fetch(`${base}/ready`, { signal: AbortSignal.timeout(500) })).ok) return { base, stop: () => stop(child) } } catch { /* waiting for own process */ }
        await new Promise((resolve) => setTimeout(resolve, 50))
      }
      throw new Error("compiled PostgreSQL server startup timed out")
    }
    const first = await start(), input = batch("socket")
    expect((await fetch(`${first.base}/api/platform/v1/observations`, { method: "POST", headers: { "content-type": "application/json", "x-api-key": WRITE }, body: JSON.stringify(input) })).status).toBe(200)
    await first.stop()
    const second = await start()
    const response = await fetch(`${second.base}/api/platform/v1/receipts/${input.receiptId}`, { headers: { "x-api-key": READ } })
    expect(response.status).toBe(200)
    expect(await response.json()).toMatchObject({ receiptId: input.receiptId, insertedCount: 1 })
    expect((await pool.query("SELECT count(*)::int AS n FROM mhb_platform.observations WHERE id=$1", [input.observations[0]!.id])).rows[0].n).toBe(1)
  })
})
