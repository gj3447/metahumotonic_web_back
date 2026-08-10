/**
 * Canonical snapshot fallback — ported verbatim from `app/kg.py`.
 *
 * When the KG is unreachable the API degrades to the same *shape* the live
 * query returns, so `/api/stats.domains == /api/research/summary.domains ==
 * len(/api/domains)` holds in an outage exactly as it does live.
 *
 * These magnitudes are measurements with a date on them, not invented numbers.
 * Do not "refresh" them by guessing — re-measure and update the date.
 */
import {
  DomainRecord,
  ResearchSummary,
  SkillRecord,
  StatsContract
} from "../domain/Contracts.js"

/** The real 13 DomainHub nodes, measured 2026-06-08. `nodeCount` is the value
 *  as stored (some are themselves stale pre-cleanup magnitudes). */
export const DOMAINS_FALLBACK: ReadonlyArray<DomainRecord> = [
  new DomainRecord({ name: "domain-hub-ai", displayName: "KG_AI 도메인", nodeCount: 499797 }),
  new DomainRecord({ name: "domain-hub-sym", displayName: "KG_SYM 도메인", nodeCount: 29215 }),
  new DomainRecord({
    name: "domain-hub-reference",
    displayName: "KG_REFERENCE 도메인",
    nodeCount: 5961
  }),
  new DomainRecord({ name: "domain-hub-cs", displayName: "KG_CS 도메인", nodeCount: 4302 }),
  new DomainRecord({
    name: "domain-hub-projects",
    displayName: "KG_PROJECTS 도메인",
    nodeCount: 3451
  }),
  new DomainRecord({ name: "domain-hub-infra", displayName: "KG_INFRA 도메인", nodeCount: 2412 }),
  new DomainRecord({
    name: "domain-hub-creative",
    displayName: "KG_CREATIVE 도메인",
    nodeCount: 2081
  }),
  new DomainRecord({ name: "domain-hub-apt", displayName: "KG_APT 도메인", nodeCount: 1716 }),
  new DomainRecord({ name: "domain-hub-import", displayName: "KG_Import 도메인", nodeCount: 275 }),
  new DomainRecord({ name: "domain-hub-orphan", displayName: "KG_ORPHAN 도메인", nodeCount: 219 }),
  new DomainRecord({ name: "domain-hub-333", displayName: "KG_333 도메인", nodeCount: 74 }),
  new DomainRecord({
    name: "domain-hub-imported",
    displayName: "KG_Imported 도메인",
    nodeCount: 6
  }),
  new DomainRecord({
    name: "domain-hub-unlabeled",
    displayName: "KG_UNLABELED 도메인",
    nodeCount: 0
  })
]

/** 7군단장 (5무기 → 5→7 확장 2026-05-26~27) + infra/meta skills. */
export const SKILLS_FALLBACK: ReadonlyArray<SkillRecord> = [
  new SkillRecord({ name: "apt", description: "APT v26.1 orchestrator", category: "apt" }),
  new SkillRecord({
    name: "prometheus",
    description: "획득 — knowledge-first research",
    category: "commander"
  }),
  new SkillRecord({
    name: "eureka",
    description: "발견·창조 — induction → new concepts (5→7 확장 2026-05-26)",
    category: "commander"
  }),
  new SkillRecord({
    name: "longinus",
    description: "연결 — KG↔code reference binding",
    category: "commander"
  }),
  new SkillRecord({
    name: "occam",
    description: "정리 — archive superseded·stale (5→7 확장 2026-05-26)",
    category: "commander"
  }),
  new SkillRecord({
    name: "naesengmoon",
    description: "검증 — 적대적 검증 (canonical, alias: taliban)",
    category: "commander"
  }),
  new SkillRecord({
    name: "jaebaeman",
    description: "출격 — plan-first subagent orchestration",
    category: "commander"
  }),
  new SkillRecord({
    name: "harness",
    description: "실현 — abstract spec → concrete code (하네스=하데스)",
    category: "commander"
  }),
  new SkillRecord({
    name: "88-naesengmoon",
    description: "113-lens mathematical meta-verification",
    category: "commander"
  }),
  new SkillRecord({ name: "solve", description: "Systematic problem resolution", category: "meta" }),
  new SkillRecord({ name: "db-query", description: "Database query execution", category: "infra" }),
  new SkillRecord({ name: "server-status", description: "Server health check", category: "infra" }),
  new SkillRecord({
    name: "docker-logs",
    description: "Container log inspection",
    category: "infra"
  }),
  new SkillRecord({
    name: "kafka-manage",
    description: "Kafka topic management",
    category: "infra"
  }),
  new SkillRecord({ name: "deploy", description: "Service deployment", category: "infra" }),
  new SkillRecord({ name: "backup", description: "Backup management", category: "infra" }),
  new SkillRecord({ name: "skill-creator", description: "Skill creation wizard", category: "meta" })
]

/**
 * domains/skills are curated-surface counts (NOT a Neo4j count), so both the
 * live and the fallback stats path derive them from the lists above. That is
 * what keeps the three surfaces consistent.
 */
export const DOMAINS_COUNT = DOMAINS_FALLBACK.length
export const SKILLS_COUNT = SKILLS_FALLBACK.length

/** node/rel/label magnitudes measured 2026-06-08 (post-Occam AI-domain cleanup;
 *  the old 582,630 was the pre-cleanup count). Used only when the KG is down. */
export const STATS_FALLBACK = new StatsContract({
  nodes: 90808,
  rels: 614376,
  labels: 3249,
  relTypes: 4682,
  domains: DOMAINS_COUNT,
  skills: SKILLS_COUNT
})

export const RESEARCH_SUMMARY_FALLBACK = new ResearchSummary({
  findings: 12417,
  lessons: 1215,
  papers: 161,
  validations: 608,
  consensus: 25,
  decisions: 133,
  apostles: 12,
  domains: 13,
  source: "snapshot"
})
