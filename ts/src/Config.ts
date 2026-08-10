/**
 * Runtime settings — a port of `app/config.py`.
 *
 * pydantic-settings read `MHB_*` at import time and produced a mutable module
 * global. Here the settings are an ordinary Effect service: read once at
 * startup, immutable afterwards, and *requestable* — a handler that needs
 * config declares it in its `R` channel, so a test can supply a different
 * `AppConfig` without touching `process.env`.
 */
import { Config, Context, Effect, Layer, Redacted } from "effect"

export interface AppConfig {
  // --- server ---
  readonly host: string
  readonly port: number
  readonly version: string

  // --- Neo4j (read-only KG queries) ---
  readonly neo4jUri: string
  readonly neo4jFallbackUris: ReadonlyArray<string>
  readonly neo4jUser: string
  readonly neo4jPassword: Redacted.Redacted<string>
  readonly neo4jDatabase: string
  /** opt-in; default off → snapshot fallback (CI / offline) */
  readonly neo4jLive: boolean

  // --- KG Cypher proxy ---
  /** Community Neo4j has no RBAC, so read/write separation is enforced HERE.
   *  Empty key → that endpoint is disabled (503). The write key is a superset:
   *  it is also accepted on /api/kg/read. */
  readonly kgReadKey: Redacted.Redacted<string>
  readonly kgWriteKey: Redacted.Redacted<string>
  readonly kgProxyMaxRows: number
  readonly kgQueryTimeoutSeconds: number

  // --- feedback intake ---
  readonly mongoUri: Redacted.Redacted<string>
  readonly mongoDb: string
  readonly mongoFeedbackCollection: string
  readonly feedbackTtlDays: number
  /** Reject rather than falsely acknowledge an in-memory fallback that would
   *  disappear on restart. */
  readonly feedbackRequireDurable: boolean
  readonly feedbackAdminKey: Redacted.Redacted<string>
  readonly feedbackMaxPerWindow: number
  readonly feedbackWindowSeconds: number

  // --- MCP registry ---
  readonly mcpRegistryCollection: string
  readonly mcpRegistryCacheTtlSeconds: number

  // --- caches ---
  readonly statsCacheTtlSeconds: number
  readonly researchCacheTtlSeconds: number
  readonly cacheMaxEntries: number
  readonly researchMaxOffset: number

  // --- Redis ---
  readonly redisUrl: Redacted.Redacted<string>

  // --- public community wiki (writes are fail-closed) ---
  readonly wikiPublicWrites: boolean
  readonly wikiDatabaseUrl: Redacted.Redacted<string>
  readonly wikiSessionSecret: Redacted.Redacted<string>
  readonly wikiModerationAdminKey: Redacted.Redacted<string>
  readonly wikiSessionTtlSeconds: number
  readonly wikiSessionCookieSecure: boolean
  readonly wikiRequireRedis: boolean
  readonly wikiSessionMaxPerWindow: number
  readonly wikiSessionWindowSeconds: number
  readonly wikiMutationMaxPerWindow: number
  readonly wikiMutationWindowSeconds: number
  readonly wikiReadMaxPerWindow: number
  readonly wikiReadWindowSeconds: number
  readonly wikiMaxBodyBytes: number
  readonly wikiMaxOffset: number

  // --- observability ---
  readonly metricsEnabled: boolean
  readonly logJson: boolean

  // --- Turnstile bot defense ---
  readonly turnstileSecret: Redacted.Redacted<string>
  readonly turnstileHostname: string
  readonly turnstileAction: string
  readonly turnstileFailOpen: boolean

  // --- CORS ---
  readonly corsOrigins: ReadonlyArray<string>
  readonly trustProxy: boolean
}

export class AppConfigTag extends Context.Tag("AppConfig")<AppConfigTag, AppConfig>() {}

/** `MHB_` prefix, matching `SettingsConfigDict(env_prefix="MHB_")`. */
const mhb = (name: string) => `MHB_${name}`

const str = (name: string, fallback: string) =>
  Config.string(mhb(name)).pipe(Config.withDefault(fallback))

const secret = (name: string) =>
  Config.redacted(mhb(name)).pipe(Config.withDefault(Redacted.make("")))

const int = (name: string, fallback: number) =>
  Config.integer(mhb(name)).pipe(Config.withDefault(fallback))

const num = (name: string, fallback: number) =>
  Config.number(mhb(name)).pipe(Config.withDefault(fallback))

const bool = (name: string, fallback: boolean) =>
  Config.boolean(mhb(name)).pipe(Config.withDefault(fallback))

const csv = (name: string, fallback: string) =>
  str(name, fallback).pipe(
    Config.map((raw) =>
      raw
        .split(",")
        .map((o) => o.trim())
        .filter((o) => o.length > 0)
    )
  )

export const configFromEnv: Config.Config<AppConfig> = Config.all({
  host: str("HOST", "0.0.0.0"),
  port: int("PORT", 8000),
  version: str("VERSION", "1.0.0"),

  neo4jUri: str("NEO4J_URI", "bolt://localhost:7687"),
  neo4jFallbackUris: csv("NEO4J_FALLBACK_URIS", ""),
  neo4jUser: str("NEO4J_USER", "neo4j"),
  neo4jPassword: secret("NEO4J_PASSWORD"),
  neo4jDatabase: str("NEO4J_DATABASE", "neo4j"),
  neo4jLive: bool("NEO4J_LIVE", false),

  kgReadKey: secret("KG_READ_KEY"),
  kgWriteKey: secret("KG_WRITE_KEY"),
  kgProxyMaxRows: int("KG_PROXY_MAX_ROWS", 1000),
  kgQueryTimeoutSeconds: num("KG_QUERY_TIMEOUT_SECONDS", 10.0),

  mongoUri: secret("MONGO_URI"),
  mongoDb: str("MONGO_DB", "metahumotonic"),
  mongoFeedbackCollection: str("MONGO_FEEDBACK_COLLECTION", "web_feedback"),
  feedbackTtlDays: int("FEEDBACK_TTL_DAYS", 365),
  feedbackRequireDurable: bool("FEEDBACK_REQUIRE_DURABLE", false),
  feedbackAdminKey: secret("FEEDBACK_ADMIN_KEY"),
  feedbackMaxPerWindow: int("FEEDBACK_MAX_PER_WINDOW", 5),
  feedbackWindowSeconds: int("FEEDBACK_WINDOW_SECONDS", 600),

  mcpRegistryCollection: str("MCP_REGISTRY_COLLECTION", "mcp_servers"),
  mcpRegistryCacheTtlSeconds: int("MCP_REGISTRY_CACHE_TTL_SECONDS", 300),

  statsCacheTtlSeconds: int("STATS_CACHE_TTL_SECONDS", 120),
  researchCacheTtlSeconds: int("RESEARCH_CACHE_TTL_SECONDS", 300),
  cacheMaxEntries: int("CACHE_MAX_ENTRIES", 512),
  researchMaxOffset: int("RESEARCH_MAX_OFFSET", 10000),

  redisUrl: secret("REDIS_URL"),

  wikiPublicWrites: bool("WIKI_PUBLIC_WRITES", false),
  wikiDatabaseUrl: secret("WIKI_DATABASE_URL"),
  wikiSessionSecret: secret("WIKI_SESSION_SECRET"),
  wikiModerationAdminKey: secret("WIKI_MODERATION_ADMIN_KEY"),
  wikiSessionTtlSeconds: int("WIKI_SESSION_TTL_SECONDS", 43_200),
  wikiSessionCookieSecure: bool("WIKI_SESSION_COOKIE_SECURE", true),
  wikiRequireRedis: bool("WIKI_REQUIRE_REDIS", true),
  wikiSessionMaxPerWindow: int("WIKI_SESSION_MAX_PER_WINDOW", 10),
  wikiSessionWindowSeconds: int("WIKI_SESSION_WINDOW_SECONDS", 600),
  wikiMutationMaxPerWindow: int("WIKI_MUTATION_MAX_PER_WINDOW", 30),
  wikiMutationWindowSeconds: int("WIKI_MUTATION_WINDOW_SECONDS", 60),
  wikiReadMaxPerWindow: int("WIKI_READ_MAX_PER_WINDOW", 180),
  wikiReadWindowSeconds: int("WIKI_READ_WINDOW_SECONDS", 60),
  wikiMaxBodyBytes: int("WIKI_MAX_BODY_BYTES", 524_288),
  wikiMaxOffset: int("WIKI_MAX_OFFSET", 10_000),

  metricsEnabled: bool("METRICS_ENABLED", true),
  logJson: bool("LOG_JSON", true),

  turnstileSecret: secret("TURNSTILE_SECRET"),
  turnstileHostname: str("TURNSTILE_HOSTNAME", "metahumotonic.com"),
  turnstileAction: str("TURNSTILE_ACTION", "feedback_submit"),
  turnstileFailOpen: bool("TURNSTILE_FAIL_OPEN", false),

  corsOrigins: csv("CORS_ORIGINS", "https://metahumotonic.com,http://localhost:4321"),
  trustProxy: bool("TRUST_PROXY", false)
})

export const AppConfigLive = Layer.effect(AppConfigTag, configFromEnv)

/**
 * Startup validation — the port of `_validate_wiki_configuration()`.
 *
 * Kept as a standalone Effect rather than a constructor side effect so it can
 * be unit-tested against a hand-built config, exactly like the Python test.
 */
export class InvalidConfiguration {
  readonly _tag = "InvalidConfiguration"
  constructor(readonly reason: string) {}
}

export const validateWikiConfiguration = (
  cfg: AppConfig
): Effect.Effect<void, InvalidConfiguration> =>
  Effect.gen(function* () {
    const fail = (reason: string) => Effect.fail(new InvalidConfiguration(reason))
    const bytes = (r: Redacted.Redacted<string>) =>
      Buffer.byteLength(Redacted.value(r), "utf8")

    if (cfg.wikiSessionTtlSeconds < 300) {
      return yield* fail("MHB_WIKI_SESSION_TTL_SECONDS must be at least 300")
    }
    if (!cfg.wikiPublicWrites) return

    if (Redacted.value(cfg.wikiDatabaseUrl) === "") {
      return yield* fail("MHB_WIKI_DATABASE_URL is required when wiki writes are enabled")
    }
    if (bytes(cfg.wikiSessionSecret) < 32) {
      return yield* fail("MHB_WIKI_SESSION_SECRET must be at least 32 bytes")
    }
    if (bytes(cfg.wikiModerationAdminKey) < 32) {
      return yield* fail("MHB_WIKI_MODERATION_ADMIN_KEY must be at least 32 bytes")
    }
    if (Redacted.value(cfg.wikiModerationAdminKey) === Redacted.value(cfg.wikiSessionSecret)) {
      return yield* fail("wiki moderation and session-signing secrets must be distinct")
    }
    if (cfg.wikiRequireRedis && Redacted.value(cfg.redisUrl) === "") {
      return yield* fail("MHB_REDIS_URL is required when public wiki writes are enabled")
    }
  })
