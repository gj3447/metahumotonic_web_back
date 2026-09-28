import { Context, Effect, Layer, Schema } from "effect"
import type { ObservationBatch } from "../domain/ObservationIngest.js"
import type { PlatformObservation } from "../domain/PlatformGraph.js"

/**
 * This allowlist is deliberately code-owned. The operator CLI has no URL,
 * command, catalog-selector, or discovery argument: changing the network
 * scope requires a reviewed code change.
 *
 * These probes only establish an HTTP result for these two public services.
 * They never establish deployment identity, backend readiness, feature
 * completeness, or authority to call the observed service.
 */
export const fixedProbeTargets = [
  { id: "public-website-home", subjectId: "service:website", url: "https://metahumotonic.com/", check: "http" as const, successStatus: 200 },
  { id: "public-soopoolim-home", subjectId: "service:soopoolim", url: "https://soopoolim.metahumotonic.com/", check: "http" as const, successStatus: 200 }
] as const

const RunId = Schema.String.pipe(Schema.pattern(/^[a-z0-9][a-z0-9-]{2,55}$/))
const IsoTime = Schema.String.pipe(Schema.filter((value) => {
  const time = Date.parse(value)
  return Number.isFinite(time) && new Date(time).toISOString() === value
}, { message: () => "expected canonical UTC ISO timestamp" }))
const TtlSeconds = Schema.Number.pipe(Schema.int(), Schema.between(60, 3600))

export const FixedProbeRequest = Schema.Struct({
  runId: RunId,
  observedAt: IsoTime,
  ttlSeconds: TtlSeconds
})
export type FixedProbeRequest = typeof FixedProbeRequest.Type

export interface ProbeFetch {
  readonly fetch: (input: string, init: RequestInit) => Promise<Response>
}
export class ProbeFetchTag extends Context.Tag("FixedProbeFetch")<ProbeFetchTag, ProbeFetch>() {}

export const FixedProbeResult = Schema.Struct({
  target: Schema.String,
  subjectId: Schema.String,
  outcome: Schema.Literal("reachable", "failed"),
  status: Schema.optional(Schema.Number),
  reason: Schema.optional(Schema.Literal("timeout", "network", "unexpected-status"))
})
export type FixedProbeResult = typeof FixedProbeResult.Type

const observationId = (runId: string, target: string) => `observation:fixed-probe:${runId}:${target}`
const receiptId = (runId: string) => `collector:fixed-probe:${runId}`

const expiry = (observedAt: string, ttlSeconds: number) => new Date(Date.parse(observedAt) + ttlSeconds * 1000).toISOString()

const bounded = async <A>(values: ReadonlyArray<A>, concurrency: number, action: (value: A) => Promise<void>): Promise<void> => {
  let next = 0
  const worker = async (): Promise<void> => {
    while (next < values.length) {
      const index = next++
      await action(values[index]!)
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, values.length) }, worker))
}

const probe = async (target: typeof fixedProbeTargets[number], request: FixedProbeRequest, fetcher: ProbeFetch["fetch"]): Promise<{ observation: PlatformObservation; result: FixedProbeResult }> => {
  let result: FixedProbeResult
  try {
    // Only the status is read. Redirects and every response body are ignored.
    const response = await fetcher(target.url, { method: "GET", redirect: "error", signal: AbortSignal.timeout(5_000) })
    result = response.status === target.successStatus
      ? { target: target.id, subjectId: target.subjectId, outcome: "reachable", status: response.status }
      : { target: target.id, subjectId: target.subjectId, outcome: "failed", status: response.status, reason: "unexpected-status" }
  } catch (error) {
    result = { target: target.id, subjectId: target.subjectId, outcome: "failed", reason: error instanceof DOMException && error.name === "TimeoutError" ? "timeout" : "network" }
  }
  const status = result.status === undefined ? "none" : String(result.status)
  const suffix = result.reason ? ` result=${result.reason}` : ""
  return {
    result,
    observation: {
      id: observationId(request.runId, target.id), subjectId: target.subjectId, check: target.check,
      outcome: result.outcome, observedAt: request.observedAt, expiresAt: expiry(request.observedAt, request.ttlSeconds),
      evidence: [{ source: "collector:fixed-probe/v1", authority: "SYSTEM_DERIVED", observedAt: request.observedAt,
        note: `Fixed GET allowlist target=${target.id}; status=${status}${suffix}; response body was not collected.` }]
    }
  }
}

/**
 * A single invocation produces a stable batch. Repeating the same runId and
 * observedAt produces byte-equivalent identities and timestamps, so the
 * platform receipt endpoint can perform its existing idempotent replay.
 */
export const collectFixedProbeBatch = (request: FixedProbeRequest) => Effect.gen(function* () {
  const fetcher = (yield* ProbeFetchTag).fetch
  const collected: Array<{ observation: PlatformObservation; result: FixedProbeResult }> = []
  yield* Effect.tryPromise(() => bounded(fixedProbeTargets, 2, async (target) => { collected.push(await probe(target, request, fetcher)) }))
  collected.sort((a, b) => a.observation.id.localeCompare(b.observation.id))
  return {
    batch: { receiptId: receiptId(request.runId), observations: collected.map((item) => item.observation) } satisfies ObservationBatch,
    results: collected.map((item) => item.result)
  }
})

export const FixedProbeFetchLive = Layer.succeed(ProbeFetchTag, { fetch: globalThis.fetch })
