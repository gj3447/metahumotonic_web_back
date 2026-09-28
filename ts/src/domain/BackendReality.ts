import { Either, Schema } from "effect"
import type { PlatformCatalog } from "./PlatformGraph.js"

const Id = Schema.String.pipe(Schema.pattern(/^[a-z0-9][a-z0-9:._/-]{0,159}$/))
const Time = Schema.String.pipe(Schema.filter((value) => Number.isFinite(Date.parse(value)) && /^\d{4}-\d{2}-\d{2}T/.test(value)))
const Evidence = Schema.Struct({
  source: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(1024)),
  kind: Schema.Literal("source", "document", "test", "runtime")
})
const RuntimeEvidence = Schema.Struct({
  source: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(1024)),
  observedAt: Time, expiresAt: Time,
  outcome: Schema.Literal("healthy", "reachable", "degraded", "failed")
})
export const RealityNode = Schema.Struct({
  id: Id,
  name: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(200)),
  implementation: Schema.Literal("native", "federated", "snapshot", "simulation", "missing", "retired"),
  runtime: Schema.Literal("production-observed", "production-unverified", "candidate-only", "not-configured", "development-only", "retired", "unknown"),
  decision: Schema.Literal("retain", "complete", "retire-after-gate", "archive"),
  platformNodeId: Schema.optional(Id),
  evidence: Schema.Array(Evidence).pipe(Schema.minItems(1), Schema.maxItems(20)),
  runtimeEvidence: Schema.optional(Schema.Array(RuntimeEvidence).pipe(Schema.minItems(1), Schema.maxItems(20))),
  finding: Schema.optional(Schema.String.pipe(Schema.maxLength(2000))),
  nextGate: Schema.optional(Schema.String.pipe(Schema.minLength(1), Schema.maxLength(2000)))
})
export type RealityNode = typeof RealityNode.Type
export const RealityEdge = Schema.Struct({
  id: Id, from: Id, to: Id,
  relation: Schema.Literal("USES", "READS", "EXPOSES", "DELEGATES_TO", "SUPERSEDES", "PRODUCES"),
  status: Schema.Literal("ACTIVE", "PROPOSED", "RETIRED"),
  meaning: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(1000)),
  evidence: Schema.Array(Evidence).pipe(Schema.minItems(1), Schema.maxItems(20))
})
export const BackendReality = Schema.Struct({
  schema: Schema.Literal("metahumotonic/backend-reality@1"),
  recordedAt: Time,
  scope: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(1000)),
  nodes: Schema.Array(RealityNode).pipe(Schema.minItems(1), Schema.maxItems(200)),
  edges: Schema.Array(RealityEdge).pipe(Schema.maxItems(1000))
})
export type BackendReality = typeof BackendReality.Type

/** Classifications are evidence claims, separate from asset identity and permission. */
export const realityProblems = (graph: BackendReality, platform: PlatformCatalog): ReadonlyArray<string> => {
  const problems: string[] = []
  const nodes = new Map<string, RealityNode>()
  const platformIds = new Set(platform.nodes.map((node) => node.id))
  for (const node of graph.nodes) {
    if (nodes.has(node.id)) problems.push(`duplicate reality node: ${node.id}`)
    nodes.set(node.id, node)
    if (node.platformNodeId && !platformIds.has(node.platformNodeId)) problems.push(`unknown platform reference: ${node.id}`)
    if (node.runtime === "production-observed" && !node.runtimeEvidence?.length) problems.push(`production claim without runtime evidence: ${node.id}`)
    if (node.runtime !== "production-observed" && node.runtimeEvidence?.length) problems.push(`runtime proof on unobserved node: ${node.id}`)
    if (node.implementation === "simulation" && node.runtime !== "development-only") problems.push(`simulation promoted beyond development: ${node.id}`)
    if (node.implementation === "retired" && (node.runtime !== "retired" || node.decision !== "archive")) problems.push(`retired classification mismatch: ${node.id}`)
    if (node.implementation === "missing" && node.runtime === "production-observed") problems.push(`missing implementation claims production: ${node.id}`)
    if (["complete", "retire-after-gate"].includes(node.decision) && !node.nextGate) problems.push(`missing completion gate: ${node.id}`)
    for (const observation of node.runtimeEvidence ?? []) {
      const observedAt = Date.parse(observation.observedAt), expiresAt = Date.parse(observation.expiresAt)
      if (observedAt > Date.parse(graph.recordedAt) || expiresAt <= observedAt || expiresAt - observedAt > 900_000) problems.push(`invalid runtime evidence clock: ${node.id}`)
      if (!observation.source.startsWith("receipt:")) problems.push(`runtime evidence lacks receipt: ${node.id}`)
    }
  }
  const edgeIds = new Set<string>()
  for (const edge of graph.edges) {
    if (edgeIds.has(edge.id)) problems.push(`duplicate reality edge: ${edge.id}`)
    edgeIds.add(edge.id)
    if (!nodes.has(edge.from) || !nodes.has(edge.to)) problems.push(`dangling reality edge: ${edge.id}`)
    if (edge.status === "ACTIVE" && (nodes.get(edge.from)?.runtime === "retired" || nodes.get(edge.to)?.runtime === "retired")) problems.push(`active retired edge: ${edge.id}`)
    if (edge.relation === "SUPERSEDES" && edge.status === "ACTIVE" && nodes.get(edge.from)?.runtime !== "production-observed") problems.push(`unproved supersession: ${edge.id}`)
  }
  return problems
}

export const decodeReality = (input: unknown, platform: PlatformCatalog) =>
  Schema.decodeUnknownEither(BackendReality, { onExcessProperty: "error" })(input).pipe(Either.flatMap((graph) => {
    const problems = realityProblems(graph, platform)
    return problems.length ? Either.left(new Error(problems.join("; "))) : Either.right(graph)
  }))

export type EvidenceState = "healthy" | "reachable" | "degraded" | "failed" | "stale" | "unverified"
export const RealityView = Schema.Struct({
  schema: BackendReality.fields.schema,
  recordedAt: BackendReality.fields.recordedAt,
  scope: BackendReality.fields.scope,
  digest: Schema.String,
  evaluatedAt: Schema.String,
  nodes: Schema.Array(Schema.Struct({ ...RealityNode.fields,
    evidenceState: Schema.Literal("healthy", "reachable", "degraded", "failed", "stale", "unverified") })),
  edges: Schema.Array(RealityEdge),
  counts: Schema.Struct({
    implementation: Schema.Record({ key: Schema.String, value: Schema.Number }),
    runtime: Schema.Record({ key: Schema.String, value: Schema.Number }),
    decision: Schema.Record({ key: Schema.String, value: Schema.Number }),
    evidenceState: Schema.Record({ key: Schema.String, value: Schema.Number })
  })
})
/** An expired observation never proves current operation. Code and test evidence cannot substitute for it. */
export const evidenceState = (node: RealityNode, now: number): EvidenceState => {
  const latest = [...(node.runtimeEvidence ?? [])].sort((a, b) => Date.parse(b.observedAt) - Date.parse(a.observedAt))[0]
  if (!latest) return "unverified"
  if (now < Date.parse(latest.observedAt)) return "unverified"
  if (now >= Date.parse(latest.expiresAt)) return "stale"
  return latest.outcome
}
export const realityView = (graph: BackendReality, now: number) => {
  const nodes = graph.nodes.map((node) => ({ ...node, evidenceState: evidenceState(node, now) }))
  const count = (values: ReadonlyArray<string>) => values.reduce<Record<string, number>>((result, value) => {
    result[value] = (result[value] ?? 0) + 1
    return result
  }, {})
  return {
    schema: graph.schema, recordedAt: graph.recordedAt, scope: graph.scope,
    evaluatedAt: new Date(now).toISOString(), nodes, edges: graph.edges,
    counts: { implementation: count(nodes.map((node) => node.implementation)), runtime: count(nodes.map((node) => node.runtime)),
      decision: count(nodes.map((node) => node.decision)), evidenceState: count(nodes.map((node) => node.evidenceState)) }
  }
}

/** Native USL property-graph/v2 projection; status remains a declared meaning, never execution authority. */
export const realityPropertyGraph = (graph: BackendReality, now: number) => ({
  nodes: graph.nodes.map((node) => ({ uid: node.id, properties: {
    name: node.name, implementation: node.implementation, runtime: node.runtime, decision: node.decision,
    evidenceState: evidenceState(node, now), evidence: node.evidence, runtimeEvidence: node.runtimeEvidence ?? [],
    ...(node.platformNodeId ? { platformNodeId: node.platformNodeId } : {}),
    locator: `https://metahumotonic.com/api/platform/v1/reality#${encodeURIComponent(node.id)}`
  } })),
  relations: graph.edges.map((edge) => ({ uid: edge.id, from_uid: edge.from, to_uid: edge.to, type: edge.relation,
    properties: { description: JSON.stringify({ meaning: edge.meaning, status: edge.status, evidence: edge.evidence }) } }))
})

const NS = "https://metahumotonic.com/vocab/backend-reality#"
const iri = (id: string, scope = "node") => `urn:metahumotonic:backend-reality:${scope}:${encodeURIComponent(id)}`
/** Inline JSON-LD; relations are reified so PROPOSED and RETIRED are not asserted as facts. */
export const realityJsonLd = (graph: BackendReality, now: number) => ({
  "@context": { "@version": 1.1, mh: NS, schema: "https://schema.org/", prov: "http://www.w3.org/ns/prov#",
    rdf: "http://www.w3.org/1999/02/22-rdf-syntax-ns#", xsd: "http://www.w3.org/2001/XMLSchema#" },
  "@id": iri("assessment", "document"), "@type": "schema:Dataset",
  "prov:generatedAtTime": { "@value": graph.recordedAt, "@type": "xsd:dateTime" },
  "mh:evaluatedAt": { "@value": new Date(now).toISOString(), "@type": "xsd:dateTime" },
  "mh:visibility": "internal",
  "@graph": [
    ...graph.nodes.map((node) => ({
      "@id": iri(node.id), "@type": ["prov:Entity", "mh:Component"],
      "schema:identifier": node.id, "schema:name": node.name,
      "mh:implementation": node.implementation, "mh:runtime": node.runtime, "mh:decision": node.decision,
      "mh:evidenceState": evidenceState(node, now),
      ...(node.platformNodeId ? { "mh:platformReference": { "@id": `urn:metahumotonic:platform:node:${encodeURIComponent(node.platformNodeId)}` } } : {}),
      "prov:wasDerivedFrom": node.evidence.map((e, index) => ({ "@id": iri(`${node.id}:${index}`, "evidence"),
        "@type": "prov:Entity", "mh:sourceLocator": e.source, "mh:evidenceKind": e.kind })),
      "mh:runtimeEvidence": (node.runtimeEvidence ?? []).map((e, index) => ({ "@id": iri(`${node.id}:${index}`, "runtime"),
        "@type": "mh:RuntimeEvidence", "mh:sourceLocator": e.source, "mh:outcome": e.outcome,
        "prov:generatedAtTime": { "@value": e.observedAt, "@type": "xsd:dateTime" },
        "mh:expiresAt": { "@value": e.expiresAt, "@type": "xsd:dateTime" } }))
    })),
    ...graph.edges.map((edge) => ({
      "@id": iri(edge.id, "edge"), "@type": "rdf:Statement", "schema:identifier": edge.id,
      "rdf:subject": { "@id": iri(edge.from) }, "rdf:predicate": { "@id": `${NS}${edge.relation}` },
      "rdf:object": { "@id": iri(edge.to) }, "mh:status": edge.status, "schema:description": edge.meaning,
      "prov:wasDerivedFrom": edge.evidence.map((e, index) => ({ "@id": iri(`${edge.id}:${index}`, "evidence"),
        "@type": "prov:Entity", "mh:sourceLocator": e.source, "mh:evidenceKind": e.kind }))
    }))
  ]
})
