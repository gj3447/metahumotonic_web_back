/**
 * The agent's write path — closing the feedback loop.
 *
 * Four rules, each of them a scar:
 *
 *  1. **Dry-run first.** `dryRun` defaults to true. A caller who wants to
 *     touch canon has to say so.
 *  2. **Preflight against the registry.** Labels and relationship types are
 *     checked before the transaction, so a frozen-schema violation is a 400
 *     naming the offending type, not a commit-time blob.
 *  3. **Read back, always.** House rule: *a skipped readback creates a
 *     duplicate, not a missing node.* Anything planned but absent afterwards
 *     is reported in `missing` — a silent partial write is the failure mode
 *     this exists to prevent.
 *  4. **Never MERGE an endpoint into existence.** `LinkNodes` MATCHes both
 *     ends. Merging them is how orphans appear.
 *
 * What this port deliberately does NOT do: supersede, archive, delete, or
 * re-label anything. It is additive. Retiring canon is Occam's job and needs
 * a human verdict.
 */
import { Context, Effect, Layer } from "effect"
import {
  PlannedWrite,
  WriteReceipt,
  type CitationEvidence,
  type Provenance,
  type WriteBatch,
  type WriteIntent
} from "../domain/WriteIntent.js"
import { BadRequest, KgQueryFailed, Unavailable } from "../domain/Errors.js"
import { KgPortTag } from "./KgPort.js"
import { SchemaGuardTag } from "./SchemaGuard.js"

export interface KgWritePort {
  /** Plan + (optionally) execute + read back. Never throws on a node-level miss. */
  readonly apply: (
    batch: WriteBatch
  ) => Effect.Effect<WriteReceipt, BadRequest | Unavailable | KgQueryFailed>
}

export class KgWritePortTag extends Context.Tag("KgWritePort")<KgWritePortTag, KgWritePort>() {}

// ---------------------------------------------------------------------------
// planning — pure
// ---------------------------------------------------------------------------

/** Flatten the citation union back into the two properties the trigger reads. */
const citationProps = (c: CitationEvidence): Record<string, string> =>
  c._tag === "Url"
    ? { citation_url: c.citation_url }
    : { no_external_citation_reason: c.no_external_citation_reason }

interface Statement {
  readonly intent: string
  readonly target: string
  readonly cypher: string
  readonly params: Record<string, unknown>
  readonly labels: ReadonlyArray<string>
  readonly relTypes: ReadonlyArray<string>
  /** Node names to verify on read-back. Empty for pure edge writes. */
  readonly expectNodes: ReadonlyArray<string>
}

const stamp = (p: Provenance) => ({
  actor: p.actor,
  run_id: p.runId,
  sourcePath: p.sourcePath,
  provenance: `agent run ${p.runId} by ${p.actor}${p.sourcePath === "" ? "" : ` — ${p.sourcePath}`}`
})

/**
 * Turn one intent into one statement.
 *
 * Every relationship type is a literal in the Cypher text, never interpolated
 * from user input — `LinkNodes` is the only intent that varies, and its type
 * is checked against the registry *and* a SCREAMING_SNAKE pattern before it
 * reaches here.
 */
export const plan = (intent: WriteIntent, prov: Provenance, nowIso: string): Statement => {
  const base = { ...stamp(prov), written_at: nowIso }

  switch (intent._tag) {
    case "RecordFinding": {
      const withHub = intent.hub !== undefined && intent.hub !== null
      return {
        intent: intent._tag,
        target: intent.name,
        labels: ["ResearchFinding"],
        relTypes: withHub ? ["HAS_FINDING"] : [],
        expectNodes: [intent.name],
        cypher: withHub
          ? `MATCH (h {name: $hub})
             MERGE (f:ResearchFinding {name: $name})
             SET f += $props
             MERGE (h)-[:HAS_FINDING]->(f)
             RETURN f.name AS name`
          : `MERGE (f:ResearchFinding {name: $name})
             SET f += $props
             RETURN f.name AS name`,
        params: {
          name: intent.name,
          ...(withHub ? { hub: intent.hub } : {}),
          props: {
            ...base,
            description: intent.description,
            evidence: intent.evidence,
            status: intent.status,
            epistemic_status: "RESEARCHED",
            verified: true,
            ...citationProps(intent.citation)
          }
        }
      }
    }

    case "RecordLesson":
      return {
        intent: intent._tag,
        target: intent.name,
        labels: ["Lesson"],
        relTypes: [],
        expectNodes: [intent.name],
        cypher: `MERGE (l:Lesson {name: $name}) SET l += $props RETURN l.name AS name`,
        params: {
          name: intent.name,
          props: {
            ...base,
            wrongAssumption: intent.wrongAssumption,
            truth: intent.truth,
            problem: intent.problem,
            solution: intent.solution,
            // the trigger reads snake_case; the read surface reads camelCase
            lakatos_mechanism: intent.lakatos_mechanism,
            lakatosMechanism: intent.lakatos_mechanism,
            ...citationProps(intent.citation)
          }
        }
      }

    case "RecordDispatch":
      return {
        intent: intent._tag,
        target: intent.name,
        labels: ["DispatchHyperedge"],
        relTypes: ["DISPATCHED", "PRODUCED_BY"],
        expectNodes: [intent.name],
        // Children are MATCHed, not created: a dispatch records what happened,
        // it does not conjure the nodes it claims to have visited.
        cypher: `MERGE (d:DispatchHyperedge {name: $name})
                 SET d += $props
                 WITH d
                 MATCH (p {name: $parent})
                 MERGE (d)-[:PRODUCED_BY]->(p)
                 WITH d
                 UNWIND $children AS childName
                 MATCH (c {name: childName})
                 MERGE (d)-[:DISPATCHED]->(c)
                 RETURN d.name AS name`,
        params: {
          name: intent.name,
          parent: intent.parent,
          children: intent.children,
          props: {
            ...base,
            kind: intent.kind,
            parent: intent.parent,
            child_count: intent.children.length,
            stopped_because: intent.stoppedBecause,
            ...citationProps(intent.citation)
          }
        }
      }

    case "LinkNodes":
      return {
        intent: intent._tag,
        target: `${intent.from} -[:${intent.relType}]-> ${intent.to}`,
        labels: [],
        relTypes: [intent.relType],
        expectNodes: [],
        cypher: `MATCH (a {name: $from})
                 MATCH (b {name: $to})
                 MERGE (a)-[r:\`${intent.relType}\`]->(b)
                 SET r.run_id = $runId, r.actor = $actor
                 RETURN type(r) AS name`,
        params: { from: intent.from, to: intent.to, runId: prov.runId, actor: prov.actor }
      }
  }
}

const READBACK = `
UNWIND $names AS n
MATCH (x {name: n})
RETURN collect(DISTINCT x.name) AS found
`

// ---------------------------------------------------------------------------
// the live port
// ---------------------------------------------------------------------------

export const KgWritePortLive: Layer.Layer<
  KgWritePortTag,
  never,
  KgPortTag | SchemaGuardTag
> = Layer.effect(
  KgWritePortTag,
  Effect.gen(function* () {
    const kg = yield* KgPortTag
    const guard = yield* SchemaGuardTag

    const apply: KgWritePort["apply"] = (batch) =>
      Effect.gen(function* () {
        const nowIso = new Date().toISOString()
        const statements = batch.intents.map((i) => plan(i, batch.provenance, nowIso))

        // --- preflight: the registry check, before any transaction ----------
        const planned = statements.map((s) => {
          const badLabels = guard.rejectedLabels(s.labels)
          const badRels = guard.rejectedRelTypes(s.relTypes)
          const admissible = guard.loaded && badLabels.length === 0 && badRels.length === 0
          const refusedBecause = !guard.loaded
            ? "SchemaRegistry unreadable — refusing all writes (fail-closed)"
            : badLabels.length > 0
              ? `labels not in SchemaRegistry: ${badLabels.join(", ")}`
              : badRels.length > 0
                ? `relationship types not in SchemaRegistry: ${badRels.join(", ")}`
                : ""

          return new PlannedWrite({
            intent: s.intent,
            target: s.target,
            cypher: s.cypher.replace(/\s+/g, " ").trim(),
            labels: s.labels,
            relTypes: s.relTypes,
            admissible,
            refusedBecause
          })
        })

        // All-or-nothing: one inadmissible statement refuses the batch rather
        // than committing a partial one the caller did not ask for.
        const inadmissible = planned.filter((p) => !p.admissible)
        if (inadmissible.length > 0) {
          return yield* Effect.fail(
            new BadRequest({
              reason: `refused ${inadmissible.length}/${planned.length} statement(s): ${inadmissible
                .map((p) => `${p.target} — ${p.refusedBecause}`)
                .join("; ")}`
            })
          )
        }

        if (batch.dryRun) {
          return new WriteReceipt({
            runId: batch.provenance.runId,
            dryRun: true,
            planned,
            readback: [],
            missing: [],
            committed: false
          })
        }

        // --- execute --------------------------------------------------------
        for (const s of statements) {
          yield* kg.run(s.cypher, s.params, "write")
        }

        // --- read back ------------------------------------------------------
        // Not optional. A write reported as successful but absent on read-back
        // is the single most expensive failure mode this KG has.
        const expected = Array.from(new Set(statements.flatMap((s) => s.expectNodes)))
        const found =
          expected.length === 0
            ? []
            : yield* kg.run(READBACK, { names: expected }, "read").pipe(
                Effect.map((rows) => {
                  const raw = rows[0]?.["found"]
                  return Array.isArray(raw) ? raw.map((x) => String(x)) : []
                })
              )

        const foundSet = new Set(found)
        const missing = expected.filter((n) => !foundSet.has(n)).sort()

        if (missing.length > 0) {
          yield* Effect.logError(
            `kg write: ${missing.length} node(s) absent on readback — ${missing.join(", ")}`
          )
        }

        return new WriteReceipt({
          runId: batch.provenance.runId,
          dryRun: false,
          planned,
          readback: found.slice().sort(),
          missing,
          committed: missing.length === 0
        })
      }).pipe(Effect.withSpan("kg.write", { attributes: { intents: batch.intents.length } }))

    return { apply } satisfies KgWritePort
  })
)

/**
 * Offline port: plans and refuses to commit.
 *
 * Used when no live KG is configured. It still runs the full preflight, so a
 * schema violation is caught in CI rather than the first time someone points
 * the service at a real database.
 */
export const KgWritePortDryOnly: Layer.Layer<KgWritePortTag, never, SchemaGuardTag> = Layer.effect(
  KgWritePortTag,
  Effect.gen(function* () {
    const guard = yield* SchemaGuardTag
    return {
      apply: (batch) =>
        Effect.gen(function* () {
          const nowIso = new Date().toISOString()
          const planned = batch.intents.map((i) => {
            const s = plan(i, batch.provenance, nowIso)
            const badLabels = guard.rejectedLabels(s.labels)
            const badRels = guard.rejectedRelTypes(s.relTypes)
            return new PlannedWrite({
              intent: s.intent,
              target: s.target,
              cypher: s.cypher.replace(/\s+/g, " ").trim(),
              labels: s.labels,
              relTypes: s.relTypes,
              admissible: guard.loaded && badLabels.length === 0 && badRels.length === 0,
              refusedBecause: [...badLabels, ...badRels].join(", ")
            })
          })

          const bad = planned.filter((p) => !p.admissible)
          if (bad.length > 0) {
            return yield* Effect.fail(
              new BadRequest({
                reason: `refused: ${bad.map((p) => `${p.target} — ${p.refusedBecause}`).join("; ")}`
              })
            )
          }
          if (!batch.dryRun) {
            return yield* Effect.fail(
              new Unavailable({
                reason: "no live KG configured — set MHB_NEO4J_LIVE to commit writes"
              })
            )
          }
          return new WriteReceipt({
            runId: batch.provenance.runId,
            dryRun: true,
            planned,
            readback: [],
            missing: [],
            committed: false
          })
        })
    } satisfies KgWritePort
  })
)
