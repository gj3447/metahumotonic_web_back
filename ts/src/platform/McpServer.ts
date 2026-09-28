import { Clock, Effect, Runtime } from "effect"
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js"
import { z } from "zod"
import { PlatformConfigTag } from "./Config.js"
import { PlatformInventoryStoreTag } from "../ports/PlatformInventoryStore.js"
import { McpFederationTag } from "../ports/McpFederation.js"
import { LearningHubTag } from "../ports/LearningHub.js"
import { catalogEnvelope, platformAccess } from "../server/PlatformHandlers.js"
import { Unavailable } from "../domain/Errors.js"
import { neighborhood, platformIntegrations, platformLifecycles, propertyGraph } from "../domain/PlatformGraph.js"
import { inventory, inventorySummary, platformJsonLd } from "../domain/PlatformInventory.js"
import { realityJsonLd, realityProblems, realityPropertyGraph, realityView } from "../domain/BackendReality.js"
import { backendReality, backendRealityDigest } from "./BackendRealityFile.js"
import { BodyLimitError, readBounded } from "./BoundedHttp.js"

/** SDK owns MCP framing/negotiation. Every operation calls the same Effect services as HTTP. */
export const handleMcp = (request: Request) => Effect.gen(function* () {
  const cfg = yield* PlatformConfigTag
  const canWrite = yield* platformAccess(cfg, [request.headers.get("x-api-key") ?? undefined, request.headers.get("authorization") ?? undefined])
  const federation = yield* McpFederationTag
  const store = yield* PlatformInventoryStoreTag
  const learning = yield* LearningHubTag
  const runtime = yield* Effect.runtime<never>()
  return yield* Effect.tryPromise({ try: async (signal) => {
    const origin = request.headers.get("origin")
    // This machine endpoint uses bearer keys. Browser use goes through the CORS-controlled REST API.
    if (origin !== null) return Response.json({ reason: "browser origin not allowed on MCP endpoint" }, { status: 403 })
    if (request.method !== "POST") return new Response(null, { status: 405, headers: { Allow: "POST" } })
    let parsed: unknown
    try { parsed = JSON.parse(Buffer.from(await readBounded(request.body, cfg.maxBodyBytes)).toString("utf8")) }
    catch (error) { return Response.json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Invalid or oversized JSON body" } }, { status: error instanceof BodyLimitError ? 413 : 400 }) }
    const server = new McpServer({ name: "metahumotonic-platform", version: "1.0.0" })
    const result = (value: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(value) }] })
    const readOnly = { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
    const run = <A, E>(effect: Effect.Effect<A, E>, abortSignal: AbortSignal) => Runtime.runPromise(runtime)(effect.pipe(
      Effect.map(result), Effect.catchAll(() => Effect.succeed({ ...result({ reason: "MCP operation refused or unavailable" }), isError: true }))
    ), { signal: AbortSignal.any([signal, abortSignal, request.signal]) })
    server.registerTool("platform_inventory", { description: "Search programs, owners, infrastructure and deployments. Observations carry freshness; no live probe or dispatch is performed.",
      inputSchema: { q: z.string().max(200).optional(), kind: z.enum(["program", "repository", "service", "mcp-server", "datastore", "host", "deployment"]).optional(),
        category: z.enum(["product", "research", "tooling", "operations", "knowledge", "portfolio"]).optional(),
        lifecycle: z.enum(platformLifecycles).optional(), integration: z.enum(platformIntegrations).optional(),
        owner: z.string().regex(/^[a-z0-9][a-z0-9:._/-]{0,159}$/).optional(),
        limit: z.number().int().min(1).max(100).default(25), offset: z.number().int().min(0).max(2000).default(0) }, annotations: readOnly
    }, (query, extra) => run(Effect.gen(function* () {
      const view = yield* store.read, time = yield* Clock.currentTimeMillis
      return { ...catalogEnvelope(view), evaluatedAt: new Date(time).toISOString(), ...inventory(view.catalog, query, time) }
    }), extra.signal))
    server.registerTool("platform_summary", { description: "Inventory coverage gaps, category counts and observation freshness. Storage availability is distinct from observed service health.",
      inputSchema: {}, annotations: readOnly
    }, (_, extra) => run(Effect.gen(function* () {
      const view = yield* store.read, time = yield* Clock.currentTimeMillis
      return { ...catalogEnvelope(view), evaluatedAt: new Date(time).toISOString(), ...inventorySummary(view.catalog, time) }
    }), extra.signal))
    server.registerTool("platform_export", { description: "Internal graph with original identities, relationship status and evidence. JSON-LD/PROV-O or native USL adapter input; no resources resolved.",
      inputSchema: { format: z.enum(["jsonld", "usl"]) }, annotations: readOnly
    }, ({ format }, extra) => run(store.read.pipe(Effect.map((view) => format === "jsonld" ? platformJsonLd(view.catalog, view.source) : propertyGraph(view.catalog))), extra.signal))
    server.registerTool("platform_reality", { description: "Backend implementation, production evidence, simulation and retirement assessment. Dated proof expires; no probe or dispatch is performed.",
      inputSchema: { format: z.enum(["catalog", "jsonld", "usl"]).default("catalog") }, annotations: readOnly
    }, ({ format }, extra) => run(Effect.gen(function* () {
      if (realityProblems(backendReality, cfg.catalog).length) return yield* Effect.fail(new Unavailable({ reason: "backend_reality_catalog_mismatch" }))
      const now = yield* Clock.currentTimeMillis
      return format === "jsonld" ? realityJsonLd(backendReality, now)
        : format === "usl" ? realityPropertyGraph(backendReality, now)
        : { ...realityView(backendReality, now), digest: backendRealityDigest }
    }), extra.signal))
    server.registerTool("platform_observations", { description: "Read append-only observation history from PostgreSQL. Cursor paging preserves history across concurrent ingestion.",
      inputSchema: { subjectId: z.string().regex(/^[a-z0-9][a-z0-9:._/-]{0,159}$/).optional(),
        before: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER).optional(), limit: z.number().int().min(1).max(100).default(25) }, annotations: readOnly
    }, (query, extra) => run(store.history(query), extra.signal))
    server.registerTool("platform_learning", { description: "Public philosophy, learning paths, GitHub and YouTube links. USL projection preserves meanings and provenance without resolving resources.",
      inputSchema: { format: z.enum(["catalog", "jsonld", "usl"]).default("catalog") }, annotations: readOnly
    }, ({ format }) => {
      const { jsonld, usl, ...catalog } = learning
      return result(format === "jsonld" ? jsonld : format === "usl" ? usl : catalog)
    })
    server.registerTool("platform_programs", { description: "Evidence-backed company programs with explicit snapshot or PostgreSQL source. Registration does not establish runtime health.", inputSchema: {}, annotations: readOnly }, (_, extra) =>
      run(store.read.pipe(Effect.map((view) => ({ ...catalogEnvelope(view), programs: view.catalog.nodes.filter((node) => node.kind === "program") }))), extra.signal))
    server.registerTool("platform_graph", { description: "Bounded graph neighborhood with relationship provenance; edges do not grant execution authority.", inputSchema: {
      id: z.string().max(160), limit: z.number().int().min(1).max(100).default(25), include_proposed: z.boolean().default(false)
    }, annotations: readOnly }, ({ id, limit, include_proposed }, extra) => run(store.read.pipe(Effect.flatMap((view) =>
      view.catalog.nodes.some((node) => node.id === id)
        ? Effect.succeed({ ...catalogEnvelope(view), ...neighborhood(view.catalog, id, limit, include_proposed) })
        : Effect.fail(new Unavailable({ reason: "graph node not found" })))), extra.signal))
    server.registerTool("platform_services", { description: "Configured service bindings. Configuration is distinct from observed runtime health.", inputSchema: {}, annotations: readOnly }, () =>
      result({ services: cfg.bindings.map((binding) => ({ id: binding.id, nodeId: binding.nodeId, configured: binding.url !== null,
        tools: binding.tools.filter((tool) => canWrite || tool.access === "read") })) }))
    server.registerTool("platform_tools", { description: "Discover only startup-allowlisted tools on one configured MCP server.", inputSchema: { server: z.string().max(64) },
      annotations: { ...readOnly, openWorldHint: true } }, ({ server: id }, extra) => run(federation.tools(id, canWrite), extra.signal))
    server.registerTool("platform_call", { description: "Call one allowlisted MCP tool. Write tools require the independent platform write credential; effects are never retried here.",
      inputSchema: { server: z.string().max(64), tool: z.string().max(100), arguments: z.record(z.unknown()).default({}) },
      annotations: { readOnlyHint: !canWrite, destructiveHint: canWrite, openWorldHint: true }
    }, ({ server: id, tool, arguments: args }, extra) => Runtime.runPromise(runtime)(
      federation.call(id, tool, args, canWrite).pipe(Effect.catchAll(() => Effect.succeed({
        ...result({ reason: "MCP operation refused or unavailable" }), isError: true
      }))), { signal: AbortSignal.any([signal, extra.signal, request.signal]) }
    ))
    const transport = new WebStandardStreamableHTTPServerTransport({ enableJsonResponse: true })
    try {
      await server.connect(transport)
      const response = await transport.handleRequest(request, { parsedBody: parsed })
      const bytes = await readBounded(response.body, 4_194_304)
      return new Response([202, 204, 205, 304].includes(response.status) && bytes.length === 0 ? null : Buffer.from(bytes),
        { status: response.status, headers: response.headers })
    } finally { await server.close() }
  }, catch: () => new Unavailable({ reason: "MCP transport failed" }) })
})
