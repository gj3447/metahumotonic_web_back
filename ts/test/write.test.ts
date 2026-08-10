/**
 * Write-path tests.
 *
 * The property under test throughout: **a rule the database enforces must be
 * enforced here too, earlier and more legibly.** Every case below corresponds
 * to a real APOC trigger on the SYMPOSIUM KG, verified live on 2026-08-10.
 */
import { Effect, Layer, Schema } from "effect"
import { describe, expect, it } from "vitest"
import {
  RecordFinding,
  WriteBatch,
  type Provenance,
  type WriteIntent
} from "../src/domain/WriteIntent.js"
import { plan } from "../src/ports/KgWritePort.js"
import { KgWritePortDryOnly, KgWritePortTag } from "../src/ports/KgWritePort.js"
import * as SchemaGuard from "../src/ports/SchemaGuard.js"
import { SchemaGuardOffline, SchemaGuardTag } from "../src/ports/SchemaGuard.js"

const prov: Provenance = { actor: "test-agent", runId: "run-0001", sourcePath: "test" }
const NOW = "2026-08-10T00:00:00.000Z"

const url = { _tag: "Url" as const, citation_url: "https://example.org/x" }

const finding = (over: Partial<Record<string, unknown>> = {}) => ({
  _tag: "RecordFinding",
  name: "rf-test-2026-08-10",
  description: "a description",
  evidence: "measured",
  citation: url,
  status: "MEASURED",
  ...over
})

// ---------------------------------------------------------------------------

describe("WriteIntent — the schema mirrors the database triggers", () => {
  const decode = Schema.decodeUnknownEither(WriteBatch)
  const batch = (intents: ReadonlyArray<unknown>, dryRun = true) =>
    decode({ dryRun, provenance: prov, intents })

  it("dryRun defaults to true — the dangerous call must be spelled out", () => {
    const r = decode({ provenance: prov, intents: [finding()] })
    expect(r._tag === "Right" && r.right.dryRun).toBe(true)
  })

  // t_researchfinding_citation_required
  it("refuses a finding with no citation and no stated reason", () => {
    const { citation, ...noCitation } = finding()
    void citation
    expect(batch([noCitation])._tag).toBe("Left")
  })

  it("accepts a finding with an explicit no-external-source reason", () => {
    const r = batch([
      finding({
        citation: {
          _tag: "NoExternalSource",
          no_external_citation_reason: "measured directly in this session"
        }
      })
    ])
    expect(r._tag).toBe("Right")
  })

  it("refuses a hand-wave in place of a reason", () => {
    const r = batch([
      finding({ citation: { _tag: "NoExternalSource", no_external_citation_reason: "n/a" } })
    ])
    expect(r._tag).toBe("Left") // too short to be a reason
  })

  it("refuses a non-http citation url", () => {
    const r = batch([finding({ citation: { _tag: "Url", citation_url: "see the paper" } })])
    expect(r._tag).toBe("Left")
  })

  // t_lesson_lakatos_mechanism_required_v27
  it("refuses a Lesson without a lakatos_mechanism", () => {
    const r = batch([
      {
        _tag: "RecordLesson",
        name: "lesson-x-2026-08-10",
        wrongAssumption: "w",
        truth: "t",
        citation: url
      }
    ])
    expect(r._tag).toBe("Left")
  })

  it("refuses a lakatos_mechanism outside the canonical five", () => {
    const r = batch([
      {
        _tag: "RecordLesson",
        name: "lesson-x-2026-08-10",
        wrongAssumption: "w",
        truth: "t",
        lakatos_mechanism: "vibes",
        citation: url
      }
    ])
    expect(r._tag).toBe("Left")
  })

  it("accepts one of the canonical five", () => {
    const r = batch([
      {
        _tag: "RecordLesson",
        name: "lesson-x-2026-08-10",
        wrongAssumption: "w",
        truth: "t",
        lakatos_mechanism: "exception-barring",
        citation: url
      }
    ])
    expect(r._tag).toBe("Right")
  })

  // t_lesson_name_not_null + the whitespace rule
  it("refuses an empty or whitespace-bearing node name", () => {
    expect(batch([finding({ name: "" })])._tag).toBe("Left")
    expect(batch([finding({ name: "has spaces" })])._tag).toBe("Left")
  })

  it("refuses a relType that is not SCREAMING_SNAKE_CASE", () => {
    expect(batch([{ _tag: "LinkNodes", from: "a", to: "b", relType: "has_finding" }])._tag).toBe(
      "Left"
    )
    expect(batch([{ _tag: "LinkNodes", from: "a", to: "b", relType: "HAS_FINDING" }])._tag).toBe(
      "Right"
    )
  })

  it("bounds the batch size", () => {
    expect(batch([])._tag).toBe("Left")
    expect(batch(Array.from({ length: 51 }, () => finding()))._tag).toBe("Left")
  })
})

// ---------------------------------------------------------------------------

describe("plan — the Cypher it would run", () => {
  const decodeFinding = Schema.decodeUnknownSync(RecordFinding)

  it("MERGEs the finding and attaches it to an existing hub", () => {
    const s = plan(decodeFinding(finding({ hub: "some-hub-2026-08-10" })) as WriteIntent, prov, NOW)
    expect(s.labels).toEqual(["ResearchFinding"])
    expect(s.relTypes).toEqual(["HAS_FINDING"])
    // the hub is MATCHed, never created — that is how orphans are avoided
    expect(s.cypher).toContain("MATCH (h {name: $hub})")
    expect(s.cypher).toContain("MERGE (h)-[:HAS_FINDING]->(f)")
    expect(s.expectNodes).toEqual(["rf-test-2026-08-10"])
  })

  it("flattens the citation union into the property the trigger reads", () => {
    const s = plan(decodeFinding(finding()) as WriteIntent, prov, NOW)
    const props = (s.params["props"] ?? {}) as Record<string, unknown>
    expect(props["citation_url"]).toBe("https://example.org/x")
    expect(props["no_external_citation_reason"]).toBeUndefined()
  })

  it("stamps provenance on every node it writes", () => {
    const s = plan(decodeFinding(finding()) as WriteIntent, prov, NOW)
    const props = (s.params["props"] ?? {}) as Record<string, unknown>
    expect(props["actor"]).toBe("test-agent")
    expect(props["run_id"]).toBe("run-0001")
    expect(props["written_at"]).toBe(NOW)
  })

  it("writes the lakatos mechanism in both the trigger's and the reader's casing", () => {
    const s = plan(
      {
        _tag: "RecordLesson",
        name: "lesson-y-2026-08-10",
        wrongAssumption: "w",
        truth: "t",
        problem: "",
        solution: "",
        lakatos_mechanism: "proof-analysis",
        citation: url
      } as WriteIntent,
      prov,
      NOW
    )
    const props = (s.params["props"] ?? {}) as Record<string, unknown>
    expect(props["lakatos_mechanism"]).toBe("proof-analysis")
    expect(props["lakatosMechanism"]).toBe("proof-analysis")
  })

  it("MATCHes dispatch children instead of conjuring them", () => {
    const s = plan(
      {
        _tag: "RecordDispatch",
        name: "dispatch-1",
        parent: "p",
        children: ["c1", "c2"],
        kind: "explore",
        stoppedBecause: "Dry",
        citation: url
      } as WriteIntent,
      prov,
      NOW
    )
    expect(s.cypher).toContain("MATCH (c {name: childName})")
    expect(s.cypher).not.toContain("MERGE (c {name: childName})")
    expect([...s.relTypes].sort()).toEqual(["DISPATCHED", "PRODUCED_BY"])
  })

  it("LinkNodes matches both endpoints — never merges one into existence", () => {
    const s = plan(
      { _tag: "LinkNodes", from: "a", to: "b", relType: "SUPPORTS" } as WriteIntent,
      prov,
      NOW
    )
    expect(s.cypher).toContain("MATCH (a {name: $from})")
    expect(s.cypher).toContain("MATCH (b {name: $to})")
    expect(s.expectNodes).toEqual([])
  })
})

// ---------------------------------------------------------------------------

describe("SchemaGuard", () => {
  it("rejects what the frozen registry does not list", () => {
    const g = SchemaGuard.make(["ResearchFinding"], ["HAS_FINDING"])
    expect(g.labelAllowed("ResearchFinding")).toBe(true)
    expect(g.labelAllowed("MadeUpLabel")).toBe(false)
    // the real mistake made by hand earlier today: FINDING_OF does not exist
    expect(g.relTypeAllowed("FINDING_OF")).toBe(false)
    expect(g.relTypeAllowed("HAS_FINDING")).toBe(true)
    expect(g.rejectedRelTypes(["HAS_FINDING", "FINDING_OF"])).toEqual(["FINDING_OF"])
  })
})

// ---------------------------------------------------------------------------

describe("KgWritePort — preflight and dry-run", () => {
  const layer = KgWritePortDryOnly.pipe(Layer.provideMerge(SchemaGuardOffline))
  const apply = (batch: unknown) =>
    Effect.runPromise(
      Effect.provide(
        Effect.gen(function* () {
          const w = yield* KgWritePortTag
          return yield* w.apply(Schema.decodeUnknownSync(WriteBatch)(batch))
        }),
        layer
      ).pipe(Effect.either)
    )

  it("plans without committing and shows the Cypher", async () => {
    const r = await apply({ provenance: prov, intents: [finding()] })
    expect(r._tag).toBe("Right")
    if (r._tag !== "Right") return
    expect(r.right.dryRun).toBe(true)
    expect(r.right.committed).toBe(false)
    expect(r.right.planned[0]?.admissible).toBe(true)
    expect(r.right.planned[0]?.cypher).toContain("MERGE (f:ResearchFinding")
  })

  it("refuses the whole batch when one relType is not in the registry", async () => {
    const r = await apply({
      provenance: prov,
      intents: [finding(), { _tag: "LinkNodes", from: "a", to: "b", relType: "TOTALLY_INVENTED" }]
    })
    expect(r._tag).toBe("Left")
    if (r._tag !== "Left") return
    expect(r.left._tag).toBe("BadRequest")
    expect(String(r.left.reason)).toContain("TOTALLY_INVENTED")
  })

  it("refuses to commit when no live KG is configured", async () => {
    const r = await apply({ dryRun: false, provenance: prov, intents: [finding()] })
    expect(r._tag).toBe("Left")
    if (r._tag !== "Left") return
    expect(r.left._tag).toBe("Unavailable")
  })

  it("fails closed when the registry could not be read", async () => {
    const denied = Layer.succeed(SchemaGuardTag, SchemaGuard.make([], []))
    const r = await Effect.runPromise(
      Effect.provide(
        Effect.gen(function* () {
          const w = yield* KgWritePortTag
          return yield* w.apply(Schema.decodeUnknownSync(WriteBatch)({ provenance: prov, intents: [finding()] }))
        }),
        KgWritePortDryOnly.pipe(Layer.provideMerge(denied))
      ).pipe(Effect.either)
    )
    // an agent that cannot verify what is allowed must not guess
    expect(r._tag).toBe("Left")
  })
})
