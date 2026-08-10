/**
 * Wire contracts — a one-for-one port of `app/contracts.py`.
 *
 * Pydantic's `field_validator` bodies become Schema *filters* here, which means
 * the same rule that rejects a bad request at runtime also narrows the static
 * type and lands in the generated OpenAPI document. One declaration, three
 * jobs — that is the whole reason for choosing this stack.
 */
import { Schema } from "effect"

// --------------------------------------------------------------------------
// shared primitives
// --------------------------------------------------------------------------

/**
 * `ord(ch) < 32` — the exact predicate `app/contracts.py` uses. Spelled with
 * char codes instead of a regex literal so the rule stays readable and can be
 * diffed against the Python line by line.
 */
const hasControlChar = (v: string, allow: ReadonlySet<string>): boolean => {
  for (const ch of v) {
    if (ch.charCodeAt(0) < 32 && !allow.has(ch)) return true
  }
  return false
}

const NOTHING_ALLOWED: ReadonlySet<string> = new Set<string>()
const WHITESPACE_ALLOWED: ReadonlySet<string> = new Set<string>(["\n", "\r", "\t"])

/** Single-line field: rejects CR/LF and every other C0 char (header + log injection). */
const noControlChars = <A extends string>(s: Schema.Schema<A, string>) =>
  s.pipe(
    Schema.filter(
      (v) =>
        !hasControlChar(v as string, NOTHING_ALLOWED) ||
        "control characters are not allowed"
    )
  )

/** Multi-line field (textarea): \n \r \t are legal, every other C0 char is not. */
const noControlCharsMultiline = <A extends string>(s: Schema.Schema<A, string>) =>
  s.pipe(
    Schema.filter(
      (v) =>
        !hasControlChar(v as string, WHITESPACE_ALLOWED) ||
        "control characters are not allowed"
    )
  )

const Trimmed = Schema.transform(Schema.String, Schema.String, {
  strict: true,
  decode: (s) => s.trim(),
  encode: (s) => s
})

export const FeedbackType = Schema.Literal(
  "general",
  "bug",
  "feature",
  "thesis",
  "compute",
  "collaboration"
)
export type FeedbackType = Schema.Schema.Type<typeof FeedbackType>

export const FeedbackStatus = Schema.Literal("new", "reviewed", "archived", "spam")
export type FeedbackStatus = Schema.Schema.Type<typeof FeedbackStatus>

// --------------------------------------------------------------------------
// GET /api/stats
// --------------------------------------------------------------------------

export class StatsContract extends Schema.Class<StatsContract>("StatsContract")({
  nodes: Schema.Number,
  rels: Schema.Number,
  labels: Schema.Number,
  relTypes: Schema.Number,
  domains: Schema.Number,
  skills: Schema.Number
}) {}

// --------------------------------------------------------------------------
// GET /api/domains, GET /api/skills
// --------------------------------------------------------------------------

export class DomainRecord extends Schema.Class<DomainRecord>("DomainRecord")({
  name: Schema.String,
  displayName: Schema.String,
  nodeCount: Schema.Number,
  description: Schema.optionalWith(Schema.String, { default: () => "" })
}) {}

export class SkillRecord extends Schema.Class<SkillRecord>("SkillRecord")({
  name: Schema.String,
  description: Schema.String,
  category: Schema.optionalWith(Schema.String, { default: () => "methodology" })
}) {}

// --------------------------------------------------------------------------
// POST /api/feedback
// --------------------------------------------------------------------------

/**
 * The submission body.
 *
 * Python enforced `contact_consent` with a `model_validator(mode="after")`;
 * the equivalent here is a struct-level filter, so the cross-field rule is
 * part of the type rather than a runtime afterthought.
 */
/**
 * The WIRE shape: field names and base types only.
 *
 * This is what the endpoint declares, so OpenAPI still documents every field.
 * The constraints live in `FeedbackRequest` below and run inside the handler,
 * because a schema rejection at the endpoint boundary is a fixed-400
 * `HttpApiDecodeError` and FastAPI answers 422 — see `domain/Validation.ts`.
 */
export const FeedbackRequestWire = Schema.Struct({
  type: Schema.optional(Schema.String),
  subject: Schema.String,
  body: Schema.String,
  email: Schema.optional(Schema.String),
  source_path: Schema.optional(Schema.String),
  contact_consent: Schema.optional(Schema.Boolean),
  honeypot: Schema.optional(Schema.String),
  turnstile_token: Schema.optional(Schema.String)
})

export const FeedbackRequest = Schema.Struct({
  type: Schema.optionalWith(FeedbackType, { default: () => "general" as const }),
  subject: noControlChars(Trimmed).pipe(
    Schema.minLength(1, { message: () => "subject must contain visible text" }),
    Schema.maxLength(255)
  ),
  body: noControlCharsMultiline(Trimmed).pipe(
    Schema.minLength(1, { message: () => "body must contain visible text" }),
    Schema.maxLength(5000)
  ),
  email: Schema.optionalWith(
    noControlChars(Trimmed).pipe(
      Schema.maxLength(255),
      Schema.filter(
        (v) =>
          v === "" ||
          (v.includes("@") && !v.startsWith("@") && !v.endsWith("@")) ||
          "invalid email address"
      )
    ),
    { default: () => "" }
  ),
  source_path: Schema.optionalWith(
    noControlChars(Trimmed).pipe(
      Schema.maxLength(500),
      Schema.filter(
        (v) =>
          (v.startsWith("/") && !v.startsWith("//")) ||
          "source_path must be a local absolute path"
      )
    ),
    { default: () => "/" }
  ),
  contact_consent: Schema.optionalWith(Schema.Boolean, { default: () => false }),
  /** Must stay empty. A non-empty value marks a bot and is handled silently. */
  honeypot: Schema.optionalWith(Schema.String.pipe(Schema.maxLength(255)), {
    default: () => ""
  }),
  turnstile_token: Schema.optionalWith(Schema.String.pipe(Schema.maxLength(4096)), {
    default: () => ""
  })
}).pipe(
  Schema.filter(
    (r) =>
      r.email === "" ||
      r.contact_consent ||
      "contact_consent is required when email is provided"
  )
)
export type FeedbackRequest = Schema.Schema.Type<typeof FeedbackRequest>

export class FeedbackResponse extends Schema.Class<FeedbackResponse>("FeedbackResponse")({
  ok: Schema.optionalWith(Schema.Boolean, { default: () => true }),
  id: Schema.optionalWith(Schema.NullOr(Schema.String), { default: () => null }),
  status: Schema.optionalWith(Schema.Literal("stored", "accepted"), {
    default: () => "accepted" as const
  })
}) {}

/** One operator-visible inbox item. Network identifiers are never exposed. */
export class FeedbackRecord extends Schema.Class<FeedbackRecord>("FeedbackRecord")({
  id: Schema.String,
  created_at: Schema.String,
  type: FeedbackType,
  subject: Schema.String,
  body: Schema.String,
  email: Schema.optionalWith(Schema.String, { default: () => "" }),
  source_path: Schema.optionalWith(Schema.String, { default: () => "/" }),
  contact_consent: Schema.optionalWith(Schema.Boolean, { default: () => false }),
  status: Schema.optionalWith(FeedbackStatus, { default: () => "new" as const }),
  operator_note: Schema.optionalWith(Schema.String, { default: () => "" }),
  reviewed_at: Schema.optionalWith(Schema.NullOr(Schema.String), { default: () => null })
}) {}

export class FeedbackInboxResponse extends Schema.Class<FeedbackInboxResponse>(
  "FeedbackInboxResponse"
)({
  items: Schema.Array(FeedbackRecord),
  count: Schema.Number
}) {}

export const FeedbackTriageRequest = Schema.Struct({
  status: Schema.Literal("reviewed", "archived", "spam"),
  operator_note: Schema.optionalWith(
    noControlCharsMultiline(Trimmed).pipe(Schema.maxLength(1000)),
    { default: () => "" }
  )
})
export type FeedbackTriageRequest = Schema.Schema.Type<typeof FeedbackTriageRequest>

export class FeedbackTriageResponse extends Schema.Class<FeedbackTriageResponse>(
  "FeedbackTriageResponse"
)({
  ok: Schema.optionalWith(Schema.Boolean, { default: () => true }),
  item: FeedbackRecord
}) {}

// --------------------------------------------------------------------------
// POST /api/kg/{read,write}
// --------------------------------------------------------------------------

/** Wire shape; the length bounds are enforced in the handler for 422. */
export const CypherRequestWire = Schema.Struct({
  query: Schema.String,
  params: Schema.optional(Schema.Record({ key: Schema.String, value: Schema.Unknown }))
})

export const CypherRequest = Schema.Struct({
  query: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(20000)),
  params: Schema.optionalWith(Schema.Record({ key: Schema.String, value: Schema.Unknown }), {
    default: () => ({})
  })
})
export type CypherRequest = Schema.Schema.Type<typeof CypherRequest>

export class CypherResponse extends Schema.Class<CypherResponse>("CypherResponse")({
  rows: Schema.Array(Schema.Record({ key: Schema.String, value: Schema.Unknown })),
  count: Schema.Number,
  mode: Schema.Literal("read", "write"),
  truncated: Schema.optionalWith(Schema.Boolean, { default: () => false })
}) {}

// --------------------------------------------------------------------------
// /api/research/* — the living research body
// --------------------------------------------------------------------------

/** `live` vs `snapshot` so a stale magnitude is never shown as current. */
export const SourceMode = Schema.Literal("live", "snapshot")
export type SourceMode = Schema.Schema.Type<typeof SourceMode>

export class ResearchSummary extends Schema.Class<ResearchSummary>("ResearchSummary")({
  findings: Schema.Number,
  lessons: Schema.Number,
  papers: Schema.Number,
  validations: Schema.Number,
  consensus: Schema.Number,
  decisions: Schema.Number,
  apostles: Schema.Number,
  domains: Schema.Number,
  source: Schema.optionalWith(SourceMode, { default: () => "snapshot" as const })
}) {}

export class FindingRecord extends Schema.Class<FindingRecord>("FindingRecord")({
  name: Schema.String,
  finding: Schema.String,
  axis: Schema.optionalWith(Schema.String, { default: () => "" }),
  subAxis: Schema.optionalWith(Schema.String, { default: () => "" }),
  /** Categorical ("HIGH") *or* numeric ("0.82") in the KG — kept a string so
   *  neither form is lost. */
  confidence: Schema.optionalWith(Schema.String, { default: () => "" }),
  cycleId: Schema.optionalWith(Schema.String, { default: () => "" }),
  verified: Schema.optionalWith(Schema.NullOr(Schema.Boolean), { default: () => null }),
  lakatosMechanism: Schema.optionalWith(Schema.String, { default: () => "" }),
  citationUrl: Schema.optionalWith(Schema.String, { default: () => "" }),
  createdAt: Schema.optionalWith(Schema.String, { default: () => "" })
}) {}

export class LessonRecord extends Schema.Class<LessonRecord>("LessonRecord")({
  name: Schema.String,
  problem: Schema.optionalWith(Schema.String, { default: () => "" }),
  solution: Schema.optionalWith(Schema.String, { default: () => "" }),
  wrongAssumption: Schema.optionalWith(Schema.String, { default: () => "" }),
  truth: Schema.optionalWith(Schema.String, { default: () => "" }),
  category: Schema.optionalWith(Schema.String, { default: () => "" }),
  severity: Schema.optionalWith(Schema.String, { default: () => "" }),
  lakatosMechanism: Schema.optionalWith(Schema.String, { default: () => "" }),
  createdAt: Schema.optionalWith(Schema.String, { default: () => "" })
}) {}

export class PaperRecord extends Schema.Class<PaperRecord>("PaperRecord")({
  title: Schema.String,
  author: Schema.optionalWith(Schema.String, { default: () => "" }),
  year: Schema.optionalWith(Schema.NullOr(Schema.Number), { default: () => null }),
  journal: Schema.optionalWith(Schema.String, { default: () => "" }),
  doi: Schema.optionalWith(Schema.String, { default: () => "" }),
  domain: Schema.optionalWith(Schema.String, { default: () => "" }),
  coreThesis: Schema.optionalWith(Schema.String, { default: () => "" }),
  status: Schema.optionalWith(Schema.String, { default: () => "" })
}) {}

export class ConsensusRecord extends Schema.Class<ConsensusRecord>("ConsensusRecord")({
  name: Schema.String,
  summary: Schema.optionalWith(Schema.String, { default: () => "" }),
  createdAt: Schema.optionalWith(Schema.String, { default: () => "" })
}) {}

export const RecentKind = Schema.Literal(
  "finding",
  "lesson",
  "paper",
  "consensus",
  "validation",
  "decision"
)
export type RecentKind = Schema.Schema.Type<typeof RecentKind>

export class RecentItem extends Schema.Class<RecentItem>("RecentItem")({
  type: RecentKind,
  name: Schema.String,
  title: Schema.String,
  summary: Schema.optionalWith(Schema.String, { default: () => "" }),
  createdAt: Schema.optionalWith(Schema.String, { default: () => "" })
}) {}

export class GraphNeighbor extends Schema.Class<GraphNeighbor>("GraphNeighbor")({
  direction: Schema.Literal("out", "in"),
  type: Schema.String,
  name: Schema.optionalWith(Schema.String, { default: () => "" }),
  labels: Schema.optionalWith(Schema.Array(Schema.String), { default: () => [] })
}) {}

/** Makes any node a doorway: walk to its real typed neighbours instead of
 *  reading a flat card. `truncated` is true when degree exceeds the cap. */
export class NodeNeighbors extends Schema.Class<NodeNeighbors>("NodeNeighbors")({
  name: Schema.String,
  found: Schema.Boolean,
  degree: Schema.Number,
  neighbors: Schema.Array(GraphNeighbor),
  truncated: Schema.optionalWith(Schema.Boolean, { default: () => false })
}) {}

/** The machine-readable feed. `/llms.txt` holds the doctrine; this is the
 *  live layer, so an agent can cite current research rather than a snapshot. */
export class AgentFeed extends Schema.Class<AgentFeed>("AgentFeed")({
  doctrine_url: Schema.String,
  canonical_source: Schema.String,
  generated_hint: Schema.String,
  summary: ResearchSummary,
  recent_findings: Schema.Array(FindingRecord),
  recent_lessons: Schema.Array(LessonRecord),
  how_to_cite: Schema.String
}) {}

// --------------------------------------------------------------------------
// meta
// --------------------------------------------------------------------------

export class HealthResponse extends Schema.Class<HealthResponse>("HealthResponse")({
  status: Schema.Literal("ok"),
  version: Schema.String
}) {}

/**
 * `/ready` — the exact payload `app/routers/meta.py:66-73` emits.
 *
 * This shape is load-bearing, not cosmetic: `ops/check-web-back-live.sh:296-302`
 * asserts `status == "ready"`, `kg_live is True`, `wiki_required is True`,
 * `wiki_live is True` and `degraded is False`. The first version of this port
 * invented its own `{ready, version, components[]}` shape, which would have
 * failed that gate on the first deploy.
 *
 * Readiness is dependency-aware but degraded-tolerant (PROM16 C5): the service
 * serves snapshot and in-memory fallbacks when the KG or Mongo are down, so it
 * stays READY and reports `degraded` instead of failing.
 */
export class ReadyResponse extends Schema.Class<ReadyResponse>("ReadyResponse")({
  status: Schema.Literal("ready", "not_ready"),
  kg_live: Schema.Boolean,
  wiki_required: Schema.Boolean,
  wiki_live: Schema.Boolean,
  wiki_store_live: Schema.Boolean,
  wiki_rate_limit_live: Schema.Boolean,
  degraded: Schema.Boolean
}) {}

export class RootResponse extends Schema.Class<RootResponse>("RootResponse")({
  service: Schema.String,
  version: Schema.String,
  endpoints: Schema.Array(Schema.String),
  /** Not in the Python payload. An additive key so the two implementations
   *  are distinguishable at runtime; readers of the Python shape are unaffected. */
  runtime: Schema.Literal("effect-ts")
}) {}

// --------------------------------------------------------------------------
// pagination — the bounds live here, applied by the handler (see Validation.ts)
// --------------------------------------------------------------------------

/** `Query(default, ge=lo, le=hi)` — rejects, never clamps. */
const BoundedFromString = (fallback: number, lo: number, hi: number) =>
  Schema.optionalWith(Schema.NumberFromString.pipe(Schema.int(), Schema.between(lo, hi)), {
    default: () => fallback
  })

export const ListParamsStrict = Schema.Struct({
  limit: BoundedFromString(20, 1, 100),
  offset: BoundedFromString(0, 0, 10_000)
})

export const FindingsParamsStrict = Schema.Struct({
  limit: BoundedFromString(20, 1, 100),
  offset: BoundedFromString(0, 0, 10_000),
  cycle: Schema.optionalWith(Schema.String.pipe(Schema.maxLength(200)), { default: () => "" })
})

export const PapersParamsStrict = Schema.Struct({
  limit: BoundedFromString(20, 1, 100),
  offset: BoundedFromString(0, 0, 10_000),
  domain: Schema.optionalWith(Schema.String.pipe(Schema.maxLength(200)), { default: () => "" })
})

export const RecentParamsStrict = Schema.Struct({ limit: BoundedFromString(30, 1, 100) })

export const InboxParamsStrict = Schema.Struct({ limit: BoundedFromString(50, 1, 100) })

/** `name` is REQUIRED — `tests/test_research_endpoints.py:277` asserts 422 without it. */
export const NeighborsParamsStrict = Schema.Struct({
  name: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(512)),
  limit: BoundedFromString(50, 1, 200)
})
