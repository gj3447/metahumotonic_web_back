import { Config, Context, Effect, Layer, Redacted, Schema } from "effect"
import { readFile, stat } from "node:fs/promises"
import { fileURLToPath } from "node:url"
import { contentDigest } from "./Digest.js"
import { decodeCatalog, type PlatformCatalog } from "../domain/PlatformGraph.js"

const Binding = Schema.Struct({
  id: Schema.String.pipe(Schema.pattern(/^[a-z][a-z0-9-]{0,63}$/)),
  nodeId: Schema.String,
  urlEnv: Schema.String.pipe(Schema.pattern(/^[A-Z][A-Z0-9_]+$/)),
  tokenEnv: Schema.optional(Schema.String.pipe(Schema.pattern(/^[A-Z][A-Z0-9_]+$/))),
  tools: Schema.Array(Schema.Struct({
    name: Schema.String.pipe(Schema.pattern(/^[A-Za-z0-9_.-]{1,100}$/)),
    access: Schema.Literal("read", "write"),
    description: Schema.String.pipe(Schema.maxLength(1000))
  })).pipe(Schema.maxItems(50))
})
const BindingsFile = Schema.Struct({ schema: Schema.Literal("metahumotonic/mcp-bindings@1"), servers: Schema.Array(Binding).pipe(Schema.maxItems(50)) })
export interface McpBinding extends Schema.Schema.Type<typeof Binding> {
  readonly url: Redacted.Redacted<string> | null
  readonly token: Redacted.Redacted<string>
}
export interface PlatformConfig {
  readonly readKey: Redacted.Redacted<string>
  readonly writeKey: Redacted.Redacted<string>
  readonly legacyOrigin: string | null
  readonly legacyRequired: boolean
  readonly ontologyRequired: boolean
  readonly catalog: PlatformCatalog
  readonly catalogDigest: string
  readonly bindings: ReadonlyArray<McpBinding>
  readonly maxBodyBytes: number
  readonly timeoutMs: number
}
export class PlatformConfigTag extends Context.Tag("PlatformConfig")<PlatformConfigTag, PlatformConfig>() {}

const readJson = (path: string) => Effect.tryPromise({ try: async () => {
  const info = await stat(path)
  if (!info.isFile() || info.size > 1_048_576) throw new Error("configuration must be a file <= 1 MiB")
  return JSON.parse(await readFile(path, "utf8")) as unknown
}, catch: () => new Error(`cannot read platform configuration: ${path}`) })
const secret = (name: string) => Config.redacted(name).pipe(Config.withDefault(Redacted.make("")))
export const PlatformConfigLive = Layer.effect(PlatformConfigTag, Effect.gen(function* () {
  const configuredCatalog = yield* Config.string("MHB_PLATFORM_CATALOG").pipe(Config.withDefault(""))
  const catalogPath = configuredCatalog || fileURLToPath(new URL("../../config/platform-catalog.json", import.meta.url))
  const catalog = yield* decodeCatalog(yield* readJson(catalogPath))
  const configuredBindings = yield* Config.string("MHB_MCP_BINDINGS").pipe(Config.withDefault(""))
  const bindingsPath = configuredBindings || fileURLToPath(new URL("../../config/mcp-bindings.json", import.meta.url))
  const rawBindings = yield* Schema.decodeUnknown(BindingsFile, { onExcessProperty: "error" })(yield* readJson(bindingsPath))
  const bindings: McpBinding[] = []
  const ids = new Set<string>()
  for (const binding of rawBindings.servers) {
    if (ids.has(binding.id) || !catalog.nodes.some((node) => node.id === binding.nodeId && node.kind === "mcp-server")) {
      return yield* Effect.fail(new Error("duplicate MCP binding or missing MCP graph node"))
    }
    ids.add(binding.id)
    if (new Set(binding.tools.map((tool) => tool.name)).size !== binding.tools.length) return yield* Effect.fail(new Error("duplicate MCP tool"))
    const address = yield* Config.string(binding.urlEnv).pipe(Config.withDefault(""))
    if (address) {
      const url = yield* Effect.try({ try: () => new URL(address), catch: () => new Error("invalid MCP endpoint URL") })
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.hash) {
        return yield* Effect.fail(new Error("invalid MCP endpoint configuration"))
      }
    }
    bindings.push({ ...binding, url: address ? Redacted.make(address) : null,
      token: binding.tokenEnv ? yield* secret(binding.tokenEnv) : Redacted.make("") })
  }
  const origin = yield* Config.string("MHB_LEGACY_ORIGIN").pipe(Config.withDefault(""))
  if (origin) {
    const url = yield* Effect.try({ try: () => new URL(origin), catch: () => new Error("invalid legacy service origin") })
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
      return yield* Effect.fail(new Error("MHB_LEGACY_ORIGIN must be an HTTP origin without credentials or path"))
    }
  }
  const readKey = yield* secret("MHB_PLATFORM_READ_KEY")
  const writeKey = yield* secret("MHB_PLATFORM_WRITE_KEY")
  if (Redacted.value(readKey) && Redacted.value(readKey) === Redacted.value(writeKey)) return yield* Effect.fail(new Error("platform read and write keys must differ"))
  return {
    readKey, writeKey, legacyOrigin: origin || null,
    legacyRequired: yield* Config.boolean("MHB_LEGACY_REQUIRED").pipe(Config.withDefault(false)),
    ontologyRequired: yield* Config.boolean("MHB_ONTOLOGY_ENABLED").pipe(Config.withDefault(false)),
    catalog, catalogDigest: contentDigest(catalog),
    bindings, maxBodyBytes: 524_288, timeoutMs: 10_000
  }
}).pipe(Effect.orDie))
