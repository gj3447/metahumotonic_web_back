import { createServer } from "node:http"
import { Effect, Either, Layer, Schema } from "effect"
import { describe, expect, it } from "vitest"
import { FixedProbeRequest, ProbeFetchTag, collectFixedProbeBatch, fixedProbeBatchBytes, fixedProbeTargets, isSafeFixedProbeIngestOrigin, parseFixedProbeBatchBytes } from "../src/platform/FixedProbeCollector.js"

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
  it("records success without reading bodies", async () => {
    const calls: RequestInit[] = []
    const first = await run(async (_input, init) => { calls.push(init); return new Response("never-read", { status: 200 }) })
    expect(calls.every((call) => call.method === "GET" && call.redirect === "error" && call.signal instanceof AbortSignal)).toBe(true)
    expect(first.batch.receiptId).toBe("collector:fixed-probe:probe-test-001")
    expect(first.batch.observations).toHaveLength(2)
    expect(first.batch.observations.every((observation) => observation.outcome === "reachable" && observation.expiresAt === "2026-09-28T08:15:00.000Z")).toBe(true)
    expect(JSON.stringify(first.batch)).not.toContain("never-read")
  })
  it("persists exact plan bytes: changed later probe results do not alter a replay file", async () => {
    const planned = await run(async () => new Response("body-not-collected", { status: 200 }))
    const changed = await run(async () => new Response("body-not-collected", { status: 503 }))
    const bytes = fixedProbeBatchBytes(planned.batch)
    expect(Buffer.from(fixedProbeBatchBytes(changed.batch)).equals(Buffer.from(bytes))).toBe(false)
    expect(parseFixedProbeBatchBytes(bytes)).toEqual(planned.batch)

    const received: Buffer[] = []
    const server = createServer(async (request, response) => {
      const chunks: Buffer[] = []
      for await (const chunk of request) chunks.push(Buffer.from(chunk))
      received.push(Buffer.concat(chunks))
      response.setHeader("content-type", "application/json")
      response.end(JSON.stringify({ receiptId: planned.batch.receiptId, insertedCount: 2, replayed: received.length > 1, definitionDigest: "fixture" }))
    })
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve))
    try {
      const address = server.address()
      if (!address || typeof address === "string") throw new Error("fixture listener missing")
      const post = () => fetch(`http://127.0.0.1:${address.port}/api/platform/v1/observations`, { method: "POST", headers: { "content-type": "application/json" }, body: bytes })
      expect((await post()).status).toBe(200)
      expect((await post()).status).toBe(200)
      expect(received).toHaveLength(2)
      expect(received.every((body) => body.equals(Buffer.from(bytes)))).toBe(true)
    } finally { await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())) }
  })
  it("rejects generic or tampered batches before a platform write key can be used", async () => {
    const planned = await run(async () => new Response("body-not-collected", { status: 200 }))
    type MutableBatch = { observations: Array<{ id: string; subjectId: string; evidence: Array<{ source: string }> }> }
    const tamper = (change: (value: MutableBatch) => void) => {
      const value = JSON.parse(Buffer.from(fixedProbeBatchBytes(planned.batch)).toString("utf8")) as MutableBatch
      change(value)
      expect(() => parseFixedProbeBatchBytes(Buffer.from(JSON.stringify(value)))).toThrow()
    }
    tamper((batch) => { batch.observations[0]!.subjectId = "program:hswm" })
    tamper((batch) => { batch.observations[0]!.id = "observation:other:target" })
    tamper((batch) => { batch.observations[0]!.evidence[0]!.source = "collector:other" })
    tamper((batch) => { batch.observations.push({ ...batch.observations[0]!, evidence: [{ ...batch.observations[0]!.evidence[0]! }] }) })
  })
  it("allows HTTPS or loopback HTTP ingest origins only", () => {
    for (const origin of ["https://operator.example/", "http://127.0.0.1:8080/", "http://localhost/"])
      expect(isSafeFixedProbeIngestOrigin(origin)).toBe(true)
    for (const origin of ["http://operator.example/", "http://192.168.0.24/", "https://user:token@operator.example/", "https://operator.example/api"])
      expect(isSafeFixedProbeIngestOrigin(origin)).toBe(false)
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
