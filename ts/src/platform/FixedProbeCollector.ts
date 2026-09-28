import { Context, Effect, Either, Layer, Schema } from "effect"
import { ObservationBatch } from "../domain/ObservationIngest.js"
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

/** A run produces a new observation of the current fixed targets. */
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

/**
 * The operator persists these exact UTF-8 bytes before approval. A later
 * network result can change a newly collected batch, so replay must use the
 * persisted bytes rather than collecting again.
 */
export const fixedProbeBatchBytes = (batch: typeof ObservationBatch.Type): Uint8Array => new TextEncoder().encode(JSON.stringify(batch))

const fixedBatchProblem = (batch: typeof ObservationBatch.Type): string | null => {
  const match = /^collector:fixed-probe:([a-z0-9][a-z0-9-]{2,55})$/.exec(batch.receiptId)
  if (!match) return "fixed probe receipt identity required"
  const runId = match[1]!
  if (batch.observations.length !== fixedProbeTargets.length) return "fixed probe target count required"
  const observedAt = batch.observations[0]?.observedAt
  if (!observedAt) return "fixed probe observation time required"
  const ttl = Date.parse(batch.observations[0]!.expiresAt) - Date.parse(observedAt)
  if (ttl < 60_000 || ttl > 3_600_000 || ttl % 1000 !== 0) return "fixed probe TTL range required"
  for (const target of fixedProbeTargets) {
    const observation = batch.observations.find((item) => item.id === observationId(runId, target.id))
    if (!observation || observation.subjectId !== target.subjectId || observation.check !== target.check) return `fixed probe target mismatch: ${target.id}`
    if (observation.observedAt !== observedAt || Date.parse(observation.expiresAt) - Date.parse(observation.observedAt) !== ttl) return `fixed probe time mismatch: ${target.id}`
    if (observation.evidence.length !== 1) return `fixed probe evidence count mismatch: ${target.id}`
    const evidence = observation.evidence[0]!
    const note = new RegExp(`^Fixed GET allowlist target=${target.id}; status=(?:none|[0-9]{3})(?: result=(?:timeout|network|unexpected-status))?; response body was not collected\\.$`)
    if (evidence.source !== "collector:fixed-probe/v1" || evidence.authority !== "SYSTEM_DERIVED" || evidence.observedAt !== observedAt || !note.test(evidence.note)) return `fixed probe evidence mismatch: ${target.id}`
  }
  return null
}

export const parseFixedProbeBatchBytes = (bytes: Uint8Array): typeof ObservationBatch.Type => {
  if (bytes.byteLength === 0 || bytes.byteLength > 524_288) throw new Error("invalid fixed probe batch file size")
  let value: unknown
  try { value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) } catch { throw new Error("invalid fixed probe batch JSON") }
  const decoded = Schema.decodeUnknownEither(ObservationBatch, { onExcessProperty: "error" })(value)
  if (Either.isLeft(decoded)) throw new Error("invalid fixed probe batch contract")
  const problem = fixedBatchProblem(decoded.right)
  if (problem) throw new Error(problem)
  return decoded.right
}

/** API keys may travel over HTTPS, or a local loopback test/operator endpoint only. */
export const isSafeFixedProbeIngestOrigin = (value: string): boolean => {
  try {
    const url = new URL(value)
    if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) return false
    if (url.protocol === "https:") return true
    return url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
  } catch { return false }
}

export const FixedProbeFetchLive = Layer.succeed(ProbeFetchTag, { fetch: globalThis.fetch })
