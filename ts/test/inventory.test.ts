import { Either } from "effect"
import { readFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { decodeCatalog, type PlatformCatalog, type PlatformObservation } from "../src/domain/PlatformGraph.js"
import { inventory, inventoryItem, inventorySummary, observationFreshness, platformJsonLd } from "../src/domain/PlatformInventory.js"

const input = JSON.parse(readFileSync(new URL("../config/platform-catalog.json", import.meta.url), "utf8"))
const graph = Either.getOrThrow(decodeCatalog(input))
const later = Date.parse("2030-01-01T00:00:00Z")

describe("company inventory coverage and provenance", () => {
  it("represents every audited primary checkout, VM/CT and Docker instance without treating reference clones as company programs", () => {
    const workspace = JSON.parse(readFileSync(new URL("../../docs/evidence/workspace-inventory-2026-09-27.json", import.meta.url), "utf8"))
    const runtime = JSON.parse(readFileSync(new URL("../../docs/evidence/workspace-runtime-survey-2026-09-27.json", import.meta.url), "utf8"))
    const names = new Set(graph.nodes.filter((node) => node.kind === "repository").map((node) => node.name))
    for (const repo of workspace.repositories as Array<{ path: string }>) {
      const relative = repo.path.replace("/home/lagyeongjun/CD/", "")
      if (!relative.includes("/")) expect(names.has(relative), relative).toBe(true)
    }
    for (const vm of runtime.fleet.checks.fleet.result as Array<{ name: string }>) expect(graph.nodes.some((n) => n.kind === "host" && n.id === `host:${vm.name}`)).toBe(true)
    for (const [key, host] of [["edgeContainers", "vm100"], ["dataContainers", "vm200"]] as const) {
      for (const line of (runtime.fleet.checks[key].result["out-data"] as string).trim().split("\n")) {
        expect(graph.nodes.some((n) => n.id === `deployment:${host}:${line.split("\t")[0]}`)).toBe(true)
      }
    }
    expect(graph.nodes.some((n) => n.kind === "program" && /HippoRAG|graphiti/.test(n.name))).toBe(false)
    expect(graph.nodes.find((n) => n.id === "program:hswm")?.kgUid).toBe("sym:Concept:hswm")
    const summary = inventorySummary(graph, later)
    expect(summary.gaps).toEqual({ programsWithoutOwner: ["program:bitcoin", "program:crypto-kg-studio"], unclassifiedPrograms: [], deploymentsWithoutObservations: [] })
    expect(summary.observationFreshness).toEqual({ stale: graph.observations!.length })
  })
  it("searches Korean aliases and owner/category intersections with stable bounded pagination", () => {
    expect(inventory(graph, { q: "버엑시", kind: "program" }, later).items.map((i) => i.node.id)).toContain("program:virtual-excel")
    expect(inventory(graph, { q: "Virtual Excel Simulator", kind: "program" }, later).items.map((i) => i.node.id)).toEqual(["program:virtual-excel"])
    const games = inventory(graph, { owner: "repository:game", category: "product", limit: 100 }, later)
    expect(games.total).toBe(13)
    expect(games.items.every((i) => i.node.ownerRepositoryId === "repository:game" && i.node.category === "product")).toBe(true)
    const ids: string[] = []
    let offset: number | null = 0
    while (offset !== null) {
      const page = inventory(graph, { limit: 7, offset }, later)
      ids.push(...page.items.map((i) => i.node.id)); offset = page.nextOffset
    }
    expect(new Set(ids).size).toBe(graph.nodes.length)
    expect(ids).toHaveLength(graph.nodes.length)
    expect(inventory(graph, { offset: 2000 }, later)).toMatchObject({ items: [], nextOffset: null })
  })
  it("keeps lifecycle and integration filters independent from expiring runtime observations", () => {
    const activeNative = inventory(graph, { lifecycle: "active", integration: "native", limit: 100 }, later)
    expect(activeNative.items).not.toHaveLength(0)
    expect(activeNative.items.every(({ node }) => node.lifecycle === "active" && node.integration === "native")).toBe(true)
    expect(activeNative.items.some(({ node }) => node.id === "program:company-backend")).toBe(true)
    const candidates = inventory(graph, { lifecycle: "candidate", kind: "program", limit: 100 }, later)
    expect(candidates.items).not.toHaveLength(0)
    expect(candidates.items.every(({ node }) => node.lifecycle === "candidate" && node.kind === "program")).toBe(true)
    expect(candidates.items.some(({ node }) => node.id === "program:website")).toBe(true)
  })
  it("does not reinterpret the old TS runtime's successful HTTP status as healthy readiness", () => {
    const node = graph.nodes.find((n) => n.id === "deployment:runtime-01:mhb-ts")!
    const result = inventoryItem(graph, node, later)
    expect(result.node.deployment?.subjectId).toBe("service:web-back-ts-runtime")
    expect(result.observations).toEqual(expect.arrayContaining([
      expect.objectContaining({ check: "process", outcome: "running", freshness: "stale" }),
      expect.objectContaining({ check: "readiness", outcome: "degraded", freshness: "stale" })
    ]))
    expect(graph.nodes.find((n) => n.id === "deployment:vm100:web-back-pve-1")?.deployment?.subjectId).toBe("service:web-back-python")
  })
})

describe("time, identity and deployment contracts", () => {
  const observation: PlatformObservation = { ...graph.observations![0]!, observedAt: "2026-09-27T06:00:00Z", expiresAt: "2026-09-27T06:01:00Z" }
  it("expires at the boundary and refuses to present future observations as fresh", () => {
    expect(observationFreshness(observation, Date.parse(observation.observedAt) - 1)).toBe("not-yet-observed")
    expect(observationFreshness(observation, Date.parse(observation.observedAt))).toBe("fresh")
    expect(observationFreshness(observation, Date.parse(observation.expiresAt) - 1)).toBe("fresh")
    expect(observationFreshness(observation, Date.parse(observation.expiresAt))).toBe("stale")
  })
  it("selects the latest observation numerically, including fractional seconds", () => {
    const old = { ...observation, id: "obs:old", observedAt: "2026-09-27T06:00:00Z" }
    const newest = { ...observation, id: "obs:new", observedAt: "2026-09-27T06:00:00.100Z", outcome: "failed" as const }
    const node = graph.nodes.find((n) => n.id === observation.subjectId)!
    const sample: PlatformCatalog = { ...graph, observations: [newest, old] }
    expect(inventoryItem(sample, node, later).observations[0]?.id).toBe("obs:new")
  })
  it("rejects malformed times, invented owners and inconsistent deployment references", () => {
    const deployment = graph.nodes.find((n) => n.kind === "deployment")!
    const replace = (value: unknown) => ({ ...graph, nodes: graph.nodes.map((n) => n.id === deployment.id ? value : n) })
    for (const changes of [{ deployment: undefined }, { deployment: { ...deployment.deployment, hostId: "program:hswm" } },
      { deployment: { ...deployment.deployment, subjectId: "service:hspine" } }, { ownerRepositoryId: "program:hswm" }]) {
      expect(Either.isLeft(decodeCatalog(replace({ ...deployment, ...changes })))).toBe(true)
    }
    for (const changes of [{ observedAt: "2026-02-30T00:00:00Z" }, { observedAt: "not-a-time" },
      { expiresAt: observation.observedAt }, { subjectId: "missing" }, { observedAt: "2030-01-01T00:00:00Z", expiresAt: "2030-01-02T00:00:00Z" }]) {
      expect(Either.isLeft(decodeCatalog({ ...graph, observations: [{ ...observation, ...changes }] }))).toBe(true)
    }
    expect(Either.isLeft(decodeCatalog({ ...graph, observations: [observation, observation] }))).toBe(true)
  })
  it("retains direction, non-active status, observations and evidence in offline JSON-LD", () => {
    const output = platformJsonLd(graph)
    expect(output["@context"]["@version"]).toBe(1.1)
    const edge = output["@graph"].find((n) => n["schema:identifier"] === "exposes:maplelineage") as Record<string, unknown>
    expect(edge).toMatchObject({ "@type": "rdf:Statement", "mh:status": "PROPOSED",
      "rdf:subject": { "@id": expect.stringContaining(encodeURIComponent("program:company-backend")) },
      "rdf:object": { "@id": expect.stringContaining(encodeURIComponent("mcp:maplelineage")) } })
    expect(edge["prov:wasDerivedFrom"]).toBeDefined()
    expect(output["@graph"].filter((n) => Array.isArray(n["@type"]) && n["@type"].includes("mh:Observation"))).toHaveLength(graph.observations!.length)
  })
})
