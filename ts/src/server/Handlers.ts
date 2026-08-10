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
  CypherRequest,
  FeedbackRequest,
  FindingsParamsStrict,
  InboxParamsStrict,
  ListParamsStrict,
  NeighborsParamsStrict,
  PapersParamsStrict,
  RecentParamsStrict,
  CypherResponse,
  FeedbackInboxResponse,
  FeedbackResponse,
  FeedbackTriageResponse,
  HealthResponse,
  ReadyResponse,
  RootResponse
} from "../domain/Contracts.js"
import {
  Forbidden,
  KgQueryFailed,
  NotFound,
  NotReady,
  Unavailable
} from "../domain/Errors.js"
import { validate } from "../domain/Validation.js"
import { authorize } from "../ports/Auth.js"
import { FeedbackStoreTag } from "../ports/FeedbackStore.js"
import { IdsTag } from "../ports/Ids.js"
import { KgPortTag } from "../ports/KgPort.js"
import { ClientIpTag } from "../ports/ClientIp.js"
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
    /**
     * Degraded-tolerant readiness, matching `app/routers/meta.py` field for
     * field — `ops/check-web-back-live.sh:296-302` reads five of these keys.
     *
     * The wiki plane is the only thing that can make the service NOT ready;
     * a dead KG degrades, it does not fail.
     */
    .handle("ready", () =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        const kg = yield* KgPortTag

        const kgLive = cfg.neo4jLive ? yield* kg.ping : false

        const wikiRequired = cfg.wikiPublicWrites
        // The wiki plane is not implemented in this port. Reporting it as live
        // would be a lie the live-checker would then certify, so when writes
        // are required this service declares itself not ready.
        const wikiStoreLive = false
        const wikiRateLimitLive = !(wikiRequired && cfg.wikiRequireRedis)
        const wikiLive = wikiStoreLive && wikiRateLimitLive
        const degraded = (cfg.neo4jLive && !kgLive) || (wikiRequired && !wikiLive)

        const body = {
          kg_live: kgLive,
          wiki_required: wikiRequired,
          wiki_live: wikiLive,
          wiki_store_live: wikiStoreLive,
          wiki_rate_limit_live: wikiRateLimitLive,
          degraded
        }

        if (wikiRequired && !wikiLive) {
          return yield* Effect.fail(new NotReady({ status: "not_ready", ...body }))
        }
        return new ReadyResponse({ status: "ready", ...body })
      })
    )
    .handle("root", () =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        return new RootResponse({
          service: "metahumotonic-web-back",
          version: cfg.version,
          // same list, same order as app/routers/meta.py:80-90
          endpoints: [
            "/health",
            "/ready",
            "/api/stats",
            "/api/domains",
            "/api/skills",
            "/api/feedback",
            "/api/wiki/v1"
          ],
          runtime: "effect-ts",
          // Which box answered. Cheap, and it removes a whole class of "am I
          // looking at the thing I just deployed?" confusion.
          deployedAt: process.env["MHB_DEPLOY_ENV"] ?? "unset"
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
      Effect.gen(function* () {
        const p = yield* validate(FindingsParamsStrict, urlParams)
        const kg = yield* KgPortTag
        return yield* kg.findings(p)
      })
    )
    .handle("lessons", ({ urlParams }) =>
      Effect.gen(function* () {
        const p = yield* validate(ListParamsStrict, urlParams)
        const kg = yield* KgPortTag
        return yield* kg.lessons(p)
      })
    )
    .handle("papers", ({ urlParams }) =>
      Effect.gen(function* () {
        const p = yield* validate(PapersParamsStrict, urlParams)
        const kg = yield* KgPortTag
        return yield* kg.papers(p)
      })
    )
    .handle("consensus", ({ urlParams }) =>
      Effect.gen(function* () {
        const p = yield* validate(ListParamsStrict, urlParams)
        const kg = yield* KgPortTag
        return yield* kg.consensus(p)
      })
    )
    .handle("recent", ({ urlParams }) =>
      Effect.gen(function* () {
        const p = yield* validate(RecentParamsStrict, urlParams)
        const kg = yield* KgPortTag
        return yield* kg.recent(p)
      })
    )
    .handle("neighbors", ({ urlParams }) =>
      Effect.gen(function* () {
        // `name` is required: tests/test_research_endpoints.py:277 expects 422
        // when it is absent, not a 200 with an empty result.
        const p = yield* validate(NeighborsParamsStrict, urlParams)
        const kg = yield* KgPortTag
        return yield* kg.neighbors(p)
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
  handlers.handle("submit", ({ payload: wire }) =>
    Effect.gen(function* () {
      // The endpoint declares the wire shape so OpenAPI documents the fields;
      // the constraints run here so a violation is 422, matching FastAPI.
      const payload = yield* validate(FeedbackRequest, wire)
      const cfg = yield* AppConfigTag
      const store = yield* FeedbackStoreTag
      const ids = yield* IdsTag
      const limiter = yield* FeedbackLimiter
      const clientIp = yield* ClientIpTag

      // The honeypot is handled silently: a bot gets the same shape a human
      // gets, so it learns nothing from the response.
      if (payload.honeypot !== "") {
        return new FeedbackResponse({ ok: true, id: null, status: "accepted" })
      }

      // Keyed on the CLIENT IP, resolved through the trust-proxy rule.
      // An earlier version keyed on the submission's subject text, which meant
      // varying one field bypassed the limiter entirely.
      const key = yield* clientIp.key
      yield* enforce(limiter, `feedback:${key}`)

      // Turnstile AFTER the rate limit, matching app/routers/feedback.py:56-58:
      // rate-limit first so an invalid-token flood cannot force unbounded
      // verifier calls.
      if (Redacted.value(cfg.turnstileSecret) !== "" && payload.turnstile_token === "") {
        if (!cfg.turnstileFailOpen) {
          return yield* Effect.fail(new Forbidden({ reason: "challenge_failed" }))
        }
      }

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

/**
 * `app/routers/feedback.py:151-158` validates the id BEFORE touching the store
 * and 404s a malformed one, so the endpoint is not an existence oracle.
 */
const isRecordId = (id: string): boolean => /^[0-9a-f]{32}$/.test(id)

export const FeedbackInternalLive = HttpApiBuilder.group(Api, "feedbackInternal", (handlers) =>
  handlers
    .handle("inbox", ({ headers, urlParams }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        yield* authorize({
          presented: [headers["x-api-key"], headers.authorization],
          accepted: [cfg.feedbackAdminKey],
          surface: "operator feedback inbox"
        })
        const p = yield* validate(InboxParamsStrict, urlParams)
        const store = yield* FeedbackStoreTag
        const page = yield* store.list({ limit: p.limit })
        return new FeedbackInboxResponse({ items: page.items, count: page.count })
      })
    )
    .handle("triage", ({ headers, path, payload }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        yield* authorize({
          presented: [headers["x-api-key"], headers.authorization],
          accepted: [cfg.feedbackAdminKey],
          surface: "operator feedback inbox"
        })
        if (!isRecordId(path.recordId)) {
          return yield* Effect.fail(new NotFound({ reason: "feedback not found" }))
        }
        const store = yield* FeedbackStoreTag
        const ids = yield* IdsTag
        const now = yield* ids.nowIso
        // A forbidden transition and an unknown id both surface as 409 — the
        // bounded lifecycle lives in the store, not here.
        const item = yield* store.triage(path.recordId, {
          status: payload.status,
          operatorNote: payload.operator_note,
          now
        })
        return new FeedbackTriageResponse({ ok: true, item })
      })
    )
    /**
     * ERASE, not archive.
     *
     * The first version of this port marked the record `spam` and kept it,
     * including the submitter's contact address. `app/routers/feedback.py:153`
     * is explicit: "Permanently erase an inbox item, including an optional
     * contact address." Retaining it was a silent privacy regression.
     */
    .handle("discard", ({ headers, path }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        yield* authorize({
          presented: [headers["x-api-key"], headers.authorization],
          accepted: [cfg.feedbackAdminKey],
          surface: "operator feedback inbox"
        })
        if (!isRecordId(path.recordId)) {
          return yield* Effect.fail(new NotFound({ reason: "feedback not found" }))
        }
        const store = yield* FeedbackStoreTag
        const erased = yield* store.erase(path.recordId)
        if (!erased) {
          return yield* Effect.fail(new NotFound({ reason: "feedback not found" }))
        }
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
          presented: [headers["x-api-key"], headers.authorization],
          accepted: [cfg.kgReadKey, cfg.kgWriteKey],
          surface: "KG read proxy"
        })
        const q = yield* validate(CypherRequest, payload)
        const kg = yield* KgPortTag
        const rows = yield* kg.run(q.query, q.params, "read")
        return capRows(rows, cfg.kgProxyMaxRows, "read")
      })
    )
    .handle("write", ({ headers, payload }) =>
      Effect.gen(function* () {
        const cfg = yield* AppConfigTag
        yield* authorize({
          presented: [headers["x-api-key"], headers.authorization],
          accepted: [cfg.kgWriteKey],
          surface: "KG write proxy"
        })
        const q = yield* validate(CypherRequest, payload)
        const kg = yield* KgPortTag
        const rows = yield* kg.run(q.query, q.params, "write")
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
