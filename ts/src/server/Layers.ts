/**
 * The composition root — the one place that decides *which* implementation
 * each port gets.
 *
 * `app/main.py` did this at import time with module-level singletons, which is
 * why its tests have to monkey-patch. Here the wiring is a value: `AppLive` for
 * production, `AppTest` for tests, and nothing in between needs to change.
 */
import { HttpApiBuilder, HttpApiScalar, HttpMiddleware, HttpServer } from "@effect/platform"
import { Effect, Layer } from "effect"
import { Api } from "../api/Api.js"
import { AppConfigLive, AppConfigTag, validateWikiConfiguration } from "../Config.js"
import { FeedbackStoreMemory } from "../ports/FeedbackStore.js"
import { IdsLive } from "../ports/Ids.js"
import { KgPortLive } from "../ports/KgPort.js"
import { KgWritePortLive } from "../ports/KgWritePort.js"
import { SchemaGuardLive } from "../ports/SchemaGuard.js"
import {
  FeedbackLimiter,
  layerInProcess,
  WikiMutationLimiter,
  WikiReadLimiter,
  WikiSessionLimiter
} from "../ports/RateLimiter.js"
import { HandlersLive } from "./Handlers.js"

/**
 * Startup validation as a layer.
 *
 * A misconfigured wiki plane must stop the process, not degrade quietly. As a
 * layer this happens once, before the socket is bound — the same guarantee the
 * FastAPI `lifespan` hook gives, but without a hook.
 */
export const ConfigGuard = Layer.effectDiscard(
  Effect.gen(function* () {
    const cfg = yield* AppConfigTag
    yield* validateWikiConfiguration(cfg).pipe(
      Effect.mapError((e) => new Error(`invalid configuration: ${e.reason}`)),
      Effect.orDie
    )
  })
)

/** The four independent limiters, each with the policy from config. */
export const LimitersLive = Layer.unwrapEffect(
  Effect.gen(function* () {
    const cfg = yield* AppConfigTag
    return Layer.mergeAll(
      layerInProcess(FeedbackLimiter, {
        maxEvents: cfg.feedbackMaxPerWindow,
        windowSeconds: cfg.feedbackWindowSeconds,
        failClosed: false
      }),
      layerInProcess(WikiSessionLimiter, {
        maxEvents: cfg.wikiSessionMaxPerWindow,
        windowSeconds: cfg.wikiSessionWindowSeconds,
        failClosed: cfg.wikiPublicWrites && cfg.wikiRequireRedis
      }),
      layerInProcess(WikiMutationLimiter, {
        maxEvents: cfg.wikiMutationMaxPerWindow,
        windowSeconds: cfg.wikiMutationWindowSeconds,
        failClosed: cfg.wikiPublicWrites && cfg.wikiRequireRedis
      }),
      layerInProcess(WikiReadLimiter, {
        maxEvents: cfg.wikiReadMaxPerWindow,
        windowSeconds: cfg.wikiReadWindowSeconds,
        failClosed: cfg.wikiPublicWrites && cfg.wikiRequireRedis
      })
    )
  })
)

/** Ports: config first, then everything that depends on it. */
/** The write path needs the read port (for the registry) and the guard. */
const WriteStack = KgWritePortLive.pipe(
  Layer.provideMerge(SchemaGuardLive),
  Layer.provide(KgPortLive)
)

export const PortsLive = Layer.mergeAll(
  KgPortLive,
  WriteStack,
  FeedbackStoreMemory,
  IdsLive,
  LimitersLive
).pipe(Layer.provideMerge(AppConfigLive), Layer.provideMerge(ConfigGuard.pipe(Layer.provide(AppConfigLive))))

/**
 * CORS.
 *
 * The Python comment is worth preserving: logging is added first so it ends up
 * innermost, and CORS last so it is outermost — a synthesized 500 therefore
 * flows back out *through* CORS and carries ACAO headers, letting a
 * cross-origin client actually read the error body.
 */
export const CorsLive = Layer.unwrapEffect(
  Effect.gen(function* () {
    const cfg = yield* AppConfigTag
    return HttpApiBuilder.middlewareCors({
      allowedOrigins: cfg.corsOrigins,
      allowedMethods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
      allowedHeaders: ["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
      credentials: true
    })
  })
).pipe(Layer.provide(AppConfigLive))

/** The HTTP app: API + handlers + CORS + the derived OpenAPI page at /docs. */
export const ApiLive = HttpApiBuilder.api(Api).pipe(Layer.provide(HandlersLive))

export const HttpLive = HttpApiBuilder.serve(HttpMiddleware.logger).pipe(
  Layer.provide(HttpApiScalar.layer({ path: "/docs" })),
  Layer.provide(CorsLive),
  Layer.provide(ApiLive),
  HttpServer.withLogAddress,
  Layer.provide(PortsLive)
)
