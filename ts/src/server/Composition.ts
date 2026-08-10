/**
 * The composition graph — defined ONCE, consumed by both planes.
 *
 * SPEC §4-3:
 *
 * > 프로덕션 진입점과 하네스는 반드시 동일한 composition graph 를 소비한다.
 * > 각자 배선을 따로 가지면 "하네스에서는 호출되는데 실제 진입점에서는 안 불리는"
 * > 결함이 구조적으로 생긴다.
 *
 * That defect happened here on 2026-08-10, exactly as written. The test file
 * assembled its own app — it shared two layers with production out of eleven —
 * so when `OperatorPlaneNoStore` was provided to `serve` instead of `api`,
 * every prefixed route 404'd in production while 117 tests stayed green.
 *
 * The fix is not another checker. It is this file: one `apiLayer`, one
 * `portsLayer`, and the two planes differ only in which *implementations* they
 * pass in. Adapters and observation differ; the wiring does not.
 *
 *          portsLayer(overrides)  ──►  apiLayer  ──┬─► serveLayer   (production)
 *                                                   └─► toWebHandler (harness)
 */
import { HttpApiBuilder, HttpApiScalar, HttpMiddleware, HttpServer } from "@effect/platform"
import { ConfigError, Effect, Layer } from "effect"
import { Api } from "../api/Api.js"
import { AppConfigLive, AppConfigTag, validateWikiConfiguration, type AppConfig } from "../Config.js"
import { ClientIpLive, ClientIpTag } from "../ports/ClientIp.js"
import { FeedbackStoreMemory, FeedbackStoreTag } from "../ports/FeedbackStore.js"
import { IdsLive, IdsTag } from "../ports/Ids.js"
import { KgPortLive, KgPortTag } from "../ports/KgPort.js"
import { KgWritePortLive, KgWritePortTag } from "../ports/KgWritePort.js"
import {
  FeedbackLimiter,
  layerInProcess,
  WikiMutationLimiter,
  WikiReadLimiter,
  WikiSessionLimiter
} from "../ports/RateLimiter.js"
import { SchemaGuardLive, SchemaGuardTag } from "../ports/SchemaGuard.js"
import { HandlersLive } from "./Handlers.js"
import { OperatorPlaneNoStore } from "./Middleware.js"

/**
 * Startup validation as a layer.
 *
 * A misconfigured wiki plane must stop the process, not degrade quietly. As a
 * layer this happens once, before the socket is bound — the guarantee FastAPI's
 * `lifespan` hook gives, without a hook.
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

/**
 * What a caller is allowed to substitute.
 *
 * Deliberately only *port implementations* — there is no knob here for
 * changing which handlers are mounted, which middleware runs, or in what
 * order. Those are the wiring, and the wiring is not substitutable. Adding a
 * field to this interface is the reviewable act of widening what may differ
 * between production and the harness.
 */
export interface PortOverrides {
  readonly config?: Layer.Layer<AppConfigTag, ConfigError.ConfigError>
  readonly kg?: Layer.Layer<KgPortTag>
  readonly kgWrite?: Layer.Layer<KgWritePortTag, never, SchemaGuardTag>
  readonly schemaGuard?: Layer.Layer<SchemaGuardTag, never, KgPortTag>
  readonly clientIp?: Layer.Layer<ClientIpTag>
  readonly feedbackStore?: Layer.Layer<FeedbackStoreTag>
  readonly ids?: Layer.Layer<IdsTag>
  /** All four tags, or none. Supplying a subset would leave the rest bound to
   *  the production layer — a wiring difference dressed up as a substitution. */
  readonly limiters?: Layer.Layer<
    FeedbackLimiter | WikiSessionLimiter | WikiMutationLimiter | WikiReadLimiter
  >
  /** Skip `ConfigGuard`. Only a harness that deliberately feeds an invalid
   *  config to test the guard's *absence* should do this. */
  readonly skipConfigGuard?: boolean
}

/**
 * Every port, assembled the same way regardless of which implementations win.
 *
 * The dependency shape is the interesting part and it is identical in both
 * planes: the write path needs the schema guard, the schema guard needs the
 * read port, and everything needs config.
 */
export const portsLayer = (overrides: PortOverrides = {}) => {
  const config = overrides.config ?? AppConfigLive
  const kg = overrides.kg ?? KgPortLive
  const schemaGuard = overrides.schemaGuard ?? SchemaGuardLive
  const kgWrite = overrides.kgWrite ?? KgWritePortLive

  const writeStack = kgWrite.pipe(
    Layer.provideMerge(schemaGuard),
    Layer.provide(kg)
  )

  const base = Layer.mergeAll(
    kg,
    writeStack,
    overrides.clientIp ?? ClientIpLive,
    overrides.feedbackStore ?? FeedbackStoreMemory,
    overrides.ids ?? IdsLive,
    overrides.limiters ?? LimitersLive
  )

  const withConfig = base.pipe(Layer.provideMerge(config))
  return overrides.skipConfigGuard === true
    ? withConfig
    : withConfig.pipe(Layer.provideMerge(ConfigGuard.pipe(Layer.provide(config))))
}

/**
 * The API plane: endpoints + handlers + API-level middleware.
 *
 * `OperatorPlaneNoStore` goes HERE, on `HttpApiBuilder.api`. Providing it to
 * `HttpApiBuilder.serve` instead type-checks, starts cleanly, and silently
 * drops every prefixed route. Because this function is the single definition,
 * that mistake can now only be made in one place — and the harness makes it
 * too, so a test would catch it.
 */
export const apiLayer = HttpApiBuilder.api(Api).pipe(
  Layer.provide(HandlersLive),
  Layer.provide(OperatorPlaneNoStore)
)

/**
 * CORS.
 *
 * Preserving the Python's note: logging is innermost and CORS outermost, so a
 * synthesized 500 flows back out *through* CORS and carries ACAO headers —
 * a cross-origin client can actually read the error body.
 */
export const corsLayer = (
  config: Layer.Layer<AppConfigTag, ConfigError.ConfigError> = AppConfigLive
) =>
  Layer.unwrapEffect(
    Effect.gen(function* () {
      const cfg = yield* AppConfigTag
      return HttpApiBuilder.middlewareCors({
        allowedOrigins: cfg.corsOrigins,
        allowedMethods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allowedHeaders: ["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
        credentials: true
      })
    })
  ).pipe(Layer.provide(config))

/** The production plane: the composition above, served over a socket. */
export const serveLayer = (overrides: PortOverrides = {}) =>
  HttpApiBuilder.serve(HttpMiddleware.logger).pipe(
    Layer.provide(HttpApiScalar.layer({ path: "/docs" })),
    Layer.provide(corsLayer(overrides.config ?? AppConfigLive)),
    Layer.provide(apiLayer),
    HttpServer.withLogAddress,
    Layer.provide(portsLayer(overrides))
  )

/**
 * The harness plane: the SAME composition, reached through a web handler
 * instead of a socket.
 *
 * A test may substitute ports through `overrides`. It may not re-declare the
 * wiring — `apiLayer` is shared, not rebuilt.
 */
export const webHandlerLayer = (overrides: PortOverrides = {}) =>
  Layer.mergeAll(
    apiLayer.pipe(Layer.provide(portsLayer(overrides))),
    HttpServer.layerContext
  )

/** Convenience for a harness that also wants a fixed config object. */
export const configOf = (cfg: AppConfig): Layer.Layer<AppConfigTag> =>
  Layer.succeed(AppConfigTag, cfg)
