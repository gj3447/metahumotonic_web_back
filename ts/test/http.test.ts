/**
 * HTTP-level tests, driven through the real `HttpApiBuilder` app.
 *
 * Two things changed after the 2026-08-10 audit:
 *
 *  1. **Per-test isolation.** The previous version built ONE handler at module
 *     scope, so state leaked between `it` blocks — visible as an inbox count
 *     asserted with `>= 2` because the exact number depended on test order.
 *     Every test now gets a fresh composition root.
 *  2. **The assertions follow `app/`, not this port's earlier behaviour.**
 *     Where the two disagreed, the Python is the contract; several tests below
 *     exist specifically to pin a divergence that shipped once.
 */
import { HttpApiBuilder, HttpServer } from "@effect/platform"
import { Layer, Redacted } from "effect"
import { beforeEach, describe, expect, it } from "vitest"
import { Api } from "../src/api/Api.js"
import { AppConfigTag, type AppConfig } from "../src/Config.js"
import { ClientIpFixed } from "../src/ports/ClientIp.js"
import { FeedbackStoreMemory } from "../src/ports/FeedbackStore.js"
import { IdsDeterministic } from "../src/ports/Ids.js"
import { KgPortSnapshot } from "../src/ports/KgPort.js"
import { KgWritePortDryOnly } from "../src/ports/KgWritePort.js"
import { FeedbackLimiter, layerInProcess } from "../src/ports/RateLimiter.js"
import { SchemaGuardOffline } from "../src/ports/SchemaGuard.js"
import { HandlersLive } from "../src/server/Handlers.js"
import { OperatorPlaneNoStore } from "../src/server/Middleware.js"

const ADMIN_KEY = "admin-key-that-is-at-least-32-bytes!!"

const baseConfig: AppConfig = {
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

/** A fresh app per test. Nothing carries over. */
const buildApp = (overrides: Partial<AppConfig> = {}) => {
  const cfg: AppConfig = { ...baseConfig, ...overrides }
  const ports = Layer.mergeAll(
    KgPortSnapshot,
    ClientIpFixed("test-client"),
    KgWritePortDryOnly.pipe(Layer.provideMerge(SchemaGuardOffline)),
    FeedbackStoreMemory,
    IdsDeterministic(),
    layerInProcess(FeedbackLimiter, {
      maxEvents: cfg.feedbackMaxPerWindow,
      windowSeconds: cfg.feedbackWindowSeconds,
      failClosed: false
    })
  ).pipe(Layer.provideMerge(Layer.succeed(AppConfigTag, cfg)))

  const api = HttpApiBuilder.api(Api).pipe(
    Layer.provide(HandlersLive),
    Layer.provide(OperatorPlaneNoStore),
    Layer.provide(ports)
  )
  const web = HttpApiBuilder.toWebHandler(Layer.mergeAll(api, HttpServer.layerContext))
  return web.handler
}

let handler: (req: Request) => Promise<Response>
beforeEach(() => {
  handler = buildApp()
})

const get = (path: string, headers: Record<string, string> = {}) =>
  handler(new Request(`http://test${path}`, { headers }))

const send = (method: string, path: string, body: unknown, headers: Record<string, string> = {}) =>
  handler(
    new Request(`http://test${path}`, {
      method,
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body)
    })
  )

/** The Python service reads `x-api-key` (app/routers/feedback.py:85). */
const key = { "X-API-Key": ADMIN_KEY }

const submit = (subject: string) => send("POST", "/api/feedback", { subject, body: "b" })

// ---------------------------------------------------------------------------

describe("meta", () => {
  it("GET /health matches the contract the live checker asserts", async () => {
    const res = await get("/health")
    expect(res.status).toBe(200)
    // ops/check-web-back-live.sh:289-291 asserts exactly these two keys
    expect(await res.json()).toEqual({ status: "ok", version: "test-1.0.0" })
  })

  it("GET /ready emits every key ops/check-web-back-live.sh:296-302 reads", async () => {
    const res = await get("/ready")
    expect(res.status).toBe(200)
    const body = (await res.json()) as Record<string, unknown>
    expect(Object.keys(body).sort()).toEqual([
      "degraded",
      "kg_live",
      "status",
      "wiki_live",
      "wiki_rate_limit_live",
      "wiki_required",
      "wiki_store_live"
    ])
    expect(body["status"]).toBe("ready")
    expect(body["degraded"]).toBe(false)
  })

  it("GET /ready is 503 with the same body when the wiki plane is required but absent", async () => {
    handler = buildApp({ wikiPublicWrites: true })
    const res = await get("/ready")
    expect(res.status).toBe(503)
    const body = (await res.json()) as Record<string, unknown>
    expect(body["status"]).toBe("not_ready")
    expect(body["wiki_required"]).toBe(true)
    expect(body["wiki_live"]).toBe(false)
    expect(body["degraded"]).toBe(true)
  })

  it("GET / carries the endpoints array the Python payload has", async () => {
    const body = (await (await get("/")).json()) as { endpoints: Array<string>; runtime: string }
    expect(body.endpoints).toEqual([
      "/health",
      "/ready",
      "/api/stats",
      "/api/domains",
      "/api/skills",
      "/api/feedback",
      "/api/wiki/v1"
    ])
    expect(body.runtime).toBe("effect-ts") // additive, tells the two apart
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

describe("pagination", () => {
  // app/routers/research.py:36 is Query(20, ge=1, le=100) -> FastAPI 422.
  // The first version of this port clamped, so ?limit=99999 quietly returned
  // 100 rows: an impossible request got a plausible answer.
  it("rejects an over-range limit with 422 instead of clamping", async () => {
    expect((await get("/api/research/findings?limit=99999")).status).toBe(422)
  })

  it("rejects a zero or negative limit", async () => {
    expect((await get("/api/research/findings?limit=0")).status).toBe(422)
    expect((await get("/api/research/lessons?limit=-1")).status).toBe(422)
  })

  it("rejects an offset past research_max_offset", async () => {
    expect((await get("/api/research/findings?offset=10001")).status).toBe(422)
  })

  it("accepts the boundary values", async () => {
    expect((await get("/api/research/findings?limit=100&offset=10000")).status).toBe(200)
    expect((await get("/api/research/findings?limit=1&offset=0")).status).toBe(200)
  })
})

describe("research agent feed", () => {
  it("labels snapshot magnitudes so an agent cannot cite them as live", async () => {
    const feed = (await (await get("/api/research/agent")).json()) as {
      generated_hint: string
      summary: { source: string }
    }
    expect(feed.summary.source).toBe("snapshot")
    expect(feed.generated_hint).toContain("do not cite as current")
  })
})

describe("feedback", () => {
  it("accepts a valid submission", async () => {
    const res = await submit("hello")
    expect(res.status).toBe(200)
    const body = (await res.json()) as { ok: boolean; id: string }
    expect(body.ok).toBe(true)
    expect(body.id).toMatch(/^test-\d{4}$/)
  })

  it("rejects an email without consent with 422", async () => {
    const res = await send("POST", "/api/feedback", {
      subject: "a",
      body: "b",
      email: "me@example.com"
    })
    expect(res.status).toBe(422)
  })

  it("swallows a honeypot hit and returns a null id", async () => {
    const res = await send("POST", "/api/feedback", { subject: "a", body: "b", honeypot: "bot" })
    expect(res.status).toBe(200)
    expect((await res.json()) as { id: null }).toMatchObject({ id: null })
  })

  // The limiter keys on client IP now, so VARYING THE SUBJECT MUST NOT HELP.
  // That bypass is the defect this test exists to prevent recurring.
  it("rate-limits by client IP, and a varying subject does not evade it", async () => {
    const codes: Array<number> = []
    for (const s of ["one", "two", "three", "four", "five"]) {
      codes.push((await submit(s)).status)
    }
    expect(codes.slice(0, 3)).toEqual([200, 200, 200])
    expect(codes[3]).toBe(429)

    const body = (await (await submit("six")).json()) as { retryAfterSeconds: number }
    expect(body.retryAfterSeconds).toBeGreaterThan(0)
  })
})

describe("operator inbox", () => {
  it("401s without a credential and with a wrong one", async () => {
    expect((await get("/internal/feedback")).status).toBe(401)
    expect((await get("/internal/feedback", { "X-API-Key": "nope" })).status).toBe(401)
  })

  it("accepts X-API-Key, which is what the Python service reads", async () => {
    expect((await get("/internal/feedback", key)).status).toBe(200)
  })

  it("also accepts Authorization: Bearer (additive, not a replacement)", async () => {
    const res = await get("/internal/feedback", { Authorization: `Bearer ${ADMIN_KEY}` })
    expect(res.status).toBe(200)
  })

  it("never lets an intermediary cache the operator plane", async () => {
    const res = await get("/internal/feedback", key)
    expect(res.headers.get("cache-control")).toBe("private, no-store")
  })

  it("lists submissions newest-first with an exact count", async () => {
    await submit("first")
    await submit("second")
    const body = (await (await get("/internal/feedback", key)).json()) as {
      items: Array<{ subject: string }>
      count: number
    }
    // exact, not >=: the app is fresh for this test
    expect(body.count).toBe(2)
    expect(body.items.map((i) => i.subject)).toEqual(["second", "first"])
  })

  it("404s a malformed record id before touching the store", async () => {
    // app/routers/feedback.py:156 requires 32 lowercase hex chars
    const res = await send("PATCH", "/internal/feedback/not-a-real-id", { status: "reviewed" }, key)
    expect(res.status).toBe(404)
  })

  it("409s an unknown but well-formed id — not an existence oracle", async () => {
    const res = await send(
      "PATCH",
      `/internal/feedback/${"a".repeat(32)}`,
      { status: "reviewed" },
      key
    )
    expect(res.status).toBe(409)
  })

  it("404s DELETE of a malformed id", async () => {
    expect((await handler(
      new Request("http://test/internal/feedback/nope", { method: "DELETE", headers: key })
    )).status).toBe(404)
  })
})

describe("KG Cypher proxy", () => {
  it("503s — not 200, not 401 — when no key is configured", async () => {
    const res = await send("POST", "/api/kg/read", { query: "RETURN 1" })
    expect(res.status).toBe(503)
    expect((await res.json()) as { _tag: string }).toMatchObject({ _tag: "Unavailable" })
  })

  it("rejects an empty query at the schema boundary with 422", async () => {
    handler = buildApp({ kgReadKey: Redacted.make("r".repeat(40)) })
    const res = await send("POST", "/api/kg/read", { query: "" }, { "X-API-Key": "r".repeat(40) })
    expect(res.status).toBe(422)
  })

  it("accepts X-API-Key on the proxy, matching app/routers/kg_proxy.py:74", async () => {
    handler = buildApp({ kgReadKey: Redacted.make("r".repeat(40)) })
    const res = await send(
      "POST",
      "/api/kg/read",
      { query: "RETURN 1" },
      { "X-API-Key": "r".repeat(40) }
    )
    // snapshot KgPort refuses to run Cypher -> 503, but auth passed (not 401)
    expect(res.status).toBe(503)
  })

  it("401s a wrong X-API-Key", async () => {
    handler = buildApp({ kgReadKey: Redacted.make("r".repeat(40)) })
    const res = await send("POST", "/api/kg/read", { query: "RETURN 1" }, { "X-API-Key": "wrong" })
    expect(res.status).toBe(401)
  })
})
