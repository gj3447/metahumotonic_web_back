import { Schema } from "effect"
import { PlatformIntegration, PlatformKind, PlatformLifecycle, PlatformNode, PlatformObservation, ProgramCategory, type PlatformCatalog } from "./PlatformGraph.js"

export const Freshness = Schema.Literal("fresh", "stale", "not-yet-observed")
export const ObservationView = Schema.Struct({ ...PlatformObservation.fields, freshness: Freshness })
export const InventoryItem = Schema.Struct({ node: PlatformNode, observations: Schema.Array(ObservationView) })
const Counts = Schema.Record({ key: Schema.String, value: Schema.Number })
export const InventorySummary = Schema.Struct({
  counts: Schema.Struct({ nodes: Schema.Number, edges: Schema.Number, observations: Schema.Number }),
  byKind: Counts, byCategory: Counts, observationFreshness: Counts,
  gaps: Schema.Struct({ programsWithoutOwner: Schema.Array(Schema.String), unclassifiedPrograms: Schema.Array(Schema.String),
    deploymentsWithoutObservations: Schema.Array(Schema.String) })
})
export interface InventoryQuery {
  readonly q?: string | undefined
  readonly kind?: typeof PlatformKind.Type | undefined
  readonly category?: typeof ProgramCategory.Type | undefined
  /** Catalog intent filters; these are separate from expiring runtime observations. */
  readonly lifecycle?: typeof PlatformLifecycle.Type | undefined
  readonly integration?: typeof PlatformIntegration.Type | undefined
  readonly owner?: string | undefined
  readonly limit?: number | undefined
  readonly offset?: number | undefined
}

/** Time is supplied by the Effect boundary. Old evidence never becomes a live probe. */
export const observationFreshness = (observation: PlatformObservation, now: number): typeof Freshness.Type =>
  now < Date.parse(observation.observedAt) ? "not-yet-observed" : now >= Date.parse(observation.expiresAt) ? "stale" : "fresh"

const latestObservations = (graph: PlatformCatalog, subjectId: string, now: number) => {
  const latest = new Map<string, PlatformObservation>()
  for (const observation of graph.observations ?? []) {
    if (observation.subjectId !== subjectId) continue
    const previous = latest.get(observation.check)
    if (!previous || Date.parse(observation.observedAt) > Date.parse(previous.observedAt) ||
      (Date.parse(observation.observedAt) === Date.parse(previous.observedAt) && observation.id > previous.id)) latest.set(observation.check, observation)
  }
  return [...latest.values()].sort((a, b) => a.check.localeCompare(b.check)).map((observation) => ({
    ...observation, freshness: observationFreshness(observation, now)
  }))
}
export const inventoryItem = (graph: PlatformCatalog, node: PlatformNode, now: number) => ({ node, observations: latestObservations(graph, node.id, now) })
export const inventory = (graph: PlatformCatalog, query: InventoryQuery, now: number) => {
  const terms = (query.q ?? "").normalize("NFKC").toLocaleLowerCase("en-US").trim().split(/\s+/).filter(Boolean)
  const matches = graph.nodes.filter((node) => {
    if (query.kind && node.kind !== query.kind) return false
    if (query.category && node.category !== query.category) return false
    if (query.lifecycle && node.lifecycle !== query.lifecycle) return false
    if (query.integration && node.integration !== query.integration) return false
    if (query.owner && node.ownerRepositoryId !== query.owner) return false
    const text = [node.id, node.name, ...(node.aliases ?? []), node.description].join(" ").normalize("NFKC").toLocaleLowerCase("en-US")
    return terms.every((term) => text.includes(term))
  }).sort((a, b) => a.id.localeCompare(b.id, "en"))
  const offset = Math.max(0, Math.trunc(query.offset ?? 0)), limit = Math.min(100, Math.max(1, Math.trunc(query.limit ?? 25)))
  const selected = matches.slice(offset, offset + limit)
  return { items: selected.map((node) => inventoryItem(graph, node, now)), total: matches.length, offset, limit,
    nextOffset: offset + selected.length < matches.length ? offset + selected.length : null }
}

/** Coverage gaps are explicit engineering work; no inferred owners or synthetic health. */
export const inventorySummary = (graph: PlatformCatalog, now: number) => {
  const count = (values: ReadonlyArray<string>) => values.reduce<Record<string, number>>((counts, value) => ({ ...counts, [value]: (counts[value] ?? 0) + 1 }), {})
  const observations = graph.nodes.flatMap((node) => latestObservations(graph, node.id, now))
  return {
    counts: { nodes: graph.nodes.length, edges: graph.edges.length, observations: (graph.observations ?? []).length },
    byKind: count(graph.nodes.map((node) => node.kind)),
    byCategory: count(graph.nodes.filter((node) => node.kind === "program").map((node) => node.category ?? "unclassified")),
    observationFreshness: count(observations.map((observation) => observation.freshness)),
    gaps: {
      programsWithoutOwner: graph.nodes.filter((node) => node.kind === "program" && !node.ownerRepositoryId).map((node) => node.id),
      unclassifiedPrograms: graph.nodes.filter((node) => node.kind === "program" && !node.category).map((node) => node.id),
      deploymentsWithoutObservations: graph.nodes.filter((node) => node.kind === "deployment" && !observations.some((o) => o.subjectId === node.id)).map((node) => node.id)
    }
  }
}

const NS = "https://metahumotonic.com/vocab/platform#"
const iri = (id: string, scope = "node") => `urn:metahumotonic:platform:${scope}:${encodeURIComponent(id)}`
const evidence = (id: string, records: PlatformNode["evidence"]) => records.map((record, index) => ({
  "@id": iri(`${id}:${index}`, "evidence"), "@type": "prov:Entity",
  "mh:sourceLocator": record.source, "mh:authority": record.authority,
  "prov:generatedAtTime": { "@value": record.observedAt, "@type": "xsd:dateTime" }, "schema:description": record.note
}))

/** Offline JSON-LD 1.1 + RDF statements + PROV-O. No context fetches or KG writes. */
export const platformJsonLd = (graph: PlatformCatalog, source: "snapshot" | "postgres" = "snapshot") => ({
  "@context": { "@version": 1.1, mh: NS, schema: "https://schema.org/", prov: "http://www.w3.org/ns/prov#",
    rdf: "http://www.w3.org/1999/02/22-rdf-syntax-ns#", xsd: "http://www.w3.org/2001/XMLSchema#" },
  "@id": iri("catalog", "document"), "@type": "schema:Dataset",
  "prov:generatedAtTime": { "@value": graph.observedAt, "@type": "xsd:dateTime" },
  "mh:source": source, "mh:visibility": "internal",
  "@graph": [
    ...graph.nodes.map((node) => ({
      "@id": iri(node.id), "@type": ["prov:Entity", `mh:${node.kind}`],
      "schema:identifier": node.id, "schema:name": node.name, "schema:description": node.description,
      "schema:alternateName": node.aliases ?? [], "mh:lifecycle": node.lifecycle, "mh:integration": node.integration,
      ...(node.category ? { "mh:category": node.category } : {}),
      ...(node.ownerRepositoryId ? { "mh:ownerRepository": { "@id": iri(node.ownerRepositoryId) } } : {}),
      ...(node.kgUid ? { "mh:sourceKnowledgeRecord": node.kgUid } : {}),
      ...(node.deployment ? { "mh:environment": node.deployment.environment, "mh:runtime": node.deployment.runtime,
        "mh:deploymentSubject": { "@id": iri(node.deployment.subjectId) }, "mh:host": { "@id": iri(node.deployment.hostId) },
        ...(node.deployment.revision ? { "mh:revision": node.deployment.revision } : {}),
        ...(node.deployment.image ? { "mh:image": node.deployment.image } : {}) } : {}),
      "prov:wasDerivedFrom": evidence(`node:${node.id}`, node.evidence)
    })),
    ...graph.edges.map((edge) => ({
      "@id": iri(edge.id, "edge"), "@type": "rdf:Statement", "schema:identifier": edge.id,
      "rdf:subject": { "@id": iri(edge.from) }, "rdf:predicate": { "@id": `${NS}${edge.relation}` },
      "rdf:object": { "@id": iri(edge.to) }, "mh:status": edge.status,
      // Reification describes the claim. It does not assert even a PROPOSED/RETIRED triple.
      "prov:wasDerivedFrom": evidence(`edge:${edge.id}`, edge.evidence)
    })),
    ...(graph.observations ?? []).map((observation) => ({
      "@id": iri(observation.id, "observation"), "@type": ["prov:Entity", "mh:Observation"], "schema:identifier": observation.id,
      "mh:subject": { "@id": iri(observation.subjectId) }, "mh:check": observation.check, "mh:outcome": observation.outcome,
      "prov:generatedAtTime": { "@value": observation.observedAt, "@type": "xsd:dateTime" },
      "mh:expiresAt": { "@value": observation.expiresAt, "@type": "xsd:dateTime" },
      "prov:wasDerivedFrom": evidence(`observation:${observation.id}`, observation.evidence)
    }))
  ]
})
