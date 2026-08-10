/**
 * The Cypher, copied verbatim from `app/kg.py`.
 *
 * Kept in its own module with no imports so the two services provably ask the
 * graph the same questions — a diff between this file and the Python constants
 * is a one-screen review, not an archaeology exercise.
 *
 * There is no string interpolation anywhere here. Every variable part is a
 * `$bind` parameter, which is what makes the public `/api/kg/read` proxy
 * injection-safe.
 */

export const STATS = `
CALL db.labels() YIELD label
WITH count(label) AS labels
CALL db.relationshipTypes() YIELD relationshipType
WITH labels, count(relationshipType) AS relTypes
MATCH (n) WITH labels, relTypes, count(n) AS nodes
MATCH ()-[r]->() WITH labels, relTypes, nodes, count(r) AS rels
OPTIONAL MATCH (d:DomainHub)
RETURN labels, relTypes, nodes, rels, count(d) AS domains
`

export const DOMAINS = `
MATCH (d:DomainHub)
RETURN d.name AS name, d.displayName AS displayName,
       d.nodeCount AS nodeCount, coalesce(d.description, '') AS description
ORDER BY d.nodeCount DESC
`

/** Single-label count(n) hits Neo4j's count-store (O(1)); chaining one WITH per
 *  label keeps the whole summary to a single cheap row. */
export const RESEARCH_SUMMARY = `
MATCH (n:ResearchFinding) WITH count(n) AS findings
MATCH (l:Lesson) WITH findings, count(l) AS lessons
MATCH (p:Paper) WITH findings, lessons, count(p) AS papers
MATCH (v:ValidationResult) WITH findings, lessons, papers, count(v) AS validations
MATCH (c:Consensus) WITH findings, lessons, papers, validations, count(c) AS consensus
MATCH (d:DecisionLog)
  WITH findings, lessons, papers, validations, consensus, count(d) AS decisions
MATCH (a:Apostle)
  WITH findings, lessons, papers, validations, consensus, decisions, count(a) AS apostles
MATCH (dh:DomainHub)
RETURN findings, lessons, papers, validations, consensus, decisions, apostles,
       count(dh) AS domains
`

export const FINDINGS = `
MATCH (n:ResearchFinding)
WHERE ($cycle = '' OR n.cycle_id = $cycle)
  AND n.name IS NOT NULL
WITH n, coalesce(toString(n.created_at), toString(n.createdAt),
                 toString(n.timestamp), '') AS ts
WHERE coalesce(n.finding, n.claim, n.description, n.summary, n.body, '') <> ''
RETURN n.name AS name,
       coalesce(n.finding, n.claim, n.description, n.summary, n.body, '') AS finding,
       coalesce(n.axis, '') AS axis,
       coalesce(n.sub_axis, n.subAxis, '') AS subAxis,
       n.confidence AS confidence,
       coalesce(n.cycle_id, '') AS cycleId,
       n.verified AS verified,
       coalesce(n.lakatos_mechanism, '') AS lakatosMechanism,
       coalesce(n.citation_url, '') AS citationUrl,
       ts AS createdAt
ORDER BY ts DESC, name
SKIP $offset LIMIT $limit
`

export const LESSONS = `
MATCH (l:Lesson)
WHERE l.name IS NOT NULL
WITH l, coalesce(toString(l.createdAt), toString(l.created_at), '') AS ts
RETURN l.name AS name,
       coalesce(l.problem, '') AS problem,
       coalesce(l.solution, '') AS solution,
       coalesce(l.wrongAssumption, '') AS wrongAssumption,
       coalesce(l.truth, '') AS truth,
       coalesce(l.category, '') AS category,
       coalesce(l.severity, '') AS severity,
       coalesce(l.lakatos_mechanism, '') AS lakatosMechanism,
       ts AS createdAt
ORDER BY ts DESC, name
SKIP $offset LIMIT $limit
`

export const PAPERS = `
MATCH (p:Paper)
WHERE ($domain = '' OR p.domain = $domain)
  AND coalesce(p.title, p.name) IS NOT NULL
RETURN coalesce(p.title, p.name) AS title,
       coalesce(p.author, '') AS author,
       p.year AS year,
       coalesce(p.journal, '') AS journal,
       coalesce(p.doi, '') AS doi,
       coalesce(p.domain, '') AS domain,
       coalesce(p.core_thesis, p.key_insight, p.description, '') AS coreThesis,
       coalesce(p.status, '') AS status
ORDER BY coalesce(p.year, 0) DESC, title
SKIP $offset LIMIT $limit
`

/** Note: no SKIP — the Python getter takes `limit` only. Kept identical. */
export const CONSENSUS = `
MATCH (c:Consensus)
WHERE c.name IS NOT NULL
WITH c, coalesce(toString(c.created_at), toString(c.createdAt), '') AS ts
RETURN c.name AS name,
       coalesce(c.summary, c.description, c.statement, c.body, '') AS summary,
       ts AS createdAt
ORDER BY ts DESC, name
LIMIT $limit
`

/** Returns ONE row: `degree` plus a capped `neighbors` list. */
export const NEIGHBORS = `
MATCH (n {name: $name})
WITH n LIMIT 1
WITH n, COUNT { (n)--() } AS degree
CALL {
  WITH n
  MATCH (n)-[r]->(m)
  RETURN 'out' AS direction, type(r) AS rtype, m.name AS mname, labels(m) AS lbls
  UNION ALL
  WITH n
  MATCH (n)<-[r]-(m)
  RETURN 'in' AS direction, type(r) AS rtype, m.name AS mname, labels(m) AS lbls
}
WITH degree, collect({direction: direction, type: rtype,
                      name: coalesce(mname, ''), labels: lbls})[0..$limit] AS neighbors
RETURN degree, neighbors
`

export const PING = `RETURN 1 AS ok`
