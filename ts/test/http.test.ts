/**
 * HTTP-level tests.
 *
 * These drive the *real* API — the same `HttpApiBuilder` app `main.ts` serves —
 * through `toWebHandler`, so routing, schema decoding, status-code mapping and
 * handler wiring are all exercised. No socket, no infra: the ports are swapped
 * for their in-memory layers by supplying a different set of `Layer`s.
 *
 * That substitution is the whole argument for the `R` channel. Nothing is
 * patched or monkeyed; a different composition root is simply passed in.
 */
import { HttpApiBuilder, HttpServer } from "@effect/platform"
import { Layer, Redacted } from "effect"
import { afterAll, describe, expect, it } from "vitest"
import { Api } from "../src/api/Api.js"
import { AppConfigTag, type AppConfig } from "../src/Config.js"
import { FeedbackStoreMemory } from "../src/ports/FeedbackStore.js"
import { IdsDeterministic } from "../src/ports/Ids.js"
import { KgPortSnapshot } from "../src/ports/KgPort.js"
import { KgWritePortDryOnly } from "../src/ports/KgWritePort.js"
import { SchemaGuardOffline } from "../src/ports/SchemaGuard.js"
import { FeedbackLimiter, layerInProcess } from "../src/ports/RateLimiter.js"
import { HandlersLive } from "../src/server/Handlers.js"

const ADMIN_KEY = "admin-key-that-is-at-least-32-bytes!!"

const testConfig: AppConfig = {
  host: "127.0.0.1",
  port: 0,
  version: "test-1.0.0",
  neo4jUri: "",
  neo4jFallbackUris: [],
  neo4jUser: "neo4j",
  neo4jPassword: Redacted.make(""),
  neo4jDatabase: "neo4j",
  neo4jLive: false,
  kgReadKey: Redacted.make(""),
  kgWriteKey: Redacted.make(""),
  kgProxyMaxRows: 1000,
  kgQueryTimeoutSeconds: 10,
  mongoUri: Redacted.make(""),
  mongoDb: "m",
  mongoFeedbackCollection: "f",
  feedbackTtlDays: 365,
  feedbackRequireDurable: false,
  feedbackAdminKey: Redacted.make(ADMIN_KEY),
  feedbackMaxPerWindow: 3,
  feedbackWindowSeconds: 600,
  mcpRegistryCollection: "mcp",
  mcpRegistryCacheTtlSeconds: 300,
  statsCacheTtlSeconds: 120,
  researchCacheTtlSeconds: 300,
  cacheMaxEntries: 512,
  researchMaxOffset: 10000,
  redisUrl: Redacted.make(""),
  wikiPublicWrites: false,
  wikiDatabaseUrl: Redacted.make(""),
  wikiSessionSecret: Redacted.make(""),
  wikiModerationAdminKey: Redacted.make(""),
  wikiSessionTtlSeconds: 43200,
  wikiSessionCookieSecure: true,
  wikiRequireRedis: true,
  wikiSessionMaxPerWindow: 10,
  wikiSessionWindowSeconds: 600,
  wikiMutationMaxPerWindow: 30,
  wikiMutationWindowSeconds: 60,
  wikiReadMaxPerWindow: 180,
  wikiReadWindowSeconds: 60,
  wikiMaxBodyBytes: 524288,
  wikiMaxOffset: 10000,
  metricsEnabled: false,
  logJson: false,
  turnstileSecret: Redacted.make(""),
  turnstileHostname: "metahumotonic.com",
  turnstileAction: "feedback_submit",
  turnstileFailOpen: false,
  corsOrigins: ["https://metahumotonic.com"],
  trustProxy: false
}

const ConfigTest = Layer.succeed(AppConfigTag, testConfig)

const PortsTest = Layer.mergeAll(
  KgPortSnapshot,
  KgWritePortDryOnly.pipe(Layer.provideMerge(SchemaGuardOffline)),
  FeedbackStoreMemory,
  IdsDeterministic(),
  layerInProcess(FeedbackLimiter, {
    maxEvents: testConfig.feedbackMaxPerWindow,
    windowSeconds: testConfig.feedbackWindowSeconds,
    failClosed: false
  })
).pipe(Layer.provideMerge(ConfigTest))

const ApiTest = HttpApiBuilder.api(Api).pipe(Layer.provide(HandlersLive), Layer.provide(PortsTest))

/** The same app `main.ts` serves, minus the socket. */
const web = HttpApiBuilder.toWebHandler(
  Layer.mergeAll(ApiTest, HttpServer.layerContext)
)
const handler = web.handler

afterAll(() => web.dispose())

const get = (path: string, headers: Record<string, string> = {}) =>
  handler(new Request(`http://test${path}`, { headers }))

const send = (
  method: string,
  path: string,
  body: unknown,
  headers: Record<string, string> = {}
) =>
  handler(
    new Request(`http://test${path}`, {
      method,
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body)
    })
  )

const auth = { Authorization: `Bearer ${ADMIN_KEY}` }

// ---------------------------------------------------------------------------

describe("meta", () => {
  it("GET /health", async () => {
    const res = await get("/health")
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ status: "ok", version: "test-1.0.0" })
  })

  it("GET / advertises the runtime so the two ports are distinguishable", async () => {
    const body = (await (await get("/")).json()) as { runtime: string }
    expect(body.runtime).toBe("effect-ts")
  })

  it("GET /ready reports each component", async () => {
    const body = (await (await get("/ready")).json()) as {
      ready: boolean
      components: Array<{ name: string; ready: boolean }>
    }
    expect(body.ready).toBe(true)
    expect(body.components.map((c) => c.name)).toEqual(["kg", "feedback-store", "wiki"])
  })
})

describe("curated KG surfaces", () => {
  it("keeps stats.domains == len(/api/domains) in snapshot mode", async () => {
    const stats = (await (await get("/api/stats")).json()) as { domains: number; skills: number }
    const domains = (await (await get("/api/domains")).json()) as Array<unknown>
    const skills = (await (await get("/api/skills")).json()) as Array<unknown>
    expect(stats.domains).toBe(domains.length)
    expect(stats.skills).toBe(skills.length)
  })

  it("keeps research summary domains consistent with /api/domains", async () => {
    const summary = (await (await get("/api/research/summary")).json()) as {
      domains: number
      source: string
    }
    const domains = (await (await get("/api/domains")).json()) as Array<unknown>
    expect(summary.domains).toBe(domains.length)
    expect(summary.source).toBe("snapshot")
  })
})

describe("research agent feed", () => {
  it("labels snapshot magnitudes so an agent cannot cite them as live", async () => {
    const feed = (await (await get("/api/research/agent")).json()) as {
      generated_hint: string
      summary: { source: string }
      doctrine_url: string
    }
    expect(feed.summary.source).toBe("snapshot")
    expect(feed.generated_hint).toContain("do not cite as current")
    expect(feed.doctrine_url).toContain("llms.txt")
  })
})

describe("feedback", () => {
  it("accepts a valid submission", async () => {
    const res = await send("POST", "/api/feedback", { subject: "hello", body: "world" })
    expect(res.status).toBe(200)
    const body = (await res.json()) as { ok: boolean; id: string; status: string }
    expect(body.ok).toBe(true)
    expect(body.id).toMatch(/^test-\d{4}$/) // deterministic Ids layer
  })

  it("rejects an email without consent with 400", async () => {
    const res = await send("POST", "/api/feedback", {
      subject: "a",
      body: "b",
      email: "me@example.com"
    })
    expect(res.status).toBe(400)
  })

  it("swallows a honeypot hit and returns a null id", async () => {
    const res = await send("POST", "/api/feedback", {
      subject: "a",
      body: "b",
      honeypot: "i am a bot"
    })
    expect(res.status).toBe(200)
    expect((await res.json()) as { id: null }).toMatchObject({ id: null })
  })

  it("rate-limits identical submissions with 429 and a retry hint", async () => {
    const payload = { subject: "flood", body: "flood" }
    const codes: Array<number> = []
    for (let i = 0; i < 5; i += 1) {
      codes.push((await send("POST", "/api/feedback", payload)).status)
    }
    expect(codes.slice(0, 3)).toEqual([200, 200, 200])
    expect(codes[3]).toBe(429)

    const res = await send("POST", "/api/feedback", payload)
    const body = (await res.json()) as { _tag: string; retryAfterSeconds: number }
    expect(body._tag).toBe("RateLimited")
    expect(body.retryAfterSeconds).toBeGreaterThan(0)
  })
})

describe("operator inbox", () => {
  it("401s without a credential", async () => {
    expect((await get("/internal/feedback")).status).toBe(401)
  })

  it("401s on a wrong credential", async () => {
    const res = await get("/internal/feedback", { Authorization: "Bearer nope" })
    expect(res.status).toBe(401)
  })

  it("lists submissions newest-first with the correct key", async () => {
    await send("POST", "/api/feedback", { subject: "first", body: "b" })
    await send("POST", "/api/feedback", { subject: "second", body: "b" })

    const res = await get("/internal/feedback", auth)
    expect(res.status).toBe(200)
    const body = (await res.json()) as { items: Array<{ subject: string }>; count: number }
    expect(body.items[0]?.subject).toBe("second")
    expect(body.count).toBeGreaterThanOrEqual(2)
  })

  it("triages a record and 404s an unknown id", async () => {
    await send("POST", "/api/feedback", { subject: "triage-me", body: "b" })
    const inbox = (await (await get("/internal/feedback", auth)).json()) as {
      items: Array<{ id: string }>
    }
    const id = inbox.items[0]!.id

    const ok = await send(
      "PATCH",
      `/internal/feedback/${id}`,
      { status: "reviewed", operator_note: "seen" },
      auth
    )
    expect(ok.status).toBe(200)
    const patched = (await ok.json()) as { item: { status: string; operator_note: string } }
    expect(patched.item.status).toBe("reviewed")
    expect(patched.item.operator_note).toBe("seen")

    const missing = await send(
      "PATCH",
      "/internal/feedback/does-not-exist",
      { status: "spam" },
      auth
    )
    expect(missing.status).toBe(404)
  })
})

describe("KG Cypher proxy", () => {
  // Both keys are empty in this config, so the surface must be OFF rather than
  // open. This is the test that would have caught an "empty key = allow all".
  it("503s — not 200, not 401 — when no key is configured", async () => {
    const res = await send("POST", "/api/kg/read", { query: "RETURN 1" })
    expect(res.status).toBe(503)
    expect((await res.json()) as { _tag: string }).toMatchObject({ _tag: "Unavailable" })
  })

  it("rejects an empty query at the schema boundary", async () => {
    const res = await send("POST", "/api/kg/read", { query: "" })
    expect(res.status).toBe(400)
  })
})
