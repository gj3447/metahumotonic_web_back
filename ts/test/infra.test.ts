/**
 * Tests for the two pieces the 2026-08-10 audit found MISSING rather than wrong:
 * a cache whose settings were parsed but never used, and a client-IP rule that
 * was parsed but never read.
 */
import { Effect, Ref, TestClock, TestContext } from "effect"
import { describe, expect, it } from "vitest"
import * as Cache from "../src/ports/Cache.js"
import { resolve } from "../src/ports/ClientIp.js"

const runTest = <A, E>(e: Effect.Effect<A, E, never>) =>
  Effect.runPromise(Effect.provide(e, TestContext.TestContext) as Effect.Effect<A, E, never>)

describe("TtlCache", () => {
  it("serves the second read from cache", async () => {
    const r = await runTest(
      Effect.gen(function* () {
        const calls = yield* Ref.make(0)
        const cache = yield* Cache.make<number>({ ttlSeconds: 60, maxEntries: 10 })
        const produce = Ref.updateAndGet(calls, (n) => n + 1)
        const a = yield* cache.get("k", produce)
        const b = yield* cache.get("k", produce)
        return { a, b, calls: yield* Ref.get(calls) }
      })
    )
    expect(r).toEqual({ a: 1, b: 1, calls: 1 })
  })

  it("expires on the TTL — instantly, via TestClock", async () => {
    const r = await runTest(
      Effect.gen(function* () {
        const calls = yield* Ref.make(0)
        const cache = yield* Cache.make<number>({ ttlSeconds: 120, maxEntries: 10 })
        const produce = Ref.updateAndGet(calls, (n) => n + 1)
        yield* cache.get("k", produce)
        yield* TestClock.adjust("121 seconds")
        yield* cache.get("k", produce)
        return yield* Ref.get(calls)
      })
    )
    expect(r).toBe(2)
  })

  it("single-flights concurrent misses into ONE upstream call", async () => {
    // Without this a cache makes a thundering herd worse, not better: every
    // miss arrives at once the moment an entry expires.
    const r = await runTest(
      Effect.gen(function* () {
        const calls = yield* Ref.make(0)
        const cache = yield* Cache.make<number>({ ttlSeconds: 60, maxEntries: 10 })
        const slow = Effect.gen(function* () {
          const n = yield* Ref.updateAndGet(calls, (x) => x + 1)
          yield* Effect.yieldNow()
          return n
        })
        const results = yield* Effect.all(
          Array.from({ length: 8 }, () => cache.get("hot", slow)),
          { concurrency: 8 }
        )
        return { calls: yield* Ref.get(calls), distinct: new Set(results).size }
      })
    )
    expect(r.calls).toBe(1)
    expect(r.distinct).toBe(1)
  })

  it("is bounded — attacker-controlled keys cannot grow it without limit", async () => {
    const size = await runTest(
      Effect.gen(function* () {
        const cache = yield* Cache.make<number>({ ttlSeconds: 60, maxEntries: 5 })
        for (let i = 0; i < 50; i += 1) yield* cache.get(`k${i}`, Effect.succeed(i))
        return yield* cache.size
      })
    )
    expect(size).toBeLessThanOrEqual(5)
  })

  it("ttlSeconds 0 disables caching entirely", async () => {
    const calls = await runTest(
      Effect.gen(function* () {
        const c = yield* Ref.make(0)
        const cache = yield* Cache.make<number>({ ttlSeconds: 0, maxEntries: 10 })
        const produce = Ref.updateAndGet(c, (n) => n + 1)
        yield* cache.get("k", produce)
        yield* cache.get("k", produce)
        return yield* Ref.get(c)
      })
    )
    expect(calls).toBe(2)
  })

  it("a failed producer does not poison the key", async () => {
    const r = await runTest(
      Effect.gen(function* () {
        const cache = yield* Cache.make<string>({ ttlSeconds: 60, maxEntries: 10 })
        const failed = yield* Effect.either(cache.get("k", Effect.fail("boom" as const)))
        const after = yield* cache.get("k", Effect.succeed("ok"))
        return { failed: failed._tag, after }
      })
    )
    expect(r.failed).toBe("Left")
    expect(r.after).toBe("ok")
  })
})

describe("client IP resolution", () => {
  const remote = "10.0.0.1"

  it("ignores forwarded headers when trust_proxy is off", () => {
    // The default. A directly-exposed instance must not be IP-spoofable.
    expect(
      resolve({
        trustProxy: false,
        cfConnectingIp: "1.2.3.4",
        xForwardedFor: "5.6.7.8",
        remoteAddress: remote
      })
    ).toBe(remote)
  })

  it("prefers CF-Connecting-IP when trusted", () => {
    expect(
      resolve({
        trustProxy: true,
        cfConnectingIp: "1.2.3.4",
        xForwardedFor: "5.6.7.8",
        remoteAddress: remote
      })
    ).toBe("1.2.3.4")
  })

  it("takes the RIGHTMOST X-Forwarded-For entry, never the leftmost", () => {
    // The leftmost entry is whatever the client sent; taking it is worse than
    // having no limiter because it looks like one.
    expect(
      resolve({
        trustProxy: true,
        cfConnectingIp: undefined,
        xForwardedFor: "evil-spoof, 9.9.9.9, 203.0.113.7",
        remoteAddress: remote
      })
    ).toBe("203.0.113.7")
  })

  it("falls back to the socket address, then to a constant", () => {
    expect(
      resolve({ trustProxy: true, cfConnectingIp: "", xForwardedFor: "", remoteAddress: remote })
    ).toBe(remote)
    expect(
      resolve({
        trustProxy: false,
        cfConnectingIp: undefined,
        xForwardedFor: undefined,
        remoteAddress: undefined
      })
    ).toBe("unknown")
  })
})
