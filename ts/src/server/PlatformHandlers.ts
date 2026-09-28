import { HttpApiBuilder, HttpServerResponse } from "@effect/platform"
import { Clock, Effect, Redacted } from "effect"
import { Api } from "../api/Api.js"
import { Forbidden, NotFound, Unavailable } from "../domain/Errors.js"
import { realityJsonLd, realityProblems, realityPropertyGraph, realityView } from "../domain/BackendReality.js"
import { neighborhood, propertyGraph } from "../domain/PlatformGraph.js"
import { inventory, inventoryItem, inventorySummary, platformJsonLd } from "../domain/PlatformInventory.js"
import { PlatformConfigTag, type PlatformConfig } from "../platform/Config.js"
import { backendReality, backendRealityDigest } from "../platform/BackendRealityFile.js"
import { authorize, constantTimeEquals, extractKey } from "../ports/Auth.js"
import { PlatformInventoryStoreTag, type CatalogView } from "../ports/PlatformInventoryStore.js"
import { McpFederationTag } from "../ports/McpFederation.js"

export const platformAccess = (cfg: PlatformConfig, presented: ReadonlyArray<string | undefined>) =>
  authorize({ presented, accepted: [cfg.readKey, cfg.writeKey], surface: "company platform" }).pipe(
    Effect.as(Redacted.value(cfg.writeKey).length > 0 && presented.some((value) => constantTimeEquals(extractKey(value), Redacted.value(cfg.writeKey))))
  )
export const catalogEnvelope = (view: CatalogView) => ({ schema: view.catalog.schema, source: view.source, observedAt: view.catalog.observedAt, digest: view.digest, definitionDigest: view.definitionDigest })
export const PlatformHandlers = HttpApiBuilder.group(Api, "platform", (handlers) => {
  const access = (headers: { readonly "x-api-key"?: string | undefined; readonly authorization?: string | undefined }) => Effect.gen(function* () {
    const cfg = yield* PlatformConfigTag
    const canWrite = yield* platformAccess(cfg, [headers["x-api-key"], headers.authorization])
    return { cfg, canWrite }
  })
  const read = (headers: Parameters<typeof access>[0]) => access(headers).pipe(Effect.zipRight(
    Effect.flatMap(PlatformInventoryStoreTag, (store) => store.read)))
  const readReality = (headers: Parameters<typeof access>[0]) => Effect.gen(function* () {
    const { cfg } = yield* access(headers)
    if (realityProblems(backendReality, cfg.catalog).length) return yield* Effect.fail(new Unavailable({ reason: "backend_reality_catalog_mismatch" }))
    return backendReality
  })
  return handlers
    .handle("appendObservations", ({ headers, payload }) => Effect.gen(function* () {
      const { canWrite } = yield* access(headers)
      if (!canWrite) return yield* Effect.fail(new Forbidden({ reason: "platform write credential required" }))
      return yield* (yield* PlatformInventoryStoreTag).append(payload, yield* Clock.currentTimeMillis)
    }))
    .handle("observationHistory", ({ headers, urlParams }) => access(headers).pipe(Effect.zipRight(
      Effect.flatMap(PlatformInventoryStoreTag, (store) => store.history(urlParams)))))
    .handle("ingestReceipt", ({ headers, path }) => access(headers).pipe(Effect.zipRight(
      Effect.flatMap(PlatformInventoryStoreTag, (store) => store.receipt(path.id)))))
    .handle("inventory", ({ headers, urlParams }) => Effect.gen(function* () {
      const view = yield* read(headers)
      const now = yield* Clock.currentTimeMillis
      return { ...catalogEnvelope(view), evaluatedAt: new Date(now).toISOString(), ...inventory(view.catalog, urlParams, now) }
    }))
    .handle("inventoryItem", ({ headers, path }) => Effect.gen(function* () {
      const view = yield* read(headers)
      const node = view.catalog.nodes.find((item) => item.id === path.id)
      if (!node) return yield* Effect.fail(new NotFound({ reason: "inventory item not found" }))
      const now = yield* Clock.currentTimeMillis
      return { ...catalogEnvelope(view), evaluatedAt: new Date(now).toISOString(), ...inventoryItem(view.catalog, node, now) }
    }))
    .handle("summary", ({ headers }) => Effect.gen(function* () {
      const view = yield* read(headers)
      const now = yield* Clock.currentTimeMillis
      return { ...catalogEnvelope(view), evaluatedAt: new Date(now).toISOString(), ...inventorySummary(view.catalog, now) }
    }))
    .handle("programs", ({ headers }) => read(headers).pipe(Effect.map((view) => ({ ...catalogEnvelope(view), programs: view.catalog.nodes.filter((node) => node.kind === "program") }))))
    .handle("graph", ({ headers }) => read(headers).pipe(Effect.map((view) => ({ ...catalogEnvelope(view), nodes: view.catalog.nodes, edges: view.catalog.edges, observations: view.catalog.observations ?? [] }))))
    .handle("graphExport", ({ headers }) => read(headers).pipe(Effect.map((view) => propertyGraph(view.catalog))))
    .handle("graphJsonLd", ({ headers }) => read(headers).pipe(Effect.map((view) =>
      HttpServerResponse.text(JSON.stringify(platformJsonLd(view.catalog, view.source)), { contentType: "application/ld+json" }))))
    .handle("reality", ({ headers }) => Effect.gen(function* () {
      const graph = yield* readReality(headers)
      return { ...realityView(graph, yield* Clock.currentTimeMillis), digest: backendRealityDigest }
    }))
    .handle("realityExport", ({ headers }) => Effect.gen(function* () {
      const graph = yield* readReality(headers)
      return realityPropertyGraph(graph, yield* Clock.currentTimeMillis)
    }))
    .handle("realityJsonLd", ({ headers }) => Effect.gen(function* () {
      const graph = yield* readReality(headers)
      return HttpServerResponse.text(JSON.stringify(realityJsonLd(graph, yield* Clock.currentTimeMillis)), { contentType: "application/ld+json" })
    }))
    .handle("neighbors", ({ headers, urlParams }) => Effect.gen(function* () {
      const view = yield* read(headers)
      if (!view.catalog.nodes.some((node) => node.id === urlParams.id)) return yield* Effect.fail(new NotFound({ reason: "graph node not found" }))
      return { ...catalogEnvelope(view), ...neighborhood(view.catalog, urlParams.id, urlParams.limit ?? 25, urlParams.include_proposed === "true") }
    }))
    .handle("services", ({ headers }) => Effect.gen(function* () {
      const { cfg, canWrite } = yield* access(headers)
      const view = yield* (yield* PlatformInventoryStoreTag).read
      return { infrastructure: view.catalog.nodes.filter((node) => ["service", "datastore", "host", "deployment", "mcp-server"].includes(node.kind)),
        services: cfg.bindings.map((binding) => ({
          id: binding.id, nodeId: binding.nodeId, configured: binding.url !== null, execution: "external" as const,
          tools: binding.tools.filter((tool) => canWrite || tool.access === "read")
        })) }
    }))
    .handle("tools", ({ headers, path }) => Effect.gen(function* () {
      const { canWrite } = yield* access(headers)
      const federation = yield* McpFederationTag
      return { tools: yield* federation.tools(path.id, canWrite) }
    }))
    .handle("call", ({ headers, path, payload }) => Effect.gen(function* () {
      const { canWrite } = yield* access(headers)
      const federation = yield* McpFederationTag
      return yield* federation.call(path.id, payload.tool, payload.arguments, canWrite)
    }))
})
