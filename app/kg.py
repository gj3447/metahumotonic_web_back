"""Neo4j read client with graceful snapshot fallback.

Mirrors the Cypher in `metahumotonic-web/src/lib/kg.ts`. If `MHB_NEO4J_LIVE`
is false, or the driver/query fails for any reason, the canonical fallback
constants are returned so the API never 500s on a KG outage.
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import AsyncGraphDatabase

from .breaker import Breaker
from .cache import TTLCache
from .config import settings
from .contracts import DomainRecord, SkillRecord, StatsContract

log = logging.getLogger("mhb.kg")

# --- Canonical fallback (node/rel baseline from front-end stats.json.ts) ---
_DOMAINS_FALLBACK = [
    DomainRecord(name="domain-personal", displayName="Personal", nodeCount=1200),
    DomainRecord(name="domain-math-physics", displayName="Math & Physics", nodeCount=5346),
    DomainRecord(name="domain-cs", displayName="Computer Science", nodeCount=3200),
    DomainRecord(name="domain-infra", displayName="Infrastructure", nodeCount=2844),
    DomainRecord(name="domain-ai", displayName="Artificial Intelligence", nodeCount=4200),
    DomainRecord(name="domain-apt", displayName="APT Methodology", nodeCount=1500),
    DomainRecord(name="domain-literature", displayName="Literature & Writing", nodeCount=2100),
    DomainRecord(name="domain-religion", displayName="Religion & Theology", nodeCount=1800),
    DomainRecord(name="domain-philosophy", displayName="Philosophy", nodeCount=1100),
]

# Skills surface — 7군단장 (구 5무기 → 5→7 확장 2026-05-26~27) + infra/meta skills.
_SKILLS_FALLBACK = [
    SkillRecord(name="apt", description="APT v26.1 orchestrator", category="apt"),
    SkillRecord(name="prometheus", description="획득 — knowledge-first research", category="commander"),
    SkillRecord(name="eureka", description="발견·창조 — induction → new concepts (5→7 확장 2026-05-26)", category="commander"),
    SkillRecord(name="longinus", description="연결 — KG↔code reference binding", category="commander"),
    SkillRecord(name="occam", description="정리 — archive superseded·stale (5→7 확장 2026-05-26)", category="commander"),
    SkillRecord(name="naesengmoon", description="검증 — 적대적 검증 (canonical, alias: taliban)", category="commander"),
    SkillRecord(name="jaebaeman", description="출격 — plan-first subagent orchestration", category="commander"),
    SkillRecord(name="harness", description="실현 — abstract spec → concrete code (하네스=하데스)", category="commander"),
    SkillRecord(name="88-naesengmoon", description="113-lens mathematical meta-verification", category="commander"),
    SkillRecord(name="solve", description="Systematic problem resolution", category="meta"),
    SkillRecord(name="db-query", description="Database query execution", category="infra"),
    SkillRecord(name="server-status", description="Server health check", category="infra"),
    SkillRecord(name="docker-logs", description="Container log inspection", category="infra"),
    SkillRecord(name="kafka-manage", description="Kafka topic management", category="infra"),
    SkillRecord(name="deploy", description="Service deployment", category="infra"),
    SkillRecord(name="backup", description="Backup management", category="infra"),
    SkillRecord(name="skill-creator", description="Skill creation wizard", category="meta"),
]

# domains/skills are curated-surface counts (NOT a Neo4j count), so they are
# derived from the lists above — both the live and fallback stats paths use
# these so /api/stats stays consistent with /api/domains and /api/skills.
_DOMAINS_COUNT = len(_DOMAINS_FALLBACK)
_SKILLS_COUNT = len(_SKILLS_FALLBACK)
_STATS_FALLBACK = StatsContract(
    nodes=582630, rels=1104948, labels=3095, relTypes=4498,
    domains=_DOMAINS_COUNT, skills=_SKILLS_COUNT,
)

_STATS_CYPHER = """
CALL db.labels() YIELD label
WITH count(label) AS labels
CALL db.relationshipTypes() YIELD relationshipType
WITH labels, count(relationshipType) AS relTypes
MATCH (n) WITH labels, relTypes, count(n) AS nodes
MATCH ()-[r]->() WITH labels, relTypes, nodes, count(r) AS rels
OPTIONAL MATCH (d:DomainHub)
RETURN labels, relTypes, nodes, rels, count(d) AS domains
"""

_DOMAINS_CYPHER = """
MATCH (d:DomainHub)
RETURN d.name AS name, d.displayName AS displayName,
       d.nodeCount AS nodeCount, coalesce(d.description, '') AS description
ORDER BY d.nodeCount DESC
"""


class KGClient:
    """Async Neo4j client. One driver per process; lazy + fail-soft."""

    def __init__(self) -> None:
        self._driver = None
        self._breaker = Breaker()  # time-based, not a permanent latch
        self._cache = TTLCache(settings.stats_cache_ttl_seconds)

    async def _get_driver(self):
        if not settings.neo4j_live or self._breaker.is_open():
            return None
        if self._driver is not None:
            return self._driver
        try:
            self._driver = AsyncGraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            return self._driver
        except Exception as e:  # pragma: no cover - infra dependent
            self._breaker.trip()
            log.warning("neo4j driver init failed, using snapshot: %s", e)
            return None

    async def _run(self, cypher: str, **params: Any) -> list[dict] | None:
        driver = await self._get_driver()
        if driver is None:
            return None
        try:
            async with driver.session() as session:
                result = await session.run(cypher, **params)
                rows = [dict(r) async for r in result]
            self._breaker.reset()  # healthy again
            return rows
        except Exception as e:  # pragma: no cover - infra dependent
            self._breaker.trip()
            log.warning("neo4j query failed, using snapshot: %s", e)
            return None

    async def get_stats(self) -> StatsContract:
        return await self._cache.get_or_set("stats", self._fetch_stats)

    async def _fetch_stats(self) -> StatsContract:
        rows = await self._run(_STATS_CYPHER)
        # skills = curated surface; domains = live DomainHub count from the SAME
        # query (so it matches /api/domains regardless of caching/replica).
        skills = len(await self.get_skills())
        if rows:
            r = rows[0]
            live_domains = int(r["domains"]) or _DOMAINS_COUNT
            return StatsContract(
                nodes=int(r["nodes"]), rels=int(r["rels"]),
                labels=int(r["labels"]), relTypes=int(r["relTypes"]),
                domains=live_domains, skills=skills,
            )
        return StatsContract(
            nodes=_STATS_FALLBACK.nodes, rels=_STATS_FALLBACK.rels,
            labels=_STATS_FALLBACK.labels, relTypes=_STATS_FALLBACK.relTypes,
            domains=_DOMAINS_COUNT, skills=skills,
        )

    async def get_domains(self) -> list[DomainRecord]:
        return await self._cache.get_or_set("domains", self._fetch_domains)

    async def _fetch_domains(self) -> list[DomainRecord]:
        rows = await self._run(_DOMAINS_CYPHER)
        if rows:
            return [DomainRecord(**r) for r in rows]
        return list(_DOMAINS_FALLBACK)

    async def get_skills(self) -> list[SkillRecord]:
        # Skills are a curated canon surface, not a live query.
        return list(_SKILLS_FALLBACK)

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None


kg = KGClient()
