import { Clock, Context, Effect, Layer } from "effect"
import type { Document } from "mongodb"
import { AppConfigTag } from "../Config.js"
import { NotFound, Unavailable } from "../domain/Errors.js"
import { make as makeCache } from "./Cache.js"
import { MongoTag, mongoAttempt } from "./Mongo.js"

export const REGISTRY_SCHEMA = "metahumotonic/mcp-registry@1"
const SITE = "https://metahumotonic.com"
export const VAULT_SPEC = {
  status: "disabled", access: "operator-cli-only"
}
const categoryCapabilities: Readonly<Record<string, ReadonlyArray<string>>> = {
  graph: ["cypher.read", "cypher.write", "schema.inspect"],
  vector: ["kv.crud", "vector.search", "json.document", "streams", "pubsub"],
  document: ["document.crud", "query", "aggregation", "index.admin"],
  storage: ["bucket.admin", "object.read", "object.write", "presigned-url"], utility: ["tools"]
}
export const enrichServer = (server: Document) => {
  const connection = server["connection"] as Record<string, unknown> | undefined
  const { connection: _connection, ...publicServer } = server
  const hasOperatorCredential = /<[A-Z][A-Z0-9_]*>/.test(JSON.stringify(connection ?? {}))
  return {
    "@type": ["schema:SoftwareApplication", "mhb:McpServer"], "@id": `${SITE}/api/mcp/servers/${server["name"] ?? ""}`, ...publicServer,
    capabilities: Array.isArray(server["capabilities"]) && server["capabilities"].length ? server["capabilities"] : categoryCapabilities[String(server["category"])] ?? ["tools"],
    auth: server["auth"] && Object.keys(server["auth"]).length ? server["auth"] : hasOperatorCredential
      ? { type: "operator-managed" }
      : { type: "none" }
  }
}
export const manifest = (servers: ReadonlyArray<Document>, meta: Document = {}) => ({
  "@context": { schema: "https://schema.org/", mhb: "https://metahumotonic.com/mcp/ontology#" },
  "@type": ["schema:ItemList", "mhb:McpRegistry"], schema: REGISTRY_SCHEMA,
  updated: meta["updated"] || servers.map((s) => String(s["verified_at"] ?? "")).sort().at(-1) || "",
  site: meta["site"] ?? SITE, notes: meta["notes"] ?? [], credential_vault: VAULT_SPEC, servers: servers.map(enrichServer)
})
const iso = (value: unknown): string | null => {
  if (value === null || value === undefined || value === "") return null
  const date = value instanceof Date ? value : new Date(String(value))
  return Number.isFinite(date.valueOf()) ? date.toISOString() : null
}
export const registryStatus = (items: ReadonlyArray<Document>, now: number) => {
  const counts = { total: items.length, verified: 0, available: 0, down: 0, unused: 0, stale: 0 }
  let lastVerify: string | null = null
  const servers = [...items].sort((a, b) => String(a["name"]).localeCompare(String(b["name"]))).map((item) => {
    const status = String(item["status"] || "unknown"), probe = iso(item["last_probe_at"]), verified = iso(item["verified_at"])
    const checked = probe ?? verified
    const stale = checked === null || now - Date.parse(checked) > 86_400_000
    if (probe !== null && (lastVerify === null || probe > lastVerify)) lastVerify = probe
    const badge = status === "unreachable" ? "down" : status === "verified" && stale ? "stale" : status
    if (status === "verified") counts.verified++
    if (status === "available") counts.available++
    if (status === "unused") counts.unused++
    if (badge === "down") counts.down++
    if (badge === "stale") counts.stale++
    return { name: item["name"], status, badge, stale, verified_at: item["verified_at"] ?? null,
      last_probe_at: item["last_probe_at"] ?? null, last_check_at: checked, notes: item["notes"] ?? null }
  })
  return { schema: REGISTRY_SCHEMA, source: "live", generated_at: new Date(now).toISOString(), last_verify_at: lastVerify,
    stale_after_hours: 24, summary: counts, servers }
}
export const registryStatusText = (payload: Record<string, unknown>): string => {
  if (payload["source"] !== "live") return "metahumotonic MCP registry status UNAVAILABLE (snapshot fallback — registry store unreachable)\n"
  const p = payload as ReturnType<typeof registryStatus>, s = p.summary
  return [`metahumotonic MCP registry status @ ${p.generated_at} — ${s.total} servers: ${s.verified} verified (${s.stale} stale), ${s.available} available, ${s.down} down, ${s.unused} unused; last verify run: ${p.last_verify_at ?? "never"}`,
    ...p.servers.map((server) => `${server.name}: ${server.badge}, last check ${server.last_check_at ?? "never"}`)].join("\n") + "\n"
}
export interface McpRegistry {
  readonly read: (surface: "servers" | "server" | "manifest" | "health" | "status" | "vault", name?: string) => Effect.Effect<Record<string, unknown>, NotFound>
}
export class McpRegistryTag extends Context.Tag("McpRegistry")<McpRegistryTag, McpRegistry>() {}
export const McpRegistryLive = Layer.effect(McpRegistryTag, Effect.gen(function* () {
  const cfg = yield* AppConfigTag
  const { db } = yield* MongoTag
  const collection = db?.collection<Document & { _id: string }>(cfg.mcpRegistryCollection)
  const query = <A>(f: (col: NonNullable<typeof collection>) => Promise<A>) =>
    collection ? mongoAttempt(() => f(collection)) : Effect.fail(new Unavailable({ reason: "registry unavailable" }))
  const servers = query((col) => col.find({ kind: "server" }, { projection: { _id: 0, kind: 0 } }).sort({ name: 1 }).limit(2000).toArray())
  const cache = yield* makeCache<Record<string, unknown>>({ ttlSeconds: cfg.mcpRegistryCacheTtlSeconds, maxEntries: 64 })
  return {
    read: (surface, name) => cache.get(`${surface}:${name ?? ""}`, Effect.gen(function* () {
      const envelope = { schema: REGISTRY_SCHEMA, source: "live" }
      if (surface === "server") {
        const item = yield* query((col) => col.findOne({ kind: "server", name }, { projection: { _id: 0, kind: 0 } }))
        if (!item) return yield* Effect.fail(new NotFound({ reason: `mcp server not found: ${name}` }))
        return { ...envelope, server: enrichServer(item) }
      }
      if (surface === "vault") return yield* Effect.fail(new NotFound({ reason: "credential vault is not publicly available" }))
      const items = yield* servers
      if (surface === "manifest") {
        const meta = yield* query((col) => col.findOne({ _id: "manifest_meta" }, { projection: { _id: 0, kind: 0 } }))
        return { ...manifest(items, meta ?? {}), source: "live" }
      }
      if (surface === "status") return registryStatus(items, yield* Clock.currentTimeMillis)
      const result = surface === "health" ? items.map((item) => Object.fromEntries(
        ["name", "status", "verified_at", "last_probe_at", "notes"].map((field) => [field, item[field] ?? null])
      )) : items.map(enrichServer)
      return { ...envelope, count: result.length, servers: result }
    })).pipe(Effect.catchTag("Unavailable", () => Effect.succeed({
      ...(surface === "manifest" ? manifest([]) : { schema: REGISTRY_SCHEMA }), source: "snapshot",
      ...(surface === "server" ? { server: null } : surface === "vault" ? { vault: null } : surface === "status" ? { summary: null, servers: [] } : { count: 0, servers: [] })
    })))
  } satisfies McpRegistry
}))
