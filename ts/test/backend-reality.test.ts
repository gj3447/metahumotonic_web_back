import { Either } from "effect"
import { readFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { decodeReality, evidenceState, realityJsonLd, realityPropertyGraph, realityView } from "../src/domain/BackendReality.js"
import { decodeCatalog } from "../src/domain/PlatformGraph.js"

const platform = Either.getOrThrow(decodeCatalog(JSON.parse(readFileSync(new URL("../config/platform-catalog.json", import.meta.url), "utf8"))))
const input = JSON.parse(readFileSync(new URL("../config/backend-reality.json", import.meta.url), "utf8"))
const reality = Either.getOrThrow(decodeReality(input, platform))

describe("backend reality is a bounded evidence graph", () => {
  it("keeps implementation, deployment and expiring operation evidence separate", () => {
    const python = reality.nodes.find((node) => node.id === "component:python-public-http")!
    const ts = reality.nodes.find((node) => node.id === "component:ts-http")!
    const memory = reality.nodes.find((node) => node.id === "component:feedback-memory-fallback")!
    expect(python.runtime).toBe("production-observed")
    expect(evidenceState(python, Date.parse("2026-09-28T00:20:00Z"))).toBe("healthy")
    expect(evidenceState(python, Date.parse("2026-09-28T00:27:01.802420Z"))).toBe("stale")
    expect(ts).toMatchObject({ implementation: "native", runtime: "candidate-only" })
    expect(evidenceState(ts, Date.now())).toBe("unverified")
    expect(memory).toMatchObject({ implementation: "simulation", runtime: "development-only" })
    const counts = realityView(reality, Date.parse("2026-09-29T00:00:00Z")).counts
    expect(counts.evidenceState.healthy ?? 0).toBe(0)
    expect(counts.evidenceState.stale).toBe(5)
  })
  it("rejects invented production proof, dangling references and premature supersession", () => {
    const mutate = (update: (copy: any) => void) => { const copy = structuredClone(input); update(copy); return Either.isLeft(decodeReality(copy, platform)) }
    expect(mutate((copy) => { copy.nodes.find((n: any) => n.id === "component:python-public-http").runtimeEvidence = undefined })).toBe(true)
    expect(mutate((copy) => { copy.nodes.find((n: any) => n.id === "component:feedback-memory-fallback").runtime = "production-observed" })).toBe(true)
    expect(mutate((copy) => { copy.nodes.find((n: any) => n.id === "component:ts-http").platformNodeId = "service:invented" })).toBe(true)
    expect(mutate((copy) => { copy.edges.find((e: any) => e.id === "reality:ts-supersedes-python").status = "ACTIVE" })).toBe(true)
    expect(mutate((copy) => { copy.edges[0].to = "component:missing" })).toBe(true)
  })
  it("exports directed relations without asserting proposals or hiding their status", () => {
    const native = realityPropertyGraph(reality, Date.parse("2026-09-29T00:00:00Z"))
    const proposed = native.relations.find((edge) => edge.uid === "reality:ts-supersedes-python")!
    expect(proposed).toMatchObject({ from_uid: "component:ts-http", to_uid: "component:python-public-http", type: "SUPERSEDES" })
    expect(JSON.parse(proposed.properties.description).status).toBe("PROPOSED")
    const jsonld = realityJsonLd(reality, Date.parse("2026-09-29T00:00:00Z"))
    const statement = jsonld["@graph"].find((node) => node["schema:identifier"] === "reality:ts-supersedes-python")!
    expect(statement).toMatchObject({ "@type": "rdf:Statement", "mh:status": "PROPOSED" })
  })
})
