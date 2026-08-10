/**
 * The API, declared once.
 *
 * This single file replaces what FastAPI spread over ten `app/routers/*.py`
 * modules plus their decorators. From it, `@effect/platform` derives:
 *
 *   - the server's handler signatures (a missing or mistyped handler is a
 *     compile error, not a 404 someone finds in production),
 *   - a fully typed client, and
 *   - the OpenAPI document served at `/docs`.
 *
 * That is the concrete reason "Effect needs no separate web framework": the
 * framework's job — routing, validation, docs — is done by the type.
 *
 * Route paths and status codes match `app/main.py` exactly, so the two
 * services are interchangeable behind the same ingress.
 */
import { HttpApi, HttpApiEndpoint, HttpApiGroup, HttpApiSchema } from "@effect/platform"
import { Schema } from "effect"
import {
  ExploreResponse,
  PlanResponse,
  WalkResponse
} from "../domain/AgentContracts.js"
import { WriteBatch, WriteReceipt } from "../domain/WriteIntent.js"
import {
  AgentFeed,
  ConsensusRecord,
  CypherRequestWire,
  CypherResponse,
  DomainRecord,
  FeedbackInboxResponse,
  FeedbackRequestWire,
  FeedbackResponse,
  FeedbackTriageRequest,
  FeedbackTriageResponse,
  FindingRecord,
  HealthResponse,
  LessonRecord,
  NodeNeighbors,
  PaperRecord,
  ReadyResponse,
  RecentItem,
  ResearchSummary,
  RootResponse,
  SkillRecord,
  StatsContract
} from "../domain/Contracts.js"
import { ValidationFailed } from "../domain/Validation.js"
import {
  BadRequest,
  Conflict,
  Forbidden,
  KgQueryFailed,
  NotFound,
  NotReady,
  RateLimited,
  Unauthorized,
  Unavailable
} from "../domain/Errors.js"

// --------------------------------------------------------------------------
// shared query-parameter schemas
// --------------------------------------------------------------------------

/**
 * Query params arrive as strings and are validated INSIDE the handler.
 *
 * Declaring `Schema.NumberFromString.pipe(between(...))` here would make an
 * out-of-range value a fixed-400 `HttpApiDecodeError`; FastAPI answers 422
 * (`app/routers/research.py:36` is `Query(20, ge=1, le=100)`), and twelve
 * Python tests assert it. So the wire takes strings and `PageParams` in
 * `Contracts` carries the real bounds.
 */
const RawText = Schema.optional(Schema.String)

/**
 * `?limit=` — REJECTS out of range, it does not clamp.
 *
 * `app/routers/research.py:36` is `Query(20, ge=1, le=100)`, so FastAPI answers
 * 422 for `?limit=99999`. An earlier version of this file used `Schema.clamp`,
 * which silently returned 100 rows instead — a client asking for something
 * impossible got a plausible-looking answer. The KG layer still clamps
 * defensively (`app/kg.py:229-231`); that is a second line of defence, not the
 * contract.
 */
const Limit = (fallback: number, max: number) =>
  Schema.optionalWith(
    Schema.NumberFromString.pipe(Schema.int(), Schema.between(1, max)),
    { default: () => fallback }
  )




/**
 * Both credential headers.
 *
 * `x-api-key` is the one the Python service reads (`app/routers/kg_proxy.py:74`,
 * `app/routers/feedback.py:85`). `authorization` is accepted additively so the
 * agent surface and any newer client can use a bearer token. Declaring both
 * keeps them in the generated OpenAPI instead of being folk knowledge.
 */
const CredentialHeaders = Schema.Struct({
  "x-api-key": Schema.optional(Schema.String),
  authorization: Schema.optional(Schema.String)
})

// --------------------------------------------------------------------------
// meta — /health, /ready, /
// --------------------------------------------------------------------------

export const MetaGroup = HttpApiGroup.make("meta")
  .add(HttpApiEndpoint.get("health", "/health").addSuccess(HealthResponse))
  .add(HttpApiEndpoint.get("ready", "/ready").addSuccess(ReadyResponse).addError(NotReady))
  .add(HttpApiEndpoint.get("root", "/").addSuccess(RootResponse))

// --------------------------------------------------------------------------
// curated KG surfaces — /api/stats, /api/domains, /api/skills
// --------------------------------------------------------------------------

export const KgSurfaceGroup = HttpApiGroup.make("kgSurface")
  .add(HttpApiEndpoint.get("stats", "/stats").addSuccess(StatsContract))
  .add(HttpApiEndpoint.get("domains", "/domains").addSuccess(Schema.Array(DomainRecord)))
  .add(HttpApiEndpoint.get("skills", "/skills").addSuccess(Schema.Array(SkillRecord)))
  .prefix("/api")

// --------------------------------------------------------------------------
// /api/research/* — the living research body
// --------------------------------------------------------------------------

export const ResearchGroup = HttpApiGroup.make("research")
  .add(HttpApiEndpoint.get("summary", "/summary").addSuccess(ResearchSummary))
  .add(
    HttpApiEndpoint.get("findings", "/findings")
      .setUrlParams(Schema.Struct({ limit: RawText, offset: RawText, cycle: RawText }))
      .addSuccess(Schema.Array(FindingRecord))
      .addError(ValidationFailed)
  )
  .add(
    HttpApiEndpoint.get("lessons", "/lessons")
      .setUrlParams(Schema.Struct({ limit: RawText, offset: RawText }))
      .addSuccess(Schema.Array(LessonRecord))
      .addError(ValidationFailed)
  )
  .add(
    HttpApiEndpoint.get("papers", "/papers")
      .setUrlParams(Schema.Struct({ limit: RawText, offset: RawText, domain: RawText }))
      .addSuccess(Schema.Array(PaperRecord))
      .addError(ValidationFailed)
  )
  .add(
    HttpApiEndpoint.get("consensus", "/consensus")
      .setUrlParams(Schema.Struct({ limit: RawText, offset: RawText }))
      .addSuccess(Schema.Array(ConsensusRecord))
      .addError(ValidationFailed)
  )
  .add(
    HttpApiEndpoint.get("recent", "/recent")
      .setUrlParams(Schema.Struct({ limit: RawText }))
      .addSuccess(Schema.Array(RecentItem))
      .addError(ValidationFailed)
  )
  .add(
    HttpApiEndpoint.get("neighbors", "/neighbors")
      .setUrlParams(Schema.Struct({ name: RawText, limit: RawText }))
      .addSuccess(NodeNeighbors)
      .addError(ValidationFailed)
  )
  /** The machine-readable surface. Deliberately one request: an agent should
   *  not have to fan out across six endpoints to orient itself. */
  .add(HttpApiEndpoint.get("agent", "/agent").addSuccess(AgentFeed))
  .prefix("/api/research")

// --------------------------------------------------------------------------
// /api/feedback + /internal/feedback
// --------------------------------------------------------------------------

export const FeedbackGroup = HttpApiGroup.make("feedback")
  .add(
    HttpApiEndpoint.post("submit", "/feedback")
      .setPayload(FeedbackRequestWire)
      .addSuccess(FeedbackResponse)
      .addError(ValidationFailed)
      .addError(RateLimited)
      .addError(Unavailable)
      .addError(Forbidden)
  )
  .prefix("/api")

/** Operator plane. Disabled entirely when `feedbackAdminKey` is empty. */
export const FeedbackInternalGroup = HttpApiGroup.make("feedbackInternal")
  .add(
    HttpApiEndpoint.get("inbox", "/feedback")
      .setUrlParams(Schema.Struct({ limit: RawText }))
      .setHeaders(CredentialHeaders)
      .addSuccess(FeedbackInboxResponse)
      .addError(ValidationFailed)
      .addError(Unauthorized)
      .addError(Unavailable)
  )
  .add(
    HttpApiEndpoint.patch("triage", "/feedback/:recordId")
      .setPath(Schema.Struct({ recordId: Schema.String }))
      .setPayload(FeedbackTriageRequest)
      .setHeaders(CredentialHeaders)
      .addSuccess(FeedbackTriageResponse)
      .addError(Unauthorized)
      .addError(NotFound)
      .addError(Conflict)
      .addError(Unavailable)
  )
  .add(
    HttpApiEndpoint.del("discard", "/feedback/:recordId")
      .setPath(Schema.Struct({ recordId: Schema.String }))
      .setHeaders(CredentialHeaders)
      .addSuccess(HttpApiSchema.NoContent)
      .addError(Unauthorized)
      .addError(NotFound)
      .addError(Unavailable)
  )
  .prefix("/internal")

// --------------------------------------------------------------------------
// /api/kg/{read,write} — the raw Cypher proxy
// --------------------------------------------------------------------------

/**
 * Community Neo4j has no RBAC, so read/write separation is enforced *here*:
 * the read key runs the query in a READ transaction (the server itself
 * rejects writes) and the write key runs it in a WRITE transaction. An empty
 * key disables that endpoint with 503, so the proxy is opt-in and stays off
 * in CI and offline.
 */
export const KgProxyGroup = HttpApiGroup.make("kgProxy")
  .add(
    HttpApiEndpoint.post("read", "/read")
      .setPayload(CypherRequestWire)
      .setHeaders(CredentialHeaders)
      .addSuccess(CypherResponse)
      .addError(ValidationFailed)
      .addError(Unauthorized)
      .addError(Unavailable)
      .addError(KgQueryFailed)
  )
  .add(
    HttpApiEndpoint.post("write", "/write")
      .setPayload(CypherRequestWire)
      .setHeaders(CredentialHeaders)
      .addSuccess(CypherResponse)
      .addError(ValidationFailed)
      .addError(Unauthorized)
      .addError(Unavailable)
      .addError(KgQueryFailed)
  )
  .prefix("/api/kg")

// --------------------------------------------------------------------------
// /api/agent/* — the graph surface
// --------------------------------------------------------------------------

/**
 * The KG as an agent's move set.
 *
 * `/api/research/neighbors` already said "any node is a doorway", but only a
 * human could walk through it. These three endpoints are that doorway for an
 * agent: traverse, read the traversal back as a DAG, and run it to
 * convergence with a bounded scheduler.
 */
const WalkParams = Schema.Struct({
  seed: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(512)),
  maxNodes: Limit(40, 200),
  maxDepth: Limit(3, 6),
  branching: Limit(8, 50)
})

export const AgentGroup = HttpApiGroup.make("agent")
  .add(
    HttpApiEndpoint.get("walk", "/walk").setUrlParams(WalkParams).addSuccess(WalkResponse)
  )
  .add(
    HttpApiEndpoint.get("plan", "/plan").setUrlParams(WalkParams).addSuccess(PlanResponse)
  )
  .add(
    HttpApiEndpoint.get("explore", "/explore")
      .setUrlParams(
        Schema.Struct({
          seed: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(512)),
          maxNodes: Limit(40, 200),
          maxRounds: Limit(4, 12),
          concurrency: Limit(4, 16)
        })
      )
      .addSuccess(ExploreResponse)
  )
  /**
   * The write path — the other half of the feedback loop.
   *
   * Gated on the KG **write** key, not the read key: reading canon and
   * amending it are different privileges. `dryRun` defaults to true in the
   * payload schema, so the dangerous call is the one you have to spell out.
   */
  .add(
    HttpApiEndpoint.post("record", "/record")
      .setPayload(WriteBatch)
      .setHeaders(CredentialHeaders)
      .addSuccess(WriteReceipt)
      .addError(Unauthorized)
      .addError(Unavailable)
      .addError(KgQueryFailed)
  )
  .prefix("/api/agent")

// --------------------------------------------------------------------------
// the whole API
// --------------------------------------------------------------------------

export const Api = HttpApi.make("metahumotonic-web-back")
  .add(MetaGroup)
  .add(KgSurfaceGroup)
  .add(ResearchGroup)
  .add(FeedbackGroup)
  .add(FeedbackInternalGroup)
  .add(KgProxyGroup)
  .add(AgentGroup)
  .addError(BadRequest)
  .addError(Conflict)

export type Api = typeof Api
