/**
 * Write intents — the other half of the feedback loop.
 *
 * Until now the agent could only *read* the KG. A feedback loop that cannot
 * write is half a loop: verdict → root cause → lesson → next action needs the
 * last arrow to land somewhere durable.
 *
 * The interesting part is not that these write. It is **what the types
 * encode**. This KG runs 17 APOC triggers that reject a bad write at commit
 * time with a `TransactionHookFailed` blob — I hit three of them by hand
 * earlier today. Every requirement below is one of those triggers, lifted
 * into a schema so the request is refused at the boundary with a readable
 * 400 instead of at the database with a stack trace:
 *
 *   t_researchfinding_citation_required     → CitationEvidence, a required union
 *   t_lesson_lakatos_mechanism_required_v27 → LakatosMechanism, a required enum
 *   t_lesson_name_not_null                  → minLength(1) on the name
 *   t_schema_freeze_label_v2                → checked by SchemaGuard
 *   t_schema_freeze_reltype_v1              → checked by SchemaGuard
 *
 * A constraint the database enforces and the client does not know about is a
 * constraint you discover in production. Mirroring it is the whole point.
 */
import { Schema } from "effect"

/** Slug shape used across the KG: kebab-ish, dated, no whitespace. */
export const NodeName = Schema.String.pipe(
  Schema.minLength(1),
  Schema.maxLength(256),
  Schema.filter((s) => !/\s/.test(s) || "node names must not contain whitespace")
)

/**
 * `t_researchfinding_citation_required`.
 *
 * A finding must be traceable to something outside itself. Either a URL, or
 * an explicit statement of why there isn't one — "I measured it myself" is a
 * legitimate answer, "I forgot" is not, and the union forces the author to
 * pick one on the record.
 */
export const CitationEvidence = Schema.Union(
  Schema.Struct({
    _tag: Schema.Literal("Url"),
    citation_url: Schema.String.pipe(
      Schema.minLength(1),
      Schema.filter((s) => /^https?:\/\//.test(s) || "citation_url must be http(s)")
    )
  }),
  Schema.Struct({
    _tag: Schema.Literal("NoExternalSource"),
    no_external_citation_reason: Schema.String.pipe(
      Schema.minLength(12, {
        message: () => "state *why* there is no external citation, in a sentence"
      }),
      Schema.maxLength(500)
    )
  })
)
export type CitationEvidence = Schema.Schema.Type<typeof CitationEvidence>

/**
 * `t_lesson_lakatos_mechanism_required_v27` (HR20 / RFC v27 §2.2).
 *
 * The enum is the canonical five. A Lesson without one is blocked at write
 * time, so it is required here rather than optional-with-a-default: a default
 * would be the agent inventing an epistemology.
 */
export const LakatosMechanism = Schema.Literal(
  "monster-barring",
  "exception-barring",
  "concept-stretching",
  "lemma-incorporation",
  "proof-analysis"
)
export type LakatosMechanism = Schema.Schema.Type<typeof LakatosMechanism>

/** Who/when/from-what. Stamped on every node this port writes. */
export const Provenance = Schema.Struct({
  actor: Schema.String.pipe(Schema.minLength(1)),
  /** The run that produced this — lets a whole batch be traced or reverted. */
  runId: Schema.String.pipe(Schema.minLength(1)),
  /** Free-text pointer to the evidence: a path, an endpoint, a commit. */
  sourcePath: Schema.optionalWith(Schema.String, { default: () => "" })
})
export type Provenance = Schema.Schema.Type<typeof Provenance>

// ---------------------------------------------------------------------------
// the intents
// ---------------------------------------------------------------------------

export const RecordFinding = Schema.Struct({
  _tag: Schema.Literal("RecordFinding"),
  name: NodeName,
  description: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(8000)),
  evidence: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(8000)),
  citation: CitationEvidence,
  /** Free-form, but never silently "CONFIRMED" — the caller must say. */
  status: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(64)),
  /** Hub to attach to via HAS_FINDING. Must already exist. */
  hub: Schema.optionalWith(NodeName, { nullable: true })
})

export const RecordLesson = Schema.Struct({
  _tag: Schema.Literal("RecordLesson"),
  name: NodeName,
  /** The symmetric pair the canon requires — both halves, or neither. */
  wrongAssumption: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(4000)),
  truth: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(4000)),
  problem: Schema.optionalWith(Schema.String, { default: () => "" }),
  solution: Schema.optionalWith(Schema.String, { default: () => "" }),
  lakatos_mechanism: LakatosMechanism,
  citation: CitationEvidence
})

/**
 * A dispatch, reified.
 *
 * One parent, N children, one fan-in — recorded as a single node so the
 * question "which dispatch produced this?" is answerable later. Storing N
 * separate edges loses the fact that they were one event.
 */
export const RecordDispatch = Schema.Struct({
  _tag: Schema.Literal("RecordDispatch"),
  name: NodeName,
  parent: NodeName,
  children: Schema.Array(NodeName).pipe(Schema.minItems(1), Schema.maxItems(200)),
  kind: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(64)),
  stoppedBecause: Schema.String,
  citation: CitationEvidence
})

/**
 * A typed edge between two nodes that already exist.
 *
 * `MERGE`-ing an endpoint into existence is how orphan nodes get created by
 * accident, so both ends are matched, never created.
 */
export const LinkNodes = Schema.Struct({
  _tag: Schema.Literal("LinkNodes"),
  from: NodeName,
  to: NodeName,
  /** Validated against the SchemaRegistry before the write is attempted. */
  relType: Schema.String.pipe(
    Schema.minLength(1),
    Schema.filter((s) => /^[A-Z][A-Z0-9_]*$/.test(s) || "relType must be SCREAMING_SNAKE_CASE")
  )
})

export const WriteIntent = Schema.Union(RecordFinding, RecordLesson, RecordDispatch, LinkNodes)
export type WriteIntent = Schema.Schema.Type<typeof WriteIntent>

export const WriteBatch = Schema.Struct({
  /**
   * Default TRUE. A write to canon is destructive-adjacent, and the house
   * rule is dry-run first. The caller must ask for the real thing.
   */
  dryRun: Schema.optionalWith(Schema.Boolean, { default: () => true }),
  provenance: Provenance,
  intents: Schema.Array(WriteIntent).pipe(Schema.minItems(1), Schema.maxItems(50))
})
export type WriteBatch = Schema.Schema.Type<typeof WriteBatch>

// ---------------------------------------------------------------------------
// results
// ---------------------------------------------------------------------------

export class PlannedWrite extends Schema.Class<PlannedWrite>("PlannedWrite")({
  intent: Schema.String,
  target: Schema.String,
  cypher: Schema.String,
  /** Labels and relationship types this statement will introduce. */
  labels: Schema.Array(Schema.String),
  relTypes: Schema.Array(Schema.String),
  /** False ⇒ the SchemaRegistry would reject it; the batch is refused. */
  admissible: Schema.Boolean,
  refusedBecause: Schema.optionalWith(Schema.String, { default: () => "" })
}) {}

export class WriteReceipt extends Schema.Class<WriteReceipt>("WriteReceipt")({
  runId: Schema.String,
  dryRun: Schema.Boolean,
  planned: Schema.Array(PlannedWrite),
  /** Node names verified to exist by a read-back AFTER the write. */
  readback: Schema.Array(Schema.String),
  /** Planned but absent on read-back — a silent failure made loud. */
  missing: Schema.Array(Schema.String),
  committed: Schema.Boolean
}) {}
