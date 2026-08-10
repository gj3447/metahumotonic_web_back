/**
 * Pure-kernel tests.
 *
 * Everything exercised here runs with no server, no database and no clock —
 * which is the point of having pulled these rules out of the driver class.
 */
import { Effect, Redacted, TestClock, TestContext } from "effect"
import { describe, expect, it } from "vitest"
import { InvalidConfiguration, validateWikiConfiguration, type AppConfig } from "../src/Config.js"
import {
  ConsensusRecord,
  FeedbackRequest,
  FindingRecord,
  LessonRecord
} from "../src/domain/Contracts.js"
import { Schema } from "effect"
import { authorize, constantTimeEquals, extractKey } from "../src/ports/Auth.js"
import * as Breaker from "../src/ports/Breaker.js"
import { coerceParams, decodeNeighbors, mergeRecent } from "../src/ports/KgPort.js"
import { makeInProcess } from "../src/ports/RateLimiter.js"

const run = <A, E>(e: Effect.Effect<A, E, never>) => Effect.runPromise(e)
const runTest = <A, E>(e: Effect.Effect<A, E, never>) =>
  Effect.runPromise(Effect.provide(e, TestContext.TestContext) as Effect.Effect<A, E, never>)

// ---------------------------------------------------------------------------

describe("FeedbackRequest contract", () => {
  const decode = Schema.decodeUnknownEither(FeedbackRequest)

  it("accepts a minimal submission and applies the documented defaults", () => {
    const r = decode({ subject: "hello", body: "world" })
    expect(r._tag).toBe("Right")
    if (r._tag !== "Right") return
    expect(r.right.type).toBe("general")
    expect(r.right.source_path).toBe("/")
    expect(r.right.contact_consent).toBe(false)
  })

  it("trims single-line fields", () => {
    const r = decode({ subject: "  hi  ", body: "  there  " })
    expect(r._tag === "Right" && r.right.subject).toBe("hi")
  })

  it("rejects CR/LF in the subject (header + log injection)", () => {
    expect(decode({ subject: "a\r\nX-Injected: 1", body: "b" })._tag).toBe("Left")
  })

  it("allows newlines in the multi-line body but not other control chars", () => {
    expect(decode({ subject: "a", body: "line1\nline2\ttabbed" })._tag).toBe("Right")
    expect(decode({ subject: "a", body: "bad\u0007bell" })._tag).toBe("Left")
  })

  it("rejects a whitespace-only subject", () => {
    expect(decode({ subject: "   ", body: "b" })._tag).toBe("Left")
  })

  it("requires consent when an email is supplied", () => {
    expect(decode({ subject: "a", body: "b", email: "me@example.com" })._tag).toBe("Left")
    expect(
      decode({ subject: "a", body: "b", email: "me@example.com", contact_consent: true })._tag
    ).toBe("Right")
  })

  it("rejects a malformed email but allows the empty one", () => {
    expect(decode({ subject: "a", body: "b", email: "nope", contact_consent: true })._tag).toBe(
      "Left"
    )
    expect(decode({ subject: "a", body: "b", email: "" })._tag).toBe("Right")
  })

  it("requires source_path to be a local absolute path", () => {
    expect(decode({ subject: "a", body: "b", source_path: "../etc/passwd" })._tag).toBe("Left")
    expect(decode({ subject: "a", body: "b", source_path: "//evil.example" })._tag).toBe("Left")
    expect(decode({ subject: "a", body: "b", source_path: "/research" })._tag).toBe("Right")
  })
})

// ---------------------------------------------------------------------------

describe("mergeRecent", () => {
  const finding = (name: string, createdAt: string, axis = "A", subAxis = "B") =>
    new FindingRecord({ name, finding: `finding ${name}`, axis, subAxis, createdAt })

  it("orders newest first and sinks blank timestamps to the bottom", () => {
    const merged = mergeRecent({
      findings: [finding("old", "2020-01-01"), finding("blank", "")],
      lessons: [new LessonRecord({ name: "l1", problem: "p", createdAt: "2030-01-01" })],
      consensus: [],
      limit: 10
    })
    expect(merged.map((m) => m.name)).toEqual(["l1", "old", "blank"])
  })

  it("honours the limit", () => {
    const merged = mergeRecent({
      findings: [finding("a", "2026-01-03"), finding("b", "2026-01-02")],
      lessons: [],
      consensus: [new ConsensusRecord({ name: "c", createdAt: "2026-01-01" })],
      limit: 2
    })
    expect(merged).toHaveLength(2)
  })

  it("falls back to 'ResearchFinding' when a finding has no axis labels", () => {
    const merged = mergeRecent({
      findings: [finding("x", "2026-01-01", "", "")],
      lessons: [],
      consensus: [],
      limit: 5
    })
    expect(merged[0]?.title).toBe("ResearchFinding")
  })

  it("renders a lesson as the wrong/truth pair when a truth exists", () => {
    const merged = mergeRecent({
      findings: [],
      lessons: [
        new LessonRecord({
          name: "l",
          problem: "P",
          wrongAssumption: "W",
          truth: "T",
          solution: "S",
          createdAt: "2026-01-01"
        })
      ],
      consensus: [],
      limit: 5
    })
    expect(merged[0]?.summary).toBe("W → T")
  })
})

// ---------------------------------------------------------------------------

describe("decodeNeighbors", () => {
  it("reports not-found for an empty result rather than throwing", () => {
    const n = decodeNeighbors("ghost", [])
    expect(n.found).toBe(false)
    expect(n.degree).toBe(0)
  })

  it("marks truncated when degree exceeds the returned list", () => {
    const n = decodeNeighbors("hub", [
      {
        degree: 42,
        neighbors: [{ direction: "out", type: "HAS_FINDING", name: "f1", labels: ["ResearchFinding"] }]
      }
    ])
    expect(n.found).toBe(true)
    expect(n.degree).toBe(42)
    expect(n.truncated).toBe(true)
    expect(n.neighbors[0]?.type).toBe("HAS_FINDING")
  })

  it("defaults an unknown direction to 'out'", () => {
    const n = decodeNeighbors("hub", [{ degree: 1, neighbors: [{ direction: "sideways", type: "R" }] }])
    expect(n.neighbors[0]?.direction).toBe("out")
  })
})

// ---------------------------------------------------------------------------

describe("coerceParams", () => {
  // Regression: `{"limit": 100}` used to reach Neo4j as 100.0 and be rejected
  // with "LIMIT: '100.0' is not a valid value".
  it("promotes integral numbers so Cypher sees Integer, not Float", () => {
    const out = coerceParams({ limit: 100 }) as { limit: { toNumber: () => number } }
    expect(typeof out.limit).toBe("object")
    expect(out.limit.toNumber()).toBe(100)
  })

  it("leaves genuine floats alone", () => {
    expect(coerceParams({ score: 0.5 })).toEqual({ score: 0.5 })
  })

  it("recurses through arrays and nested objects", () => {
    const out = coerceParams({ page: { sizes: [1, 2.5] } }) as {
      page: { sizes: Array<unknown> }
    }
    expect(typeof out.page.sizes[0]).toBe("object")
    expect(out.page.sizes[1]).toBe(2.5)
  })

  it("passes strings, booleans and null through untouched", () => {
    expect(coerceParams({ a: "x", b: true, c: null })).toEqual({ a: "x", b: true, c: null })
  })
})

// ---------------------------------------------------------------------------

describe("Auth", () => {
  it("accepts both `Bearer <key>` and a bare key", () => {
    expect(extractKey("Bearer abc")).toBe("abc")
    expect(extractKey("bearer abc")).toBe("abc")
    expect(extractKey("abc")).toBe("abc")
    expect(extractKey(undefined)).toBe("")
  })

  it("compares without throwing on a length mismatch", () => {
    expect(constantTimeEquals("short", "muchlongerkey")).toBe(false)
    expect(constantTimeEquals("same", "same")).toBe(true)
  })

  it("reports a surface with no configured key as Unavailable, not Unauthorized", async () => {
    const e = await run(
      Effect.flip(
        authorize({ presented: ["anything"], accepted: [Redacted.make("")], surface: "test" })
      )
    )
    expect(e._tag).toBe("Unavailable")
  })

  it("rejects a wrong key as Unauthorized", async () => {
    const e = await run(
      Effect.flip(
        authorize({ presented: ["wrong"], accepted: [Redacted.make("right")], surface: "test" })
      )
    )
    expect(e._tag).toBe("Unauthorized")
  })

  it("accepts any one of several configured keys (write key works on read)", async () => {
    await expect(
      run(
        authorize({
          presented: ["Bearer writekey"],
          accepted: [Redacted.make(""), Redacted.make("writekey")],
          surface: "test"
        })
      )
    ).resolves.toBeUndefined()
  })
})

// ---------------------------------------------------------------------------

describe("rate limiter", () => {
  it("allows up to the cap, then denies with a retry hint", async () => {
    const program = Effect.gen(function* () {
      const limiter = yield* makeInProcess({ maxEvents: 3, windowSeconds: 60, failClosed: false })
      const a = yield* limiter.check("ip")
      const b = yield* limiter.check("ip")
      const c = yield* limiter.check("ip")
      const d = yield* limiter.check("ip")
      return [a, b, c, d]
    })
    const [a, b, c, d] = await runTest(program)
    expect([a?.allowed, b?.allowed, c?.allowed]).toEqual([true, true, true])
    expect(d?.allowed).toBe(false)
    expect(d?.retryAfterSeconds).toBeGreaterThan(0)
  })

  it("keys are independent", async () => {
    const program = Effect.gen(function* () {
      const limiter = yield* makeInProcess({ maxEvents: 1, windowSeconds: 60, failClosed: false })
      yield* limiter.check("a")
      return yield* limiter.check("b")
    })
    expect((await runTest(program)).allowed).toBe(true)
  })

  it("lets the window expire — with TestClock, instantly", async () => {
    const program = Effect.gen(function* () {
      const limiter = yield* makeInProcess({ maxEvents: 1, windowSeconds: 60, failClosed: false })
      yield* limiter.check("ip")
      const blocked = yield* limiter.check("ip")
      yield* TestClock.adjust("61 seconds")
      const afterWindow = yield* limiter.check("ip")
      return { blocked, afterWindow }
    })
    const r = await runTest(program)
    expect(r.blocked.allowed).toBe(false)
    expect(r.afterWindow.allowed).toBe(true)
  })
})

// ---------------------------------------------------------------------------

describe("breaker", () => {
  it("opens on failure, serves the fallback, and closes after the cooldown", async () => {
    const program = Effect.gen(function* () {
      const breaker = yield* Breaker.make(30_000)
      let calls = 0
      const flaky = Effect.suspend(() => {
        calls += 1
        return calls === 1 ? Effect.fail("boom" as const) : Effect.succeed("live")
      })

      const first = yield* Breaker.guard(breaker, flaky, Effect.succeed("fallback"))
      // still cooling down: the dependency must not be touched at all
      const second = yield* Breaker.guard(breaker, flaky, Effect.succeed("fallback"))
      const callsWhileOpen = calls

      yield* TestClock.adjust("31 seconds")
      const third = yield* Breaker.guard(breaker, flaky, Effect.succeed("fallback"))

      return { first, second, third, callsWhileOpen }
    })

    const r = await runTest(program)
    expect(r.first).toBe("fallback")
    expect(r.second).toBe("fallback")
    expect(r.callsWhileOpen).toBe(1) // proves the second call short-circuited
    expect(r.third).toBe("live")
  })
})

// ---------------------------------------------------------------------------

describe("wiki configuration validation", () => {
  const base: AppConfig = {
    host: "0.0.0.0",
    port: 8000,
    version: "test",
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
    feedbackAdminKey: Redacted.make(""),
    feedbackMaxPerWindow: 5,
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
    metricsEnabled: true,
    logJson: true,
    turnstileSecret: Redacted.make(""),
    turnstileHostname: "metahumotonic.com",
    turnstileAction: "feedback_submit",
    turnstileFailOpen: false,
    corsOrigins: [],
    trustProxy: false
  }

  const fail = (cfg: AppConfig) =>
    run(Effect.flip(validateWikiConfiguration(cfg))) as Promise<InvalidConfiguration>

  it("passes with writes disabled", async () => {
    await expect(run(validateWikiConfiguration(base))).resolves.toBeUndefined()
  })

  it("rejects a session TTL under 300s even with writes disabled", async () => {
    const e = await fail({ ...base, wikiSessionTtlSeconds: 60 })
    expect(e.reason).toContain("at least 300")
  })

  it("requires a database URL once public writes are on", async () => {
    const e = await fail({ ...base, wikiPublicWrites: true })
    expect(e.reason).toContain("MHB_WIKI_DATABASE_URL")
  })

  it("requires 32-byte secrets", async () => {
    const e = await fail({
      ...base,
      wikiPublicWrites: true,
      wikiDatabaseUrl: Redacted.make("postgres://x"),
      wikiSessionSecret: Redacted.make("tooshort")
    })
    expect(e.reason).toContain("at least 32 bytes")
  })

  it("refuses to reuse the session secret as the moderation key", async () => {
    const same = Redacted.make("y".repeat(32))
    const e = await fail({
      ...base,
      wikiPublicWrites: true,
      wikiDatabaseUrl: Redacted.make("postgres://x"),
      wikiSessionSecret: same,
      wikiModerationAdminKey: same
    })
    expect(e.reason).toContain("must be distinct")
  })

  it("requires Redis when public writes are fail-closed", async () => {
    const e = await fail({
      ...base,
      wikiPublicWrites: true,
      wikiDatabaseUrl: Redacted.make("postgres://x"),
      wikiSessionSecret: Redacted.make("s".repeat(32)),
      wikiModerationAdminKey: Redacted.make("m".repeat(32))
    })
    expect(e.reason).toContain("MHB_REDIS_URL")
  })
})
