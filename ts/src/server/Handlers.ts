/**
 * Handler implementations.
 *
 * Every handler here is a *total function from request to Effect*: it declares
 * what it needs in `R`, what it can fail with in `E`, and returns a value the
 * API's schema already validated. There is no `request` object to reach into,
 * no module-level singleton to mutate, and no `try/except` — the failure modes
 * are the ones listed in `Api.ts`, checked by the compiler.
 */
import { HttpApiBuilder } from "@effect/platform"
import { Effect, Layer, Redacted } from "effect"
import { Api } from "../api/Api.js"
import { AppConfigTag } from "../Config.js"
import {
  AgentFeed,
  CypherResponse,
  FeedbackInboxResponse,
  FeedbackResponse,
  FeedbackTriageResponse,
  HealthResponse,
  ReadyComponent,
  ReadyResponse,
  RootResponse
} from "../domain/Contracts.js"
import { BadRequest, Forbidden, KgQueryFailed, Unavailable } from "../domain/Errors.js"
import { authorize } from "../ports/Auth.js"
import { FeedbackStoreTag } from "../ports/FeedbackStore.js"
import { IdsTag } from "../ports/Ids.js"
import { KgPortTag } from "../ports/KgPort.js"
import { enforce, FeedbackLimiter } from "../ports/RateLimiter.js"
import { AgentLive } from "./AgentHandlers.js"

// --------------------------------------------------------------------------
// meta
// --------------------------------------------------------------------------

export const MetaLive = HttpApiBuilder.group(Api, "meta", (handlers) =>
  handlers
    .handle("health", () =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        return new HealthResponse({ status: "ok", version: cfg.version })
      })
    )
    .handle("ready", () =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        const kg = yield* KgPortTag
        const feedback = yield* FeedbackStoreTag

        const kgMode = yield* kg.mode
        const kgReachable = kgMode === "snapshot" ? true : yield* kg.ping

        const components = [
          new ReadyComponent({
            name: "kg",
            ready: kgReachable,
            detail: kgMode === "snapshot" ? "snapshot fallback (MHB_NEO4J_LIVE off)" : "live bolt"
          }),
          new ReadyComponent({
            name: "feedback-store",
            ready: true,
            detail: feedback.durable ? "durable" : "in-memory (non-durable)"
          }),
          new ReadyComponent({
            name: "wiki",
            ready: !cfg.wikiPublicWrites,
            detail: cfg.wikiPublicWrites
              ? "public writes enabled — requires PostgreSQL + Redis (not wired in this port)"
              : "read-only (writes disabled)"
          })
        ]

        return new ReadyResponse({
          ready: components.every((c) => c.ready),
          version: cfg.version,
          components
        })
      })
    )
    .handle("root", () =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        return new RootResponse({
          service: "metahumotonic-web-back",
          version: cfg.version,
          docs: "/docs",
          runtime: "effect-ts"
        })
      })
    )
)

// --------------------------------------------------------------------------
// curated KG surfaces
// --------------------------------------------------------------------------

export const KgSurfaceLive = HttpApiBuilder.group(Api, "kgSurface", (handlers) =>
  handlers
    .handle("stats", () => Effect.flatMap(KgPortTag, (kg) => kg.stats))
    .handle("domains", () => Effect.flatMap(KgPortTag, (kg) => kg.domains))
    .handle("skills", () => Effect.flatMap(KgPortTag, (kg) => kg.skills))
)

// --------------------------------------------------------------------------
// research
// --------------------------------------------------------------------------

const AGENT_DOCTRINE_URL = "https://metahumotonic.com/llms.txt"
const AGENT_CANONICAL_SOURCE = "https://metahumotonic.com"

export const ResearchLive = HttpApiBuilder.group(Api, "research", (handlers) =>
  handlers
    .handle("summary", () => Effect.flatMap(KgPortTag, (kg) => kg.researchSummary))
    .handle("findings", ({ urlParams }) =>
      Effect.flatMap(KgPortTag, (kg) =>
        kg.findings({
          limit: urlParams.limit,
          offset: urlParams.offset,
          cycle: urlParams.cycle
        })
      )
    )
    .handle("lessons", ({ urlParams }) =>
      Effect.flatMap(KgPortTag, (kg) =>
        kg.lessons({ limit: urlParams.limit, offset: urlParams.offset })
      )
    )
    .handle("papers", ({ urlParams }) =>
      Effect.flatMap(KgPortTag, (kg) =>
        kg.papers({
          limit: urlParams.limit,
          offset: urlParams.offset,
          domain: urlParams.domain
        })
      )
    )
    .handle("consensus", ({ urlParams }) =>
      Effect.flatMap(KgPortTag, (kg) =>
        kg.consensus({ limit: urlParams.limit, offset: urlParams.offset })
      )
    )
    .handle("recent", ({ urlParams }) =>
      Effect.flatMap(KgPortTag, (kg) => kg.recent({ limit: urlParams.limit }))
    )
    .handle("neighbors", ({ urlParams }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        const kg = yield* KgPortTag
        if (urlParams.name.length > 512) {
          return yield* Effect.fail(new BadRequest({ reason: "name is too long" }))
        }
        void cfg
        return yield* kg.neighbors({ name: urlParams.name, limit: urlParams.limit })
      })
    )
    /**
     * The agent surface. One request, three reads, run concurrently — the
     * `R` channel guarantees they all hit the same `KgPort`, and `Effect.all`
     * bounds the fan-out instead of firing an unbounded `Promise.all`.
     */
    .handle("agent", () =>
      Effect.gen(function* () {
        const kg = yield* KgPortTag
        const [summary, findings, lessons] = yield* Effect.all(
          [
            kg.researchSummary,
            kg.findings({ limit: 10, offset: 0, cycle: "" }),
            kg.lessons({ limit: 10, offset: 0 })
          ],
          { concurrency: 3 }
        )

        return new AgentFeed({
          doctrine_url: AGENT_DOCTRINE_URL,
          canonical_source: AGENT_CANONICAL_SOURCE,
          generated_hint:
            summary.source === "live"
              ? "counts are a live KG read"
              : "counts are the offline snapshot — do not cite as current",
          summary,
          recent_findings: findings,
          recent_lessons: lessons,
          how_to_cite:
            "Cite the node `name` plus this endpoint and the retrieval date. " +
            "`source: snapshot` means the magnitudes are stale — say so."
        })
      })
    )
)

// --------------------------------------------------------------------------
// feedback
// --------------------------------------------------------------------------

export const FeedbackLive = HttpApiBuilder.group(Api, "feedback", (handlers) =>
  handlers.handle("submit", ({ payload }) =>
    Effect.gen(function* () {
      const cfg = yield* AppConfigTag
      const store = yield* FeedbackStoreTag
      const ids = yield* IdsTag
      const limiter = yield* FeedbackLimiter

      // The honeypot is handled silently: a bot gets the same shape a human
      // gets, so it learns nothing from the response.
      if (payload.honeypot !== "") {
        return new FeedbackResponse({ ok: true, id: null, status: "accepted" })
      }

      // Turnstile is opt-in. When a secret is configured but no token was
      // presented we refuse rather than fail open — unless explicitly told to.
      if (Redacted.value(cfg.turnstileSecret) !== "" && payload.turnstile_token === "") {
        if (!cfg.turnstileFailOpen) {
          return yield* Effect.fail(
            new Forbidden({ reason: "bot verification token is required" })
          )
        }
      }

      // Keyed on the submission itself. The Python keys on client IP, which
      // needs `trust_proxy` to be meaningful; wiring the forwarded-IP chain is
      // left to the ingress layer rather than guessed at here.
      yield* enforce(limiter, `feedback:${payload.type}:${payload.subject}`)

      if (cfg.feedbackRequireDurable && !store.durable) {
        return yield* Effect.fail(
          new Unavailable({
            reason: "feedback storage is not durable and MHB_FEEDBACK_REQUIRE_DURABLE is set"
          })
        )
      }

      const id = yield* ids.newId
      const now = yield* ids.nowIso
      const saved = yield* store.save(payload, { id, now })

      return new FeedbackResponse({ ok: true, id: saved.id, status: saved.status })
    })
  )
)

export const FeedbackInternalLive = HttpApiBuilder.group(Api, "feedbackInternal", (handlers) =>
  handlers
    .handle("inbox", ({ headers, urlParams }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        yield* authorize({
          presented: headers.authorization,
          accepted: [cfg.feedbackAdminKey],
          surface: "operator feedback inbox"
        })
        const store = yield* FeedbackStoreTag
        const page = yield* store.list({ limit: urlParams.limit })
        return new FeedbackInboxResponse({ items: page.items, count: page.count })
      })
    )
    .handle("triage", ({ headers, path, payload }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        yield* authorize({
          presented: headers.authorization,
          accepted: [cfg.feedbackAdminKey],
          surface: "operator feedback inbox"
        })
        const store = yield* FeedbackStoreTag
        const ids = yield* IdsTag
        const now = yield* ids.nowIso
        const item = yield* store.triage(path.recordId, {
          status: payload.status,
          operatorNote: payload.operator_note,
          now
        })
        return new FeedbackTriageResponse({ ok: true, item })
      })
    )
    .handle("discard", ({ headers, path }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        yield* authorize({
          presented: headers.authorization,
          accepted: [cfg.feedbackAdminKey],
          surface: "operator feedback inbox"
        })
        const store = yield* FeedbackStoreTag
        const ids = yield* IdsTag
        const now = yield* ids.nowIso
        // Discard = mark spam. The record is retained for audit, exactly as the
        // Python DELETE does; the TTL index is what eventually removes it.
        yield* store.triage(path.recordId, { status: "spam", operatorNote: "", now })
      })
    )
)

// --------------------------------------------------------------------------
// KG Cypher proxy
// --------------------------------------------------------------------------

/**
 * Rows are capped server-side so one client cannot drain a 90k-node label in
 * a single request. `truncated` tells the caller it happened rather than
 * silently handing back a short page.
 */
const capRows = (
  rows: ReadonlyArray<Record<string, unknown>>,
  max: number,
  mode: "read" | "write"
): CypherResponse => {
  const truncated = rows.length > max
  const kept = truncated ? rows.slice(0, max) : rows
  return new CypherResponse({ rows: kept, count: kept.length, mode, truncated })
}

export const KgProxyLive = HttpApiBuilder.group(Api, "kgProxy", (handlers) =>
  handlers
    .handle("read", ({ headers, payload }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        // The write key is a superset: it is also accepted on /api/kg/read.
        yield* authorize({
          presented: headers.authorization,
          accepted: [cfg.kgReadKey, cfg.kgWriteKey],
          surface: "KG read proxy"
        })
        const kg = yield* KgPortTag
        const rows = yield* kg.run(payload.query, payload.params, "read")
        return capRows(rows, cfg.kgProxyMaxRows, "read")
      })
    )
    .handle("write", ({ headers, payload }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        yield* authorize({
          presented: headers.authorization,
          accepted: [cfg.kgWriteKey],
          surface: "KG write proxy"
        })
        const kg = yield* KgPortTag
        const rows = yield* kg.run(payload.query, payload.params, "write")
        return capRows(rows, cfg.kgProxyMaxRows, "write")
      })
    )
)

/** Every group, composed. A missing group is a compile error at `HttpApiBuilder.api`. */
export const HandlersLive = Layer.mergeAll(
  MetaLive,
  KgSurfaceLive,
  ResearchLive,
  FeedbackLive,
  FeedbackInternalLive,
  KgProxyLive,
  AgentLive
)

// re-exported so `Layers.ts` can name the error types it must not leak
export type { KgQueryFailed }
