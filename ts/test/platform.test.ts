import { HttpApiBuilder } from "@effect/platform"
import { ConfigProvider, Effect, Either, Layer, Redacted } from "effect"
import { createServer, type Server } from "node:http"
import { readFileSync } from "node:fs"
import { afterEach, describe, expect, it } from "vitest"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js"
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js"
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js"
import { z } from "zod"
import { configFromEnv } from "../src/Config.js"
import { Unavailable } from "../src/domain/Errors.js"
import { decodeCatalog, graphProblems, neighborhood, propertyGraph } from "../src/domain/PlatformGraph.js"
import { PlatformConfigTag, type PlatformConfig } from "../src/platform/Config.js"
import { McpRegistryTag, enrichServer, manifest, registryStatus, registryStatusText, type McpRegistry } from "../src/ports/McpRegistry.js"
import { PlatformInventoryStoreTag, type PlatformInventoryStore } from "../src/ports/PlatformInventoryStore.js"
import { configOf, webHandlerLayer } from "../src/server/Composition.js"

const catalog = Either.getOrThrow(decodeCatalog(JSON.parse(readFileSync(new URL("../config/platform-catalog.json", import.meta.url), "utf8"))))
const READ = "read-credential-32-bytes-for-tests-only"
const WRITE = "write-credential-32-bytes-for-tests-only"
const clean = Effect.withConfigProvider(ConfigProvider.fromMap(new Map()))
const config = Effect.runSync(configFromEnv.pipe(clean))
const platform: PlatformConfig = {
  readKey: Redacted.make(READ), writeKey: Redacted.make(WRITE), legacyOrigin: null, legacyRequired: false, ontologyRequired: false, shadowReadOnly: false,
  catalog, catalogDigest: "test-digest", bindings: [], maxBodyBytes: 524288, timeoutMs: 1000
}
const disposers: Array<() => Promise<unknown>> = []
afterEach(async () => { for (const dispose of disposers.splice(0).reverse()) await dispose() })
const app = (overrides: Partial<PlatformConfig> = {}, registry?: McpRegistry, inventoryStore?: PlatformInventoryStore) => {
  const web = HttpApiBuilder.toWebHandler(webHandlerLayer({
    config: configOf(config), platformConfig: Layer.succeed(PlatformConfigTag, { ...platform, ...overrides }),
    ...(registry ? { registry: Layer.succeed(McpRegistryTag, registry) } : {}),
    ...(inventoryStore ? { inventoryStore: Layer.succeed(PlatformInventoryStoreTag, inventoryStore) } : {})
  }))
  disposers.push(web.dispose)
  return {
    handler: web.handler,
    request: (path: string, key: string | null = READ, init: RequestInit = {}) => web.handler(new Request(`http://localhost${path}`, {
      ...init, headers: { ...(key === null ? {} : { "x-api-key": key }), ...init.headers }
    }))
  }
}
const listen = async (server: Server) => {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve))
  disposers.push(() => new Promise<void>((resolve, reject) => { server.closeAllConnections(); server.close((error) => error ? reject(error) : resolve()) }))
  const address = server.address()
  if (!address || typeof address === "string") throw new Error("test server did not bind")
  return `http://127.0.0.1:${address.port}`
}
const connect = async (handler: (request: Request) => Promise<Response>, key = READ) => {
  const client = new Client({ name: "platform-contract-test", version: "1.0" })
  const transport = new StreamableHTTPClientTransport(new URL("http://localhost/mcp"), {
    fetch: (input, init) => handler(new Request(input, init)), requestInit: { headers: { Authorization: `Bearer ${key}` } }
  })
  await client.connect(transport as Transport)
  disposers.push(() => client.close())
  return client
}

describe("private read canary guard", () => {
  it("rejects every mutation before authentication or downstream routing", async () => {
    const web = app({ shadowReadOnly: true })
    const response = await web.request("/api/platform/v1/observations", null, { method: "POST", body: "{}" })
    expect(response.status).toBe(405)
    expect(response.headers.get("allow")).toBe("GET, HEAD, OPTIONS")
    await expect(response.json()).resolves.toEqual({ reason: "shadow_read_only" })
  })
})

describe("evidence-backed program graph", () => {
  it("serves authenticated inventory, detail, summary and JSON-LD with bounded query contracts", async () => {
    const web = app()
    for (const path of ["/inventory", "/inventory/program:hswm", "/summary", "/graph/jsonld"]) {
      expect((await web.request(`/api/platform/v1${path}`, null)).status).toBe(401)
    }
    const page = await web.request("/api/platform/v1/inventory?kind=deployment&limit=3")
    expect(page.status).toBe(200)
    const body = await page.json() as { items: Array<{ node: { kind: string } }> }
    expect(body).toMatchObject({ source: "snapshot", nextOffset: 3, limit: 3 })
    expect(body.items).toHaveLength(3)
    expect(body.items.every((item: { node: { kind: string } }) => item.node.kind === "deployment")).toBe(true)
    const detail = await web.request("/api/platform/v1/inventory/deployment:runtime-01:mhb-ts")
    expect(await detail.json()).toMatchObject({ node: { deployment: { subjectId: "service:web-back-ts-runtime" } },
      observations: expect.arrayContaining([expect.objectContaining({ check: "readiness", outcome: "degraded" })]) })
    expect((await web.request("/api/platform/v1/inventory/unknown")).status).toBe(404)
    for (const query of ["limit=101", "offset=-1", "kind=secret", "category=unknown", "lifecycle=retired", "integration=remote"]) {
      expect((await web.request(`/api/platform/v1/inventory?${query}`)).status).toBe(400)
    }
    const candidates = await (await web.request("/api/platform/v1/inventory?kind=program&lifecycle=candidate&integration=catalogued")).json() as { items: Array<{ node: { id: string; lifecycle: string; integration: string } }> }
    expect(candidates.items).not.toHaveLength(0)
    expect(candidates.items.every(({ node }) => node.lifecycle === "candidate" && node.integration === "catalogued")).toBe(true)
    expect(candidates.items.some(({ node }) => node.id === "program:website")).toBe(true)
    expect(await (await web.request("/api/platform/v1/summary")).json()).toMatchObject({
      byKind: { program: 40, deployment: 52 }, gaps: { unclassifiedPrograms: [], deploymentsWithoutObservations: [] }
    })
    const rdf = await web.request("/api/platform/v1/graph/jsonld")
    expect(rdf.status).toBe(200)
    expect(rdf.headers.get("content-type")).toBe("application/ld+json")
    expect(rdf.headers.get("cache-control")).toBe("private, no-store")
    expect(await rdf.json()).toMatchObject({ "@context": { "@version": 1.1 }, "mh:visibility": "internal" })
  })
  it("includes the owner-named products and keeps PostgreSQL ownership and pending MCP activation explicit", async () => {
    const web = app()
    const response = await web.request("/api/platform/v1/programs")
    const result = await response.json() as { programs: Array<{ id: string; name: string; aliases?: string[] }> }
    for (const id of ["virtual-excel", "soopoolim", "maplelineage", "usl", "hswm"])
      expect(result.programs.some((program) => program.id === `program:${id}`)).toBe(true)
    expect(result.programs.find((program) => program.id === "program:virtual-excel")).toMatchObject({ name: "버엑시", aliases: expect.arrayContaining(["Virtual Excel Simulator"]) })
    expect(catalog.edges.find((edge) => edge.id === "legacy-uses-postgres")).toMatchObject({ from: "service:legacy-domains", to: "datastore:postgresql", status: "ACTIVE" })
    expect(catalog.edges.find((edge) => edge.id === "maplelineage-uses-postgres-dev")).toMatchObject({ from: "program:maplelineage", to: "datastore:postgresql", status: "ACTIVE" })
    expect(catalog.edges.find((edge) => edge.id === "exposes:maplelineage")?.status).toBe("PROPOSED")
    const bindings = JSON.parse(readFileSync(new URL("../config/mcp-bindings.json", import.meta.url), "utf8"))
    expect(bindings.servers.find((server: { id: string }) => server.id === "maplelineage").tools.every((tool: { access: string }) => tool.access === "read")).toBe(true)
  })
  it("keeps KG evidence, recent repository revisions and stable identities", () => {
    expect(graphProblems(catalog)).toEqual([])
    expect(catalog.nodes.filter((n) => n.kind === "program").length).toBeGreaterThanOrEqual(10)
    expect(catalog.nodes.find((n) => n.id === "program:hswm")?.kgUid).toBe("sym:Concept:hswm")
    expect(catalog.nodes.find((n) => n.id === "program:usl")?.evidence.some((e) => /repo:USL@[a-f0-9]{40}/.test(e.source))).toBe(true)
  })
  it("rejects duplicate identities, dangling relationships and active dependency cycles", () => {
    const first = catalog.nodes[0]!, second = catalog.nodes[1]!, edge = catalog.edges[0]!
    expect(Either.isLeft(decodeCatalog({ ...catalog, nodes: [...catalog.nodes, first] }))).toBe(true)
    expect(Either.isLeft(decodeCatalog({ ...catalog, edges: [{ ...edge, to: "missing" }] }))).toBe(true)
    const cycle = [{ ...edge, id: "a", from: first.id, to: second.id, relation: "DEPENDS_ON" }, { ...edge, id: "b", from: second.id, to: first.id, relation: "DEPENDS_ON" }]
    expect(Either.isLeft(decodeCatalog({ ...catalog, edges: cycle }))).toBe(true)
  })
  it("does not promote proposed relationships to observed ones", () => {
    expect(neighborhood(catalog, "program:company-backend", 100).edges.some((e) => e.relation === "EXPOSES" && e.to.startsWith("mcp:"))).toBe(false)
    const expanded = neighborhood(catalog, "program:company-backend", 1, true)
    expect(expanded.edges).toHaveLength(1)
    expect(expanded.truncated).toBe(true)
  })
  it("exports directed native identities and includes relationship status in the USL meaning", () => {
    const exported = propertyGraph(catalog)
    expect(exported.nodes).toHaveLength(catalog.nodes.length)
    expect(exported.relations).toHaveLength(catalog.edges.length)
    const edge = exported.relations.find((item) => item.uid === "usl-extends-hswm")!
    expect(edge).toMatchObject({ from_uid: "program:usl", to_uid: "program:hswm", type: "EXTENDS" })
    expect(JSON.parse(edge.properties.description)).toMatchObject({ status: "ACTIVE" })
    expect(exported.nodes.every((node) => node.properties.locator.length > 0)).toBe(true)
  })
  it("requires a platform credential, independently of the public read APIs", async () => {
    const web = app()
    expect((await web.request("/api/platform/v1/programs", null)).status).toBe(401)
    expect((await web.request("/api/platform/v1/programs", "wrong")).status).toBe(401)
    expect((await web.request("/api/stats", null)).status).toBe(200)
    const response = await web.request("/api/platform/v1/programs")
    expect(response.status).toBe(200)
    expect(response.headers.get("cache-control")).toBe("private, no-store")
    expect(await response.json()).toMatchObject({ source: "snapshot", digest: "test-digest" })
  })
  it("protects and serves the backend reality graph through the same internal boundary", async () => {
    const web = app()
    for (const path of ["/reality", "/reality/export", "/reality/jsonld"]) {
      expect((await web.request(`/api/platform/v1${path}`, null)).status).toBe(401)
    }
    const response = await web.request("/api/platform/v1/reality")
    expect(response.status).toBe(200)
    expect(response.headers.get("cache-control")).toContain("no-store")
    const body = await response.json() as { schema: string; nodes: Array<{ id: string; runtime: string; evidenceState: string }>; counts: { evidenceState: Record<string, number> } }
    expect(body.schema).toBe("metahumotonic/backend-reality@1")
    expect(body.nodes.find((node) => node.id === "component:ts-http")).toMatchObject({ runtime: "candidate-only", evidenceState: "unverified" })
    expect(["healthy", "stale"].includes(body.nodes.find((node) => node.id === "component:python-public-http")?.evidenceState ?? "")).toBe(true)
    const native = await (await web.request("/api/platform/v1/reality/export")).json() as { nodes: unknown[]; relations: unknown[] }
    expect(native.nodes).toHaveLength(body.nodes.length)
    expect(native.relations.length).toBeGreaterThan(0)
    expect((await web.request("/api/platform/v1/reality/jsonld")).headers.get("content-type")).toContain("application/ld+json")
  })
  it("keeps the dated reality assessment readable during a platform PostgreSQL outage", async () => {
    const unavailable = () => Effect.fail(new Unavailable({ reason: "platform_storage_unavailable" }))
    const store: PlatformInventoryStore = {
      read: unavailable(), readiness: Effect.succeed({ platform_postgres_required: true, platform_postgres_live: false }),
      append: unavailable, receipt: unavailable, history: unavailable
    }
    const web = app({}, undefined, store)
    expect((await web.request("/api/platform/v1/inventory")).status).toBe(503)
    const response = await web.request("/api/platform/v1/reality")
    expect(response.status).toBe(200)
    expect((await response.json() as { counts: { evidenceState: Record<string, number> } }).counts.evidenceState.stale).toBeGreaterThan(0)
  })
  it("returns bounded neighborhoods and 404 for unknown IDs", async () => {
    const web = app()
    expect((await web.request("/api/platform/v1/neighbors?id=missing")).status).toBe(404)
    expect((await web.request("/api/platform/v1/neighbors?id=program:usl&limit=1000")).status).toBe(400)
    const result = await (await web.request("/api/platform/v1/neighbors?id=program:usl&limit=1")).json() as { edges: unknown[]; truncated: boolean }
    expect(result.edges).toHaveLength(1)
    expect(result.truncated).toBe(true)
  })
  it("disables company APIs when no key is configured", async () => {
    const web = app({ readKey: Redacted.make(""), writeKey: Redacted.make("") })
    expect((await web.request("/api/platform/v1/programs")).status).toBe(503)
    expect((await web.request("/mcp", READ, { method: "POST" })).status).toBe(503)
  })
})

describe("native registry contract", () => {
  it("keeps public discovery vault-disabled and registry connections private without Mongo", async () => {
    const web = app()
    const discovery = await (await web.request("/api/mcp", null)).json() as { credential_vault: { status: string } }
    expect(discovery.credential_vault.status).toBe("disabled")
    for (const path of ["servers", "health", "status", "manifest", "servers/missing"]) {
      const response = await web.request(`/api/mcp/${path}`, null)
      expect(response.status).toBe(200)
      expect((await response.json() as { source: string }).source).toBe("snapshot")
    }
    expect((await web.request("/api/mcp/vault", null)).status).toBe(404)
    const redirect = await web.request("/.well-known/mcp-servers.json", null)
    expect(redirect.status).toBe(302)
    expect(redirect.headers.get("location")).toBe("/api/mcp/manifest")
    const text = await web.request("/api/mcp/status?format=text", null)
    expect(text.headers.get("content-type")).toContain("text/plain")
    expect(await text.text()).toContain("UNAVAILABLE")
  })
  it("preserves manifest enrichment and reports stale verification separately from outages", () => {
    const server = enrichServer({ name: "mongo", category: "document", connection: { env: { URI: "<MONGO_PASSWORD>" } } })
    expect(server).toMatchObject({ capabilities: expect.arrayContaining(["query"]), auth: { type: "operator-managed" } })
    expect(server).not.toHaveProperty("connection")
    expect(server).toMatchObject({ capabilities_source: "category-inferred" })
    const projected = enrichServer({ name: "fixture", category: "document", connection: { url: "http://internal.example" }, token: "secret", backend: "private-host", notes: "private topology", auth: { type: "bearer", token: "secret" }, unknown_sentinel: "must-not-leak" })
    expect(projected).toMatchObject({ auth: { type: "operator-managed" } })
    for (const forbidden of ["connection", "token", "backend", "notes", "unknown_sentinel"]) expect(projected).not.toHaveProperty(forbidden)
    const publicManifest = manifest([], { site: "http://internal.example", notes: ["token=secret"], unknown_sentinel: "must-not-leak" })
    expect(publicManifest).toMatchObject({ site: "https://metahumotonic.com", servers: [] })
    expect(publicManifest).not.toHaveProperty("notes")
    const status = registryStatus([{ name: "old", status: "verified", verified_at: "2026-09-20" }, { name: "bad", status: "unreachable", notes: "postgres://internal:secret@host" }], Date.parse("2026-09-27"))
    expect(status.summary).toMatchObject({ verified: 1, stale: 1, down: 1, total: 2 })
    expect(JSON.stringify(status) + registryStatusText(status)).not.toContain("postgres://")
  })
})

describe("stateful domain boundary", () => {
  it("preserves body, status, idempotency, cookies, CSRF and sanitized client IP", async () => {
    let received: Record<string, unknown> = {}
    const origin = await listen(createServer(async (req, res) => {
      const chunks = []
      for await (const chunk of req) chunks.push(chunk)
      received = { url: req.url, body: Buffer.concat(chunks).toString(), headers: req.headers }
      res.writeHead(409, { "content-type": "application/json", "set-cookie": ["wiki_session=a; HttpOnly; Path=/", "wiki_csrf=b; Path=/"] })
      res.end(JSON.stringify({ detail: { code: "revision_conflict" } }))
    }))
    const response = await app({ legacyOrigin: origin }).request("/api/wiki/v1/pages/example", null, {
      method: "PATCH", headers: { "content-type": "application/json", authorization: "Bearer delegated-agent-token", "idempotency-key": "once", "x-csrf-token": "csrf", cookie: "wiki_session=old", "x-forwarded-for": "spoofed" }, body: '{"content":"new"}'
    })
    expect(response.status).toBe(409)
    expect(response.headers.getSetCookie()).toHaveLength(2)
    expect(await response.json()).toEqual({ detail: { code: "revision_conflict" } })
    expect(received).toMatchObject({ url: "/api/wiki/v1/pages/example", body: '{"content":"new"}', headers: { authorization: "Bearer delegated-agent-token", "idempotency-key": "once", "x-csrf-token": "csrf", cookie: "wiki_session=old" } })
    expect((received["headers"] as Record<string, unknown>)["x-forwarded-for"]).not.toBe("spoofed")
  })
  it("does not forward arbitrary internal routes or redirects", async () => {
    let calls = 0
    const origin = await listen(createServer((_req, res) => { calls++; res.writeHead(302, { location: "http://127.0.0.1:1/private" }); res.end() }))
    const web = app({ legacyOrigin: origin })
    expect((await web.request("/internal/not-allowed")).status).toBe(404)
    expect(calls).toBe(0)
    expect((await web.request("/api/wiki/v1/pages")).status).toBe(302)
    expect(calls).toBe(1)
  })
  it("rewrites same-owner redirects to the public entrypoint", async () => {
    const origin = await listen(createServer((req, res) => { res.writeHead(307, { location: `http://${req.headers.host}/api/wiki/v1/pages/` }); res.end() }))
    const response = await app({ legacyOrigin: origin }).request("/api/wiki/v1/pages")
    expect(response.status).toBe(307)
    expect(response.headers.get("location")).toBe("http://localhost/api/wiki/v1/pages/")
  })
  it("propagates a required legacy Wiki Redis outage into readiness", async () => {
    const origin = await listen(createServer((req, res) => {
      if (req.url !== "/ready") { res.writeHead(404); res.end(); return }
      res.writeHead(503, { "content-type": "application/json" })
      res.end(JSON.stringify({ status: "not_ready", wiki_required: true, wiki_live: false, wiki_store_live: true, wiki_rate_limit_live: false, ontology_live: false }))
    }))
    const response = await app({ legacyOrigin: origin, legacyRequired: true }).request("/ready", null)
    expect(response.status).toBe(503)
    await expect(response.json()).resolves.toMatchObject({
      status: "not_ready", wiki_required: true, wiki_live: false,
      wiki_store_live: true, wiki_rate_limit_live: false
    })
  })
  it("reports a missing required legacy owner as not ready", async () => {
    const web = app({ legacyRequired: true })
    const ready = await web.request("/ready", null)
    expect(ready.status).toBe(503)
    await expect(ready.json()).resolves.toMatchObject({ status: "not_ready", wiki_live: false, wiki_store_live: false })
    expect((await web.request("/health", null)).status).toBe(200)
    expect((await web.request("/api/wiki/v1/pages", null)).status).toBe(503)
  })
  it("does not report an enabled ontology plane as ready without its domain owner", async () => {
    const response = await app({ ontologyRequired: true }).request("/ready", null)
    expect(response.status).toBe(503)
    expect(await response.json()).toMatchObject({ ontology_required: true, ontology_live: false })
  })
  it("rejects oversized input before contacting the domain owner", async () => {
    let calls = 0
    const origin = await listen(createServer((_req, res) => { calls++; res.end("bad") }))
    const response = await app({ legacyOrigin: origin, maxBodyBytes: 16 }).request("/api/wiki/v1/pages", null, { method: "POST", headers: { "content-length": "32" }, body: "x".repeat(32) })
    expect(response.status).toBe(413)
    expect(calls).toBe(0)
  })
  it("enforces the body limit when Content-Length is absent", async () => {
    const response = await app({ legacyOrigin: "http://127.0.0.1:1", maxBodyBytes: 16 }).request("/api/wiki/v1/pages", null, { method: "POST", body: "x".repeat(32) })
    expect(response.status).toBe(413)
  })
})

describe("standard MCP entrypoint and federation", () => {
  it("exposes the same bounded inventory and graph export through the official MCP client", async () => {
    const web = app()
    const client = await connect(web.handler)
    const call = await client.callTool({ name: "platform_inventory", arguments: { owner: "repository:game", kind: "program", limit: 2 } })
    const content = call.content as Array<{ type: string; text: string }>
    const mcp = JSON.parse(content[0]!.text)
    const http = await (await web.request("/api/platform/v1/inventory?owner=repository:game&kind=program&limit=2")).json() as { items: unknown[]; total: number }
    expect(mcp.items).toEqual(http.items)
    expect(mcp.total).toBe(http.total)
    expect(mcp.nextOffset).toBe(2)
    const candidateCall = await client.callTool({ name: "platform_inventory", arguments: { kind: "program", lifecycle: "candidate", integration: "catalogued", limit: 100 } })
    const candidateContent = candidateCall.content as Array<{ type: string; text: string }>
    const candidateMcp = JSON.parse(candidateContent[0]!.text) as { items: Array<{ node: { id: string; lifecycle: string; integration: string } }> }
    expect(candidateMcp.items).not.toHaveLength(0)
    expect(candidateMcp.items.every(({ node }) => node.lifecycle === "candidate" && node.integration === "catalogued")).toBe(true)
    expect(candidateMcp.items.some(({ node }) => node.id === "program:website")).toBe(true)
    const summary = await client.callTool({ name: "platform_summary", arguments: {} })
    expect(JSON.stringify(summary)).toContain("programsWithoutOwner")
    const exported = await client.callTool({ name: "platform_export", arguments: { format: "jsonld" } })
    expect(exported.isError).not.toBe(true)
    expect(JSON.stringify(exported)).toContain("rdf:Statement")
    expect(JSON.stringify(exported)).toContain("PROPOSED")
    const reality = await client.callTool({ name: "platform_reality", arguments: { format: "catalog" } })
    expect(reality.isError).not.toBe(true)
    expect(JSON.stringify(reality)).toContain("component:python-public-http")
    expect(JSON.stringify(reality)).toContain("component:ts-http")
    expect((await client.callTool({ name: "platform_inventory", arguments: { limit: 101 } })).isError).toBe(true)
    expect((await client.callTool({ name: "platform_inventory", arguments: { lifecycle: "retired" } })).isError).toBe(true)
  })
  it("negotiates with the official client and executes catalog tools", async () => {
    const web = app()
    const client = await connect(web.handler)
    const inventory = await client.listTools()
    expect(inventory.tools.map((tool) => tool.name)).toContain("platform_programs")
    const learning = await client.callTool({ name: "platform_learning", arguments: { format: "usl" } })
    expect(JSON.stringify(learning)).toContain("https://metahumotonic.com/learn/#entity-usl")
    expect(JSON.stringify(learning)).not.toContain("kg://canonical-neo4j")
    const result = await client.callTool({ name: "platform_programs", arguments: {} })
    expect(result).toMatchObject({ content: [{ type: "text", text: expect.stringContaining("program:hswm") }] })
  })
  it("rejects browser-origin MCP requests and anonymous calls", async () => {
    const web = app()
    expect((await web.request("/mcp", null, { method: "POST" })).status).toBe(401)
    expect((await web.request("/mcp", READ, { method: "POST", headers: { origin: "https://evil.example" } })).status).toBe(403)
    expect((await web.request("/mcp", READ)).status).toBe(405)
  })
  it("allows only configured tools and scopes while never disclosing upstream credentials", async () => {
    let readCalls = 0, writeCalls = 0
    const upstream = await listen(createServer(async (req, res) => {
      const server = new McpServer({ name: "bounded-fixture", version: "1" })
      server.registerTool("read", { inputSchema: { value: z.string() } }, ({ value }) => { readCalls++; return { content: [{ type: "text", text: value }] } })
      server.registerTool("write", { inputSchema: {} }, () => { writeCalls++; return { content: [{ type: "text", text: "written" }] } })
      server.registerTool("hidden", { inputSchema: {} }, () => ({ content: [{ type: "text", text: "not exposed" }] }))
      const transport = new StreamableHTTPServerTransport({ enableJsonResponse: true })
      res.on("close", () => { void server.close() })
      await server.connect(transport as Transport)
      await transport.handleRequest(req, res)
    }))
    const web = app({ timeoutMs: 5000, bindings: [{ id: "fixture", nodeId: "mcp:ontology", urlEnv: "FIXTURE_URL", tokenEnv: "FIXTURE_TOKEN", url: Redacted.make(`${upstream}/mcp`), token: Redacted.make("upstream-secret"),
      tools: [{ name: "read", access: "read", description: "fixture read" }, { name: "write", access: "write", description: "fixture write" }] }] })
    const services = await (await web.request("/api/platform/v1/services")).text()
    expect(services).not.toContain("upstream-secret")
    expect(services).not.toContain(upstream)
    expect(services).not.toContain('"write"')
    const inventory = await (await web.request("/api/platform/v1/mcp/fixture/tools")).json() as { tools: Array<{ name: string }> }
    expect(inventory.tools.map((tool: { name: string }) => tool.name)).toEqual(["read"])
    const call = (tool: string, key = READ) => web.request("/api/platform/v1/mcp/fixture/call", key, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ tool, arguments: tool === "read" ? { value: "hello" } : {} }) })
    expect((await call("hidden", WRITE)).status).toBe(403)
    expect((await call("write")).status).toBe(403)
    expect(writeCalls).toBe(0)
    expect((await call("read")).status).toBe(200)
    expect(readCalls).toBe(1)
    expect((await call("write", WRITE)).status).toBe(200)
    expect(writeCalls).toBe(1)
  })
})
