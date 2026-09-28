import { Schema } from "effect"
import { PlatformId, PlatformObservation, ObservationTime, type PlatformCatalog } from "./PlatformGraph.js"

export const ObservationBatch = Schema.Struct({
  receiptId: PlatformId,
  observations: Schema.Array(PlatformObservation).pipe(Schema.minItems(1), Schema.maxItems(100))
})
export type ObservationBatch = typeof ObservationBatch.Type
export const IngestReceipt = Schema.Struct({
  receiptId: PlatformId, digest: Schema.String, definitionDigest: Schema.String,
  receivedAt: ObservationTime, observationCount: Schema.Number, insertedCount: Schema.Number
})
export type IngestReceipt = typeof IngestReceipt.Type
export const IngestResult = Schema.Struct({ ...IngestReceipt.fields, replayed: Schema.Boolean })
export const ObservationHistory = Schema.Struct({
  source: Schema.Literal("postgres"), items: Schema.Array(Schema.Struct({
    sequence: Schema.Number, receiptId: PlatformId, receivedAt: ObservationTime, observation: PlatformObservation
  })), nextBefore: Schema.NullOr(Schema.Number)
})
export type ObservationHistory = typeof ObservationHistory.Type

/** Evidence is reported by the writer; ingest never ratifies its authority. */
export const observationProblems = (catalog: PlatformCatalog, observations: ReadonlyArray<PlatformObservation>, now: number) => {
  const ids = new Set<string>(), subjects = new Set(catalog.nodes.map((n) => n.id)), problems: string[] = []
  for (const observation of observations) {
    if (ids.has(observation.id)) problems.push("duplicate observation identity")
    ids.add(observation.id)
    if (!subjects.has(observation.subjectId)) problems.push("unknown observation subject")
    if (Date.parse(observation.observedAt) > now) problems.push("future observation")
    if (Date.parse(observation.expiresAt) <= Date.parse(observation.observedAt)) problems.push("invalid observation expiry")
  }
  return problems
}
