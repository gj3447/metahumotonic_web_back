import { Effect, Either, Layer, Schema } from "effect"
import { describe, expect, it } from "vitest"
import { FixedProbeRequest, ProbeFetchTag, collectFixedProbeBatch, fixedProbeTargets } from "../src/platform/FixedProbeCollector.js"

const request = { runId: "probe-test-001", observedAt: "2026-09-28T08:00:00.000Z", ttlSeconds: 900 }
const run = (fetch: (input: string, init: RequestInit) => Promise<Response>) => Effect.runPromise(
  collectFixedProbeBatch(request).pipe(Effect.provide(Layer.succeed(ProbeFetchTag, { fetch })))
)

describe("fixed operator probe collector", () => {
  it("has a closed, public GET-only allowlist", () => {
    expect(fixedProbeTargets).toEqual([
      expect.objectContaining({ id: "public-website-home", subjectId: "service:website", url: "https://metahumotonic.com/", check: "http" }),
      expect.objectContaining({ id: "public-soopoolim-home", subjectId: "service:soopoolim", url: "https://soopoolim.metahumotonic.com/", check: "http" })
    ])
  })
  it("records success without reading bodies and makes IDs and receipt stable for replay", async () => {
    const calls: RequestInit[] = []
    const first = await run(async (_input, init) => { calls.push(init); return new Response("never-read", { status: 200 }) })
    const replay = await run(async () => new Response("never-read", { status: 200 }))
    expect(calls.every((call) => call.method === "GET" && call.redirect === "error" && call.signal instanceof AbortSignal)).toBe(true)
    expect(first.batch).toEqual(replay.batch)
    expect(first.batch.receiptId).toBe("collector:fixed-probe:probe-test-001")
    expect(first.batch.observations).toHaveLength(2)
    expect(first.batch.observations.every((observation) => observation.outcome === "reachable" && observation.expiresAt === "2026-09-28T08:15:00.000Z")).toBe(true)
    expect(JSON.stringify(first.batch)).not.toContain("never-read")
  })
  it("records status, timeout, and network failures without error bodies", async () => {
    const status = await run(async () => new Response("unexpected-body", { status: 503 }))
    expect(status.results.every((result) => result.outcome === "failed" && result.status === 503 && result.reason === "unexpected-status")).toBe(true)
    let calls = 0
    const transport = await run(async () => {
      calls++
      if (calls === 1) throw new DOMException("not retained", "TimeoutError")
      throw new Error("private endpoint details must not escape")
    })
    expect(transport.results).toEqual(expect.arrayContaining([
      expect.objectContaining({ outcome: "failed", reason: "timeout" }),
      expect.objectContaining({ outcome: "failed", reason: "network" })
    ]))
    for (const observation of [...status.batch.observations, ...transport.batch.observations]) {
      expect(observation.outcome).toBe("failed")
      expect(JSON.stringify(observation)).not.toContain("unexpected-body")
      expect(JSON.stringify(observation)).not.toContain("private endpoint")
    }
  })
  it("requires a bounded canonical request identity", () => {
    expect(Either.isLeft(Schema.decodeUnknownEither(FixedProbeRequest)({ ...request, runId: "bad/id" }))).toBe(true)
    expect(Either.isLeft(Schema.decodeUnknownEither(FixedProbeRequest)({ ...request, ttlSeconds: 59 }))).toBe(true)
  })
})
