import { readFileSync } from "node:fs"
import { ConfigProvider, Effect, Either } from "effect"
import { HttpApiBuilder } from "@effect/platform"
import { describe, expect, it } from "vitest"
import { decodeHub, projectHub, hubIri, publicUrl } from "../src/domain/LearningHub.js"
import { configFromEnv } from "../src/Config.js"
import { configOf, webHandlerLayer } from "../src/server/Composition.js"

const raw = JSON.parse(readFileSync(new URL("../config/learning-hub.json", import.meta.url), "utf8"))
const hub = Either.getOrThrow(decodeHub(raw))
describe("public learning graph", () => {
  it.each([
    ["definition must start at an article", (value: typeof raw) => { value.edges[0].from = "usl" }],
    ["source code must point at a repository", (value: typeof raw) => { value.edges.find((edge: { relation: string }) => edge.relation === "SOURCE_CODE").to = "book" }],
    ["publication must start at a channel", (value: typeof raw) => { value.edges.find((edge: { relation: string }) => edge.relation === "PUBLISHES").from = "hswm" }],
    ["self relations are not reading steps", (value: typeof raw) => { value.edges[0].to = value.edges[0].from }],
    ["a video cannot identify a GitHub repository", (value: typeof raw) => { value.nodes.find((node: { kind: string }) => node.kind === "video").href = "https://github.com/gj3447/HSWM" }],
    ["a repository cannot identify a user profile", (value: typeof raw) => { value.nodes.find((node: { kind: string }) => node.kind === "repository").href = "https://github.com/gj3447" }],
    ["impossible calendar dates are rejected", (value: typeof raw) => { value.edition = "2026-02-30" }],
    ["review date cannot follow its publication", (value: typeof raw) => { value.nodes[0].reviewedAt = "2027-01-01" }],
    ["a reading path cannot repeat a step", (value: typeof raw) => { value.paths[0].steps.push(value.paths[0].steps[0]) }],
    ["duplicate evidence is rejected", (value: typeof raw) => { value.nodes[0].sources.push(value.nodes[0].sources[0]) }]
  ] as const)("%s", (_name, mutate) => {
    const changed = structuredClone(raw)
    mutate(changed)
    expect(Either.isLeft(decodeHub(changed))).toBe(true)
  })
  it("allows genuine semantic cycles without treating reading links as an execution DAG", () => {
    const sample = raw.edges.find((edge: { relation: string }) => edge.relation === "EXPLORE_NEXT")
    expect(Either.isRight(decodeHub({ ...raw, edges: [...raw.edges,
      { ...sample, id: "read-cycle-a", from: "good", to: "freedom" },
      { ...sample, id: "read-cycle-b", from: "freedom", to: "good" }
    ] }))).toBe(true)
  })
  it("does not normalize a reviewed locator into a different address", () => {
    for (const url of ["https://metahumotonic.com:8443/wiki/axioms/", " https://metahumotonic.com/wiki/axioms/", "https://metahumotonic.com/api/../wiki/axioms/", "https://github.com:443/gj3447/HSWM"])
      expect(publicUrl(url)).toBe(false)
  })
  it("rejects private fields, non-public records, unsafe locators and broken identities before publishing", () => {
    for (const patch of [{ ...raw, password: "secret" }, { ...raw, nodes: [{ ...raw.nodes[0], public: false }] },
      { ...raw, nodes: [...raw.nodes, raw.nodes[0]] }, { ...raw, edges: [{ ...raw.edges[0], to: "missing" }] },
      { ...raw, paths: [{ ...raw.paths[0], steps: ["missing"] }] }]) expect(Either.isLeft(decodeHub(patch))).toBe(true)
    for (const url of ["file:///private", "https://metahumotonic.com/api/platform/v1/graph", "https://www.youtube.com/watch?v=smwfb9vYG10&token=secret", "https://user:secret@github.com/gj3447/HSWM", "https://github.com.evil.test/gj3447/HSWM"]) expect(publicUrl(url)).toBe(false)
  })
  it("preserves identity, direction, status and provenance between JSON-LD and native USL export", () => {
    const projection = projectHub(hub)
    expect(projection.jsonld["mh:sourceDigest"]).toBe(projection.sourceDigest)
    expect(projection.usl.nodes.map((node) => node.uid)).toEqual(hub.nodes.map((node) => hubIri(node.id)))
    for (const edge of hub.edges) {
      const relation = projection.usl.relations.find((item) => item.uid.endsWith(`#edge-${edge.id}`))!
      expect(relation.from_uid).toBe(hubIri(edge.from)); expect(relation.to_uid).toBe(hubIri(edge.to))
      expect(JSON.parse(relation.properties.description)).toMatchObject({ status: edge.status, authority: edge.authority, source: edge.source })
    }
    const proposed = projectHub({ ...hub, edges: [{ ...hub.edges[0]!, status: "PROPOSED" }] })
    const article = proposed.jsonld["@graph"].find((node) => node["@id"] === hubIri(hub.edges[0]!.from))!
    expect(JSON.stringify(article)).not.toContain('relation-defines')
    expect(proposed.usl.relations[0]!.properties.description).toContain("PROPOSED")
  })
  it("serves the curated graph without a key while the company platform stays private", async () => {
    const config = Effect.runSync(configFromEnv.pipe(Effect.withConfigProvider(ConfigProvider.fromMap(new Map()))))
    const web = HttpApiBuilder.toWebHandler(webHandlerLayer({ config: configOf(config) }))
    try {
      for (const [path, type] of [["/api/public/v1/hub", "application/json"], ["/api/public/v1/hub/graph.jsonld", "application/ld+json"], ["/api/public/v1/hub/usl.json", "application/json"]]) {
        const res = await web.handler(new Request(`http://localhost${path}`))
        expect(res.status).toBe(200); expect(res.headers.get("content-type")).toContain(type)
        expect(res.headers.get("x-hub-source-digest")).toBe(projectHub(hub).sourceDigest)
        expect(await res.text()).not.toMatch(/\/home\/|kg:\/\/|datastore:|MHB_|PRIVATE_THREAD|bolt:/)
      }
      const privateGraph = await web.handler(new Request("http://localhost/api/platform/v1/graph"))
      expect([401, 503]).toContain(privateGraph.status)
    } finally { await web.dispose() }
  })
})
