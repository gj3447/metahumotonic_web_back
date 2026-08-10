/**
 * The SchemaRegistry, mirrored client-side.
 *
 * `t_schema_freeze_label_v2` and `t_schema_freeze_reltype_v1` reject any write
 * introducing a label or relationship type that is not in
 * `schema-registry-v1-2026-08-03`. The rejection arrives as a
 * `TransactionHookFailed` blob at commit time, after the transaction has done
 * its work — I hit exactly this earlier today with `FINDING_OF`, which does
 * not exist; `HAS_FINDING` does.
 *
 * So the registry is loaded once and consulted *before* the write. Same rule,
 * enforced twice: the database is still the authority — this is a fast, legible
 * pre-check, never a replacement for it.
 *
 * Fail-closed by design: if the registry cannot be read, nothing is admissible.
 * An agent that cannot verify what is allowed must not guess.
 */
import { Context, Effect, Layer } from "effect"
import { KgPortTag } from "./KgPort.js"

export interface SchemaGuard {
  readonly labelAllowed: (label: string) => boolean
  readonly relTypeAllowed: (relType: string) => boolean
  /** Everything in `candidates` that the registry would reject. */
  readonly rejectedLabels: (candidates: Iterable<string>) => ReadonlyArray<string>
  readonly rejectedRelTypes: (candidates: Iterable<string>) => ReadonlyArray<string>
  /** False when the registry could not be loaded — then nothing is admissible. */
  readonly loaded: boolean
  readonly counts: { readonly labels: number; readonly relTypes: number }
}

export class SchemaGuardTag extends Context.Tag("SchemaGuard")<SchemaGuardTag, SchemaGuard>() {}

const REGISTRY = "schema-registry-v1-2026-08-03"

const REGISTRY_CYPHER = `
MATCH (r:SchemaRegistry {name: $registry})
RETURN r.allowed_labels AS labels, r.allowed_reltypes AS relTypes
`

const deny: SchemaGuard = {
  labelAllowed: () => false,
  relTypeAllowed: () => false,
  rejectedLabels: (c) => Array.from(c),
  rejectedRelTypes: (c) => Array.from(c),
  loaded: false,
  counts: { labels: 0, relTypes: 0 }
}

const fromSets = (labels: ReadonlySet<string>, relTypes: ReadonlySet<string>): SchemaGuard => ({
  labelAllowed: (l) => labels.has(l),
  relTypeAllowed: (r) => relTypes.has(r),
  rejectedLabels: (c) => Array.from(new Set(Array.from(c).filter((l) => !labels.has(l)))).sort(),
  rejectedRelTypes: (c) => Array.from(new Set(Array.from(c).filter((r) => !relTypes.has(r)))).sort(),
  loaded: true,
  counts: { labels: labels.size, relTypes: relTypes.size }
})

/** Build a guard from explicit lists — used by tests and by the offline layer. */
export const make = (
  labels: Iterable<string>,
  relTypes: Iterable<string>
): SchemaGuard => fromSets(new Set(labels), new Set(relTypes))

/**
 * Load the registry once at layer construction and cache it for the process
 * lifetime. The frozen registry changes only by ratified proposal, so a
 * per-request re-read would buy nothing and cost a round trip.
 */
export const SchemaGuardLive: Layer.Layer<SchemaGuardTag, never, KgPortTag> = Layer.effect(
  SchemaGuardTag,
  Effect.gen(function* () {
    const kg = yield* KgPortTag

    const rows = yield* kg.run(REGISTRY_CYPHER, { registry: REGISTRY }, "read").pipe(
      Effect.catchAll((e) =>
        Effect.logWarning(`schema guard: registry unreadable (${e.reason}) — refusing all writes`).pipe(
          Effect.as([] as ReadonlyArray<Record<string, unknown>>)
        )
      )
    )

    const row = rows[0]
    if (row === undefined) return deny

    const asStrings = (v: unknown): ReadonlyArray<string> =>
      Array.isArray(v) ? v.map((x) => String(x)) : []

    const labels = asStrings(row["labels"])
    const relTypes = asStrings(row["relTypes"])
    if (labels.length === 0 || relTypes.length === 0) return deny

    yield* Effect.logInfo(
      `schema guard: ${labels.length} labels, ${relTypes.length} relTypes from ${REGISTRY}`
    )
    return fromSets(new Set(labels), new Set(relTypes))
  })
)

/**
 * Offline guard for CI and tests.
 *
 * Seeded with the labels and relationship types this service actually writes,
 * verified against the live registry on 2026-08-10. Deliberately a short list:
 * a permissive stand-in would let a test pass on a write the real KG rejects.
 */
export const SchemaGuardOffline = Layer.succeed(
  SchemaGuardTag,
  make(
    [
      "ResearchFinding",
      "Lesson",
      "Evidence",
      "DispatchHyperedge",
      "Provenance",
      "KnowledgeHub",
      "Comment",
      "ActiveComment",
      "Possibility"
    ],
    [
      "HAS_FINDING",
      "DERIVED_FROM",
      "EVIDENCED_BY",
      "HAS_EVIDENCE",
      "PRODUCED_BY",
      "DISPATCHED",
      "GENERATED_BY",
      "SUPPORTS",
      "CONTRADICTS",
      "REFERENCES"
    ]
  )
)
