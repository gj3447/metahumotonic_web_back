import { HttpApiEndpoint, HttpApiGroup } from "@effect/platform"
import { Schema } from "effect"
import { BadRequest, Conflict, Forbidden, NotFound, Unauthorized, Unavailable } from "../domain/Errors.js"
import { IngestReceipt, IngestResult, ObservationBatch, ObservationHistory } from "../domain/ObservationIngest.js"
import { PlatformNode, PlatformEdge, PlatformKind, PlatformLifecycle, PlatformIntegration, ProgramCategory, PlatformId, PlatformObservation } from "../domain/PlatformGraph.js"
import { InventoryItem, InventorySummary } from "../domain/PlatformInventory.js"
import { RealityView } from "../domain/BackendReality.js"

export const PlatformHeaders = Schema.Struct({ "x-api-key": Schema.optional(Schema.String), authorization: Schema.optional(Schema.String) })
const Envelope = { schema: Schema.Literal("metahumotonic/platform-catalog@1"), observedAt: Schema.String, digest: Schema.String,
  definitionDigest: Schema.String, source: Schema.Literal("snapshot", "postgres") }
const NodeId = Schema.String.pipe(Schema.maxLength(160))
export const PlatformGroup = HttpApiGroup.make("platform")
  .add(HttpApiEndpoint.post("appendObservations", "/observations").setHeaders(PlatformHeaders).setPayload(ObservationBatch).addSuccess(IngestResult))
  .add(HttpApiEndpoint.get("observationHistory", "/observations").setHeaders(PlatformHeaders).setUrlParams(Schema.Struct({
    subjectId: Schema.optional(PlatformId), limit: Schema.optional(Schema.NumberFromString.pipe(Schema.int(), Schema.between(1,100))),
    before: Schema.optional(Schema.NumberFromString.pipe(Schema.int(), Schema.between(1,Number.MAX_SAFE_INTEGER)))
  })).addSuccess(ObservationHistory))
  .add(HttpApiEndpoint.get("ingestReceipt", "/receipts/:id").setHeaders(PlatformHeaders).setPath(Schema.Struct({ id: PlatformId })).addSuccess(IngestReceipt))
  .add(HttpApiEndpoint.get("inventory", "/inventory").setHeaders(PlatformHeaders).setUrlParams(Schema.Struct({
    q: Schema.optional(Schema.String.pipe(Schema.maxLength(200))), kind: Schema.optional(PlatformKind),
    category: Schema.optional(ProgramCategory), lifecycle: Schema.optional(PlatformLifecycle), integration: Schema.optional(PlatformIntegration), owner: Schema.optional(PlatformId),
    limit: Schema.optional(Schema.NumberFromString.pipe(Schema.int(), Schema.between(1,100))),
    offset: Schema.optional(Schema.NumberFromString.pipe(Schema.int(), Schema.between(0,2000)))
  })).addSuccess(Schema.Struct({ ...Envelope, evaluatedAt: Schema.String, items: Schema.Array(InventoryItem),
    total: Schema.Number, offset: Schema.Number, limit: Schema.Number, nextOffset: Schema.NullOr(Schema.Number) })))
  .add(HttpApiEndpoint.get("inventoryItem", "/inventory/:id").setHeaders(PlatformHeaders).setPath(Schema.Struct({ id: PlatformId }))
    .addSuccess(Schema.Struct({ ...Envelope, evaluatedAt: Schema.String, ...InventoryItem.fields })))
  .add(HttpApiEndpoint.get("summary", "/summary").setHeaders(PlatformHeaders).addSuccess(Schema.Struct({ ...Envelope, evaluatedAt: Schema.String, ...InventorySummary.fields })))
  .add(HttpApiEndpoint.get("programs", "/programs").setHeaders(PlatformHeaders)
    .addSuccess(Schema.Struct({ ...Envelope, programs: Schema.Array(PlatformNode) })))
  .add(HttpApiEndpoint.get("graph", "/graph").setHeaders(PlatformHeaders)
    .addSuccess(Schema.Struct({ ...Envelope, nodes: Schema.Array(PlatformNode), edges: Schema.Array(PlatformEdge), observations: Schema.Array(PlatformObservation) })))
  .add(HttpApiEndpoint.get("graphExport", "/graph/export").setHeaders(PlatformHeaders).addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("graphJsonLd", "/graph/jsonld").setHeaders(PlatformHeaders).addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("reality", "/reality").setHeaders(PlatformHeaders).addSuccess(RealityView))
  .add(HttpApiEndpoint.get("realityExport", "/reality/export").setHeaders(PlatformHeaders).addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("realityJsonLd", "/reality/jsonld").setHeaders(PlatformHeaders).addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("neighbors", "/neighbors").setHeaders(PlatformHeaders).setUrlParams(Schema.Struct({
    id: NodeId, limit: Schema.optional(Schema.NumberFromString.pipe(Schema.int(), Schema.between(1, 100))),
    include_proposed: Schema.optional(Schema.Literal("true", "false"))
  })).addSuccess(Schema.Struct({ ...Envelope, nodes: Schema.Array(PlatformNode), edges: Schema.Array(PlatformEdge), truncated: Schema.Boolean })))
  .add(HttpApiEndpoint.get("services", "/services").setHeaders(PlatformHeaders).addSuccess(Schema.Struct({
    infrastructure: Schema.Array(PlatformNode),
    services: Schema.Array(Schema.Struct({ id: Schema.String, nodeId: Schema.String, configured: Schema.Boolean,
      execution: Schema.Literal("external"), tools: Schema.Array(Schema.Struct({ name: Schema.String, access: Schema.Literal("read", "write"), description: Schema.String })) }))
  })))
  .add(HttpApiEndpoint.get("tools", "/mcp/:id/tools").setPath(Schema.Struct({ id: NodeId })).setHeaders(PlatformHeaders).addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.post("call", "/mcp/:id/call").setPath(Schema.Struct({ id: NodeId })).setHeaders(PlatformHeaders)
    .setPayload(Schema.Struct({ tool: Schema.String.pipe(Schema.maxLength(100)), arguments: Schema.Record({ key: Schema.String, value: Schema.Unknown }) })).addSuccess(Schema.Unknown))
  .addError(Unauthorized).addError(Unavailable).addError(NotFound).addError(Forbidden).addError(BadRequest).addError(Conflict)
  .prefix("/api/platform/v1")
