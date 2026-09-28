import { Context, Effect, Layer, Schema } from "effect"
import { PlatformConfigTag } from "../platform/Config.js"
import { BodyLimitError, readBounded } from "../platform/BoundedHttp.js"
import { PayloadTooLarge, Unavailable } from "../domain/Errors.js"

/** These stateful domains retain one owner during the migration. This is not a catch-all proxy. */
export const legacyPath = (path: string): boolean => ["/api/wiki/v1", "/internal/wiki/moderation", "/api/v1/ontology"]
  .some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
const Ready = Schema.Struct({
  status: Schema.Literal("ready", "not_ready"),
  wiki_live: Schema.Boolean, wiki_store_live: Schema.Boolean, wiki_rate_limit_live: Schema.Boolean,
  ontology_live: Schema.optionalWith(Schema.Boolean, { default: () => false })
})
export interface LegacyService {
  readonly readiness: Effect.Effect<typeof Ready.Type | null>
  readonly forward: (request: Request, clientIp: string) => Effect.Effect<Response, Unavailable | PayloadTooLarge>
}
export class LegacyServiceTag extends Context.Tag("LegacyService")<LegacyServiceTag, LegacyService>() {}
const forwardedHeaders = ["accept", "content-type", "cookie", "origin", "x-csrf-token", "idempotency-key", "x-api-key", "x-ontology-key", "if-none-match", "if-match", "user-agent"]
export const LegacyServiceLive = Layer.effect(LegacyServiceTag, Effect.gen(function* () {
  const cfg = yield* PlatformConfigTag
  return {
    readiness: !cfg.legacyOrigin ? Effect.succeed(null) : Effect.tryPromise({ try: async (signal) => {
      const response = await fetch(`${cfg.legacyOrigin}/ready`, { redirect: "error", signal: AbortSignal.any([signal, AbortSignal.timeout(3000)]) })
      const bytes = await readBounded(response.body, 16_384)
      if (!response.ok && response.status !== 503) throw new Error("legacy not ready")
      return JSON.parse(Buffer.from(bytes).toString("utf8")) as unknown
    }, catch: () => new Unavailable({ reason: "legacy service unavailable" }) }).pipe(
      Effect.flatMap(Schema.decodeUnknown(Ready)), Effect.catchAll(() => Effect.succeed(null))
    ),
    forward: (request, clientIp) => Effect.tryPromise({ try: async (signal) => {
      const url = new URL(request.url)
      if (!cfg.legacyOrigin || !legacyPath(url.pathname) || /%(?:2e|2f|5c)/i.test(url.pathname) || url.pathname.includes("\\")) {
        throw new Error("legacy route unavailable")
      }
      const headers = new Headers()
      for (const name of forwardedHeaders) { const value = request.headers.get(name); if (value !== null) headers.set(name, value) }
      // The TS boundary has already applied TRUST_PROXY. Never forward caller-supplied IP chains.
      headers.set("x-forwarded-for", clientIp)
      headers.set("cf-connecting-ip", clientIp)
      const body = request.method === "GET" || request.method === "HEAD" ? undefined : await readBounded(request.body, cfg.maxBodyBytes)
      const response = await fetch(`${cfg.legacyOrigin}${url.pathname}${url.search}`, {
        method: request.method, headers, ...(body === undefined ? {} : { body: Buffer.from(body) }),
        redirect: "manual", signal: AbortSignal.any([signal, request.signal, AbortSignal.timeout(cfg.timeoutMs)])
      })
      const bytes = await readBounded(response.body, 4_194_304).catch(() => { throw new Error("invalid upstream body") })
      const returned = new Headers(response.headers)
      for (const name of ["connection", "transfer-encoding", "content-length", "content-encoding", "access-control-allow-origin", "access-control-allow-credentials"]) returned.delete(name)
      returned.set("x-mhb-service", "legacy-domain")
      const location = returned.get("location")
      if (location) {
        const redirected = new URL(location, cfg.legacyOrigin)
        if (redirected.origin === cfg.legacyOrigin) returned.set("location", `${url.origin}${redirected.pathname}${redirected.search}${redirected.hash}`)
      }
      return new Response([204, 205, 304].includes(response.status) ? null : Buffer.from(bytes), { status: response.status, headers: returned })
    }, catch: (error) => error instanceof BodyLimitError
      ? new PayloadTooLarge({ reason: "request body too large", maxBytes: cfg.maxBodyBytes })
      : new Unavailable({ reason: "legacy service unavailable" }) })
  } satisfies LegacyService
}))
