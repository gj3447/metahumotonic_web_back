import { Either, Schema } from "effect"

export const PlatformId = Schema.String.pipe(Schema.pattern(/^[a-z0-9][a-z0-9:._/-]{0,159}$/))
const Id = PlatformId
export const ObservationTime = Schema.String.pipe(Schema.filter((value) =>
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) &&
  Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0,19) === value.slice(0,19)))
export const Evidence = Schema.Struct({
  source: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(1024)),
  authority: Schema.Literal("USER_PRIMARY", "SYSTEM_DERIVED", "SOURCE_DECLARED", "SECONDARY_AI", "UNSPECIFIED"),
  observedAt: ObservationTime,
  note: Schema.String.pipe(Schema.maxLength(1600))
})
export const PlatformKind = Schema.Literal("program", "repository", "service", "mcp-server", "datastore", "host", "deployment")
export const ProgramCategory = Schema.Literal("product", "research", "tooling", "operations", "knowledge", "portfolio")
/** Lifecycle and integration describe catalog intent, never live health. */
export const platformLifecycles = ["active", "candidate", "historical", "unknown"] as const
export const platformIntegrations = ["native", "federated", "catalogued"] as const
export const PlatformLifecycle = Schema.Literal(...platformLifecycles)
export const PlatformIntegration = Schema.Literal(...platformIntegrations)
export const Deployment = Schema.Struct({
  subjectId: Id, hostId: Id,
  environment: Schema.Literal("production", "development", "unknown"),
  runtime: Schema.Literal("docker", "systemd", "kubernetes", "proxmox"),
  revision: Schema.optional(Schema.String.pipe(Schema.minLength(1), Schema.maxLength(200))),
  image: Schema.optional(Schema.String.pipe(Schema.minLength(1), Schema.maxLength(256)))
})
export const PlatformNode = Schema.Struct({
  id: Id,
  kind: PlatformKind,
  name: Schema.String.pipe(Schema.minLength(1), Schema.maxLength(200)),
  aliases: Schema.optional(Schema.Array(Schema.String.pipe(Schema.minLength(1), Schema.maxLength(200))).pipe(Schema.maxItems(20))),
  description: Schema.String.pipe(Schema.maxLength(2000)),
  lifecycle: PlatformLifecycle,
  integration: PlatformIntegration,
  category: Schema.optional(ProgramCategory),
  ownerRepositoryId: Schema.optional(Id),
  deployment: Schema.optional(Deployment),
  kgUid: Schema.optional(Schema.String.pipe(Schema.maxLength(512))),
  evidence: Schema.Array(Evidence).pipe(Schema.minItems(1), Schema.maxItems(20))
})
export type PlatformNode = typeof PlatformNode.Type
export const PlatformEdge = Schema.Struct({
  id: Id, from: Id, to: Id,
  relation: Schema.Literal("DEPENDS_ON", "IMPLEMENTED_BY", "EXPOSES", "USES", "EXTENDS", "DOCUMENTED_IN", "PART_OF", "DEPLOYMENT_OF", "RUNS_ON"),
  status: Schema.Literal("ACTIVE", "PROPOSED", "RETIRED", "UNSPECIFIED"),
  evidence: Schema.Array(Evidence).pipe(Schema.minItems(1), Schema.maxItems(20))
})
export const PlatformObservation = Schema.Struct({
  id: Id, subjectId: Id, observedAt: ObservationTime, expiresAt: ObservationTime,
  check: Schema.Literal("process", "container-health", "readiness", "http", "backup"),
  outcome: Schema.Literal("healthy", "running", "degraded", "failed", "reachable", "unknown"),
  evidence: Schema.Array(Evidence).pipe(Schema.minItems(1), Schema.maxItems(20))
})
export type PlatformObservation = typeof PlatformObservation.Type
export const PlatformCatalog = Schema.Struct({
  schema: Schema.Literal("metahumotonic/platform-catalog@1"),
  observedAt: ObservationTime,
  nodes: Schema.Array(PlatformNode).pipe(Schema.maxItems(2000)),
  edges: Schema.Array(PlatformEdge).pipe(Schema.maxItems(10000)),
  observations: Schema.optional(Schema.Array(PlatformObservation).pipe(Schema.maxItems(10000)))
})
export type PlatformCatalog = typeof PlatformCatalog.Type

/** Referential integrity and dependency cycles are checked independently of semantic links. */
export const graphProblems = (graph: PlatformCatalog): ReadonlyArray<string> => {
  const problems: string[] = []
  const nodes = new Set<string>(), edges = new Set<string>()
  const byId = new Map(graph.nodes.map((node) => [node.id, node]))
  for (const node of graph.nodes) {
    if (nodes.has(node.id)) problems.push(`duplicate node: ${node.id}`)
    if (node.aliases && new Set(node.aliases).size !== node.aliases.length) problems.push(`duplicate alias: ${node.id}`)
    nodes.add(node.id)
    if (node.category && node.kind !== "program") problems.push(`category on non-program: ${node.id}`)
    if (node.ownerRepositoryId && byId.get(node.ownerRepositoryId)?.kind !== "repository") problems.push(`invalid owner repository: ${node.id}`)
    if ((node.kind === "deployment") !== (node.deployment !== undefined)) problems.push(`deployment metadata mismatch: ${node.id}`)
    if (node.deployment) {
      const subject = byId.get(node.deployment.subjectId)
      if (!subject || !["service", "mcp-server", "datastore"].includes(subject.kind)) problems.push(`invalid deployment subject: ${node.id}`)
      if (byId.get(node.deployment.hostId)?.kind !== "host") problems.push(`invalid deployment host: ${node.id}`)
      for (const [relation, to] of [["DEPLOYMENT_OF", node.deployment.subjectId], ["RUNS_ON", node.deployment.hostId]]) {
        const matches = graph.edges.filter((edge) => edge.from === node.id && edge.relation === relation && edge.status === "ACTIVE")
        if (matches.length !== 1 || matches[0]?.to !== to) problems.push(`inconsistent deployment edge: ${node.id}/${relation}`)
      }
    }
  }
  const dependencies = new Map<string, string[]>()
  for (const edge of graph.edges) {
    if (edges.has(edge.id)) problems.push(`duplicate edge: ${edge.id}`)
    edges.add(edge.id)
    if (!nodes.has(edge.from) || !nodes.has(edge.to)) problems.push(`dangling edge: ${edge.id}`)
    if (edge.relation === "DEPLOYMENT_OF" && (byId.get(edge.from)?.kind !== "deployment" || !["service", "mcp-server", "datastore"].includes(byId.get(edge.to)?.kind ?? ""))) problems.push(`invalid deployment relation: ${edge.id}`)
    if (edge.relation === "RUNS_ON" && (byId.get(edge.from)?.kind !== "deployment" || byId.get(edge.to)?.kind !== "host")) problems.push(`invalid host relation: ${edge.id}`)
    if (edge.relation === "DEPENDS_ON" && edge.status === "ACTIVE") {
      dependencies.set(edge.from, [...(dependencies.get(edge.from) ?? []), edge.to])
    }
  }
  const visited = new Set<string>(), active = new Set<string>()
  const visit = (id: string): void => {
    if (active.has(id)) { problems.push(`dependency cycle: ${id}`); return }
    if (visited.has(id)) return
    active.add(id)
    for (const child of dependencies.get(id) ?? []) visit(child)
    active.delete(id); visited.add(id)
  }
  for (const id of nodes) visit(id)
  const observations = new Set<string>()
  for (const observation of graph.observations ?? []) {
    if (observations.has(observation.id)) problems.push(`duplicate observation: ${observation.id}`)
    observations.add(observation.id)
    if (!nodes.has(observation.subjectId)) problems.push(`unknown observation subject: ${observation.id}`)
    if (Date.parse(observation.expiresAt) <= Date.parse(observation.observedAt)) problems.push(`invalid observation expiry: ${observation.id}`)
    if (Date.parse(observation.observedAt) > Date.parse(graph.observedAt)) problems.push(`observation newer than snapshot: ${observation.id}`)
  }
  return problems
}

export const decodeCatalog = (input: unknown) => Schema.decodeUnknownEither(PlatformCatalog, { onExcessProperty: "error" })(input).pipe(
  Either.flatMap((graph) => {
    const errors = graphProblems(graph)
    return errors.length ? Either.left(new Error(errors.join("; "))) : Either.right(graph)
  })
)

/** Induced, bounded one-hop view; retired/proposed edges require an explicit request. */
export const neighborhood = (graph: PlatformCatalog, id: string, limit: number, includeProposed = false) => {
  const relevant = graph.edges.filter((edge) => (edge.from === id || edge.to === id) &&
    (edge.status === "ACTIVE" || (includeProposed && edge.status === "PROPOSED")))
  const selected = relevant.slice(0, limit)
  const ids = new Set([id, ...selected.flatMap((edge) => [edge.from, edge.to])])
  return { nodes: graph.nodes.filter((node) => ids.has(node.id)), edges: selected, truncated: relevant.length > limit }
}

/** Native property-graph input for USL's property-graph/v2 adapter. No resolver calls. */
export const propertyGraph = (graph: PlatformCatalog) => ({
  nodes: graph.nodes.map((node) => ({ uid: node.id, properties: {
    name: node.name, kind: node.kind, evidence: node.evidence, lifecycle: node.lifecycle, integration: node.integration,
    ...(node.category ? { category: node.category } : {}),
    ...(node.ownerRepositoryId ? { ownerRepositoryId: node.ownerRepositoryId } : {}),
    ...(node.deployment ? { deployment: node.deployment } : {}),
    observations: (graph.observations ?? []).filter((observation) => observation.subjectId === node.id),
    ...(node.aliases ? { aliases: node.aliases } : {}),
    locator: node.kgUid ? `kg://canonical-neo4j/${node.kgUid}`
      : `https://metahumotonic.com/api/platform/v1/neighbors?id=${encodeURIComponent(node.id)}`
  } })),
  relations: graph.edges.map((edge) => ({ uid: edge.id, from_uid: edge.from, to_uid: edge.to, type: edge.relation,
    // USL binds the meaning description. Keep status/provenance inside it too,
    // so ratification/withdrawal is not lost as an unobserved extra property.
    properties: { description: JSON.stringify({ status: edge.status, evidence: edge.evidence }) }
  }))
})
