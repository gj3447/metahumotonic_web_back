import { HttpApiBuilder, HttpIncomingMessage, HttpServerRequest, HttpServerResponse } from "@effect/platform"
import { Clock, Effect, Option, Ref } from "effect"
import { LegacyServiceTag, legacyPath } from "../ports/LegacyService.js"
import { ClientIpTag } from "../ports/ClientIp.js"
import { handleMcp } from "../platform/McpServer.js"
import { PlatformConfigTag } from "../platform/Config.js"
import { AppConfigTag } from "../Config.js"

export const PlatformBoundary = HttpApiBuilder.middleware(Effect.gen(function* () {
  const legacy = yield* LegacyServiceTag
  const cfg = yield* PlatformConfigTag
  const appConfig = yield* AppConfigTag
  const requests = yield* Ref.make<ReadonlyMap<number, number>>(new Map())
  const started = yield* Clock.currentTimeMillis
  return (app) => Effect.gen(function* () {
    const request = yield* HttpServerRequest.HttpServerRequest
    const path = new URL(request.url, "http://localhost").pathname
    // A private read canary must be unable to mutate any configured downstream.
    if (cfg.shadowReadOnly && !["GET", "HEAD", "OPTIONS"].includes(request.method)) {
      return HttpServerResponse.unsafeJson({ reason: "shadow_read_only" }, {
        status: 405, headers: { allow: "GET, HEAD, OPTIONS" }
      })
    }
    if (path === "/metrics") {
      if (!appConfig.metricsEnabled) return HttpServerResponse.empty({ status: 404 })
      const counts = yield* Ref.get(requests)
      const now = yield* Clock.currentTimeMillis
      return HttpServerResponse.text([
        "# HELP mhb_http_requests_total Completed HTTP requests by status.",
        "# TYPE mhb_http_requests_total counter",
        ...[...counts].map(([status, count]) => `mhb_http_requests_total{status="${status}"} ${count}`),
        "# HELP mhb_uptime_seconds Process application uptime.",
        "# TYPE mhb_uptime_seconds gauge", `mhb_uptime_seconds ${(now - started) / 1000}`, ""
      ].join("\n"), { contentType: "text/plain; version=0.0.4" })
    }
    if (path === "/.well-known/mcp-servers.json") return HttpServerResponse.redirect("/api/mcp/manifest", { status: 302 })
    if (path === "/api/mcp/") return HttpServerResponse.redirect("/api/mcp", { status: 307 })
    if (Number(request.headers["content-length"] ?? "0") > cfg.maxBodyBytes) return HttpServerResponse.unsafeJson({ reason: "request body too large" }, { status: 413 })
    if (path !== "/mcp" && !legacyPath(path)) {
      const response = yield* HttpIncomingMessage.withMaxBodySize(app, Option.some(cfg.maxBodyBytes))
      yield* Ref.update(requests, (counts) => new Map(counts).set(response.status, (counts.get(response.status) ?? 0) + 1))
      return path.startsWith("/api/platform/") ? HttpServerResponse.setHeader(response, "cache-control", "private, no-store") : response
    }
    const forwarded = Effect.gen(function* () {
      const web = yield* HttpServerRequest.toWeb(request)
      if (path === "/mcp") return yield* handleMcp(web)
      const clientIp = yield* ClientIpTag
      return yield* legacy.forward(web, yield* clientIp.key)
    }).pipe(Effect.map(HttpServerResponse.fromWeb), Effect.catchAll((error) => Effect.succeed(
      HttpServerResponse.unsafeJson({ reason: "reason" in error ? error.reason : "service unavailable" }, {
        status: "_tag" in error && error._tag === "Unauthorized" ? 401 : "_tag" in error && error._tag === "PayloadTooLarge" ? 413 : 503
      })
    )))
    const response = yield* forwarded
    yield* Ref.update(requests, (counts) => new Map(counts).set(response.status, (counts.get(response.status) ?? 0) + 1))
    return HttpServerResponse.setHeader(response, "cache-control", "private, no-store")
  })
}), { withContext: true })
