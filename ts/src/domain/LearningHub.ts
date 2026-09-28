import { Either, Schema } from "effect"
import { createHash } from "node:crypto"

const Text = Schema.String.pipe(Schema.minLength(1), Schema.maxLength(1200))
const Id = Schema.String.pipe(Schema.pattern(/^[a-z][a-z0-9-]{0,79}$/))
const Authority = Schema.Literal("PRIMARY_SOURCE", "EDITORIAL_SUMMARY")
export const publicUrl = (value: string): boolean => {
  try {
    const url = new URL(value)
    // Do not silently normalize ports, dot segments, credentials or whitespace
    // in reviewed public locators. The bytes reviewed are the bytes published.
    if (url.href !== value || url.protocol !== "https:" || url.port || url.username || url.password) return false
    if (url.hostname === "metahumotonic.com") {
      const path = /^\/(?:wiki\/(?:axioms|authority|worldview|apostles(?:\/[a-z0-9-]+)?)|projects(?:\/[a-z0-9-]+)?|apostles(?:\/[a-z0-9-]+)?|research\/(?:hswm|lakatotree)|axioms|philosophy|agents|book|foundation|system)\/$/.test(url.pathname)
      const fragment = !url.hash || (["/axioms/", "/wiki/axioms/"].includes(url.pathname) && /^#axiom-(?:[1-9]|1[0-2])$/.test(url.hash))
      return !url.search && path && fragment
    }
    if (url.hash) return false
    if (url.hostname === "github.com") return !url.search && /^\/gj3447(?:\/[A-Za-z0-9_.-]+)?$/.test(url.pathname)
    if (url.hostname === "www.youtube.com") return (!url.search && /^\/(?:@[A-Za-z0-9_.-]+(?:\/videos)?|channel\/UC[A-Za-z0-9_-]+)$/.test(url.pathname)) ||
      (url.pathname === "/watch" && /^\?v=[A-Za-z0-9_-]{11}$/.test(url.search))
    return false
  } catch { return false }
}
const Url = Text.pipe(Schema.filter(publicUrl, { message: () => "expected an explicitly supported public URL" }))
const Day = Schema.String.pipe(Schema.pattern(/^\d{4}-\d{2}-\d{2}$/), Schema.filter((value) => {
  const date = new Date(value)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}, { message: () => "expected a real calendar date" }))
const HubNode = Schema.Struct({
  id: Id, kind: Schema.Literal("concept", "article", "project", "repository", "channel", "video", "apostle"),
  title: Text, summary: Text, href: Url, public: Schema.Literal(true), authority: Authority,
  reviewedAt: Day,
  sources: Schema.Array(Schema.Struct({ label: Text, url: Url })).pipe(Schema.minItems(1), Schema.maxItems(8))
})
export const relationDescriptions = {
  INTRODUCES: "소개 페이지가 이 항목을 안내합니다. 구현·소속·동일성 관계를 뜻하지 않습니다.",
  DEFINES: "원문이 개념의 정의를 제시합니다.",
  DOCUMENTED_IN: "개념을 설명하는 공개 원문입니다.",
  SOURCE_CODE: "프로그램 또는 헌장의 공개 저장소입니다.",
  EXPLORE_NEXT: "편집자가 추천하는 다음 읽기입니다. 동일성·인과·구현 의존성을 뜻하지 않습니다.",
  PUBLISHES: "채널의 공개 게시 목록에서 확인한 영상입니다. 영상의 주장에 대한 검증은 아닙니다."
} as const
type NodeKind = typeof HubNode.Type.kind
/** Local relationship vocabulary: endpoint roles are part of the meaning contract. */
export const relationKinds: Readonly<Record<keyof typeof relationDescriptions, {
  readonly from: ReadonlyArray<NodeKind>; readonly to: ReadonlyArray<NodeKind>
}>> = {
  INTRODUCES: { from: ["article"], to: ["concept", "project", "apostle"] },
  DEFINES: { from: ["article"], to: ["concept"] },
  DOCUMENTED_IN: { from: ["concept", "project", "apostle"], to: ["article"] },
  SOURCE_CODE: { from: ["project", "article"], to: ["repository"] },
  EXPLORE_NEXT: { from: ["concept", "article", "project", "repository", "channel", "video", "apostle"], to: ["concept", "article", "project", "repository", "channel", "video", "apostle"] },
  PUBLISHES: { from: ["channel"], to: ["video"] }
}
const Hub = Schema.Struct({
  schema: Schema.Literal("metahumotonic/learning-hub@1"), edition: Day,
  publication: Schema.Literal("curated-public"),
  nodes: Schema.Array(HubNode).pipe(Schema.minItems(1), Schema.maxItems(100)),
  edges: Schema.Array(Schema.Struct({ id: Id, from: Id, to: Id,
    relation: Schema.Literal("INTRODUCES", "DEFINES", "DOCUMENTED_IN", "SOURCE_CODE", "EXPLORE_NEXT", "PUBLISHES"),
    label: Text, status: Schema.Literal("ACTIVE", "PROPOSED", "RETIRED"), authority: Authority, source: Url
  })).pipe(Schema.maxItems(300)),
  paths: Schema.Array(Schema.Struct({ id: Id, title: Text, question: Text, description: Text,
    steps: Schema.Array(Id).pipe(Schema.minItems(1), Schema.maxItems(12))
  })).pipe(Schema.maxItems(12))
})
export type LearningHub = typeof Hub.Type
export const decodeHub = (input: unknown) => Schema.decodeUnknownEither(Hub, { onExcessProperty: "error" })(input).pipe(
  Either.flatMap((hub) => {
    const nodes = new Map(hub.nodes.map((node) => [node.id, node]))
    const bad = nodes.size !== hub.nodes.length || new Set(hub.edges.map((edge) => edge.id)).size !== hub.edges.length ||
      new Set(hub.paths.map((path) => path.id)).size !== hub.paths.length ||
      hub.paths.some((path) => new Set(path.steps).size !== path.steps.length || path.steps.some((id) => !nodes.has(id))) ||
      hub.nodes.some((node) => {
        const url = new URL(node.href)
        if (node.reviewedAt > hub.edition || new Set(node.sources.map((source) => source.url)).size !== node.sources.length) return true
        if (node.kind === "repository") return url.hostname !== "github.com" || url.pathname.split("/").length !== 3
        if (node.kind === "video") return url.hostname !== "www.youtube.com" || url.pathname !== "/watch"
        if (node.kind === "channel") return url.hostname !== "www.youtube.com" || !/^\/(?:channel\/UC[A-Za-z0-9_-]+|@[A-Za-z0-9_.-]+)$/.test(url.pathname)
        return url.hostname !== "metahumotonic.com"
      }) ||
      hub.edges.some((edge) => {
        const from = nodes.get(edge.from), to = nodes.get(edge.to)
        return !from || !to || edge.from === edge.to || !relationKinds[edge.relation].from.includes(from.kind) || !relationKinds[edge.relation].to.includes(to.kind)
      })
    return bad ? Either.left(new Error("invalid learning graph identity, endpoint role, publication date, locator or reading path")) : Either.right(hub)
  })
)

const base = "https://metahumotonic.com/learn/"
export const hubIri = (id: string): string => `${base}#entity-${id}`
const predicate = (relation: string) => `${base}#relation-${relation.toLowerCase().replaceAll("_", "-")}`
const types = { apostle: "DefinedTerm", concept: "DefinedTerm", article: "LearningResource", project: "SoftwareApplication", repository: "SoftwareSourceCode", channel: "CollectionPage", video: "VideoObject" } as const

/** One validated public source yields the UI payload, linked data and native USL input. */
export const projectHub = (hub: LearningHub) => {
  const digest = `sha256:${createHash("sha256").update(JSON.stringify(hub)).digest("hex")}`
  const active = hub.edges.filter((edge) => edge.status === "ACTIVE")
  const jsonld = {
    "@context": { "@version": 1.1, skos: "http://www.w3.org/2004/02/skos/core#", "@vocab": "https://schema.org/", mh: base + "#", rdf: "http://www.w3.org/1999/02/22-rdf-syntax-ns#", prov: "http://www.w3.org/ns/prov#" },
    "@id": `${base}graph.jsonld`, "mh:sourceDigest": digest,
    "@graph": [
      { "@id": base, "@type": "CollectionPage", name: "MetaHumotonic · 처음부터 이어 읽기", url: base,
        hasPart: hub.paths.map((path) => ({ "@id": `${base}#path-${path.id}` })) },
      ...hub.nodes.map((node) => ({
        "@id": hubIri(node.id), "@type": types[node.kind], name: node.title, description: node.summary, url: node.href,
        ...(node.kind === "concept" || node.kind === "apostle" ? {
          "skos:prefLabel": { "@value": node.title, "@language": "ko" },
          ...(node.authority === "PRIMARY_SOURCE" ? { "skos:definition": { "@value": node.summary, "@language": "ko" } } : {})
        } : {}),
        ...((active.some(edge => edge.from === node.id && edge.relation === "INTRODUCES")) ? {
          about: active.filter(edge => edge.from === node.id && edge.relation === "INTRODUCES").map(edge => ({ "@id": hubIri(edge.to) }))
        } : {}),
        "mh:authority": node.authority, "mh:reviewedAt": node.reviewedAt,
        "prov:wasDerivedFrom": node.sources.map((source) => ({ "@id": source.url })),
        ...(node.kind === "repository" ? { codeRepository: node.href } : {}),
        ...Object.fromEntries(Object.keys(relationDescriptions).flatMap((relation) => {
          const targets = active.filter((edge) => edge.from === node.id && edge.relation === relation)
          return targets.length ? [[predicate(relation), targets.map((edge) => ({ "@id": hubIri(edge.to) }))]] : []
        }))
      })),
      ...hub.edges.map((edge) => ({ "@id": `${base}#edge-${edge.id}`, "@type": "rdf:Statement",
        "rdf:subject": { "@id": hubIri(edge.from) }, "rdf:predicate": { "@id": predicate(edge.relation) }, "rdf:object": { "@id": hubIri(edge.to) },
        description: edge.label, "mh:status": edge.status, "mh:authority": edge.authority, "prov:wasDerivedFrom": { "@id": edge.source }
      })),
      ...hub.paths.map((path) => ({ "@id": `${base}#path-${path.id}`, "@type": "ItemList", name: path.title, description: path.description,
        itemListOrder: "https://schema.org/ItemListOrderAscending", itemListElement: path.steps.map((id, index) => ({ "@type": "ListItem", position: index + 1, item: { "@id": hubIri(id) } }))
      })),
      ...Object.entries(relationDescriptions).map(([id, description]) => ({ "@id": predicate(id), "@type": "rdf:Property", name: id, description }))
    ]
  }
  const usl = {
    nodes: hub.nodes.map((node) => ({ uid: hubIri(node.id), properties: { name: node.title, kind: node.kind, locator: node.href, authority: node.authority } })),
    relations: hub.edges.map((edge) => ({ uid: `${base}#edge-${edge.id}`, from_uid: hubIri(edge.from), to_uid: hubIri(edge.to), type: edge.relation,
      properties: { description: JSON.stringify({ label: edge.label, meaning: relationDescriptions[edge.relation], status: edge.status, authority: edge.authority, source: edge.source }) }
    }))
  }
  return { ...hub, sourceDigest: digest, relationDescriptions, jsonld, usl }
}
