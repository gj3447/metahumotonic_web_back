"""Neo4j read client with graceful snapshot fallback.

Mirrors the Cypher in `metahumotonic-web/src/lib/kg.ts`. If `MHB_NEO4J_LIVE`
is false, or the driver/query fails for any reason, the canonical fallback
constants are returned so the API never 500s on a KG outage.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError

from .breaker import Breaker
from .cache import TTLCache
from .config import settings
from .contracts import (
    ConsensusRecord,
    DomainRecord,
    FindingRecord,
    GraphNeighbor,
    LessonRecord,
    NodeNeighbors,
    PaperRecord,
    RecentItem,
    ResearchSummary,
    SkillRecord,
    StatsContract,
)

log = logging.getLogger("mhb.kg")


class KGUnavailable(RuntimeError):
    """No configured Neo4j URI was reachable (connection-level failure).

    The proxy router maps this to 502 — distinct from a query-level Neo4jError
    (bad Cypher / write-in-read-tx) which maps to 400.
    """

# --- Canonical fallback (the real 13 DomainHub nodes, measured 2026-06-08) ---
# Mirrors the live /api/domains so an outage degrades to the same SHAPE the live
# query returns (13 domains), keeping /api/stats.domains == /api/research/summary
# .domains == len(/api/domains). nodeCounts are the DomainHub.nodeCount values as
# currently stored (some are themselves stale pre-cleanup magnitudes).
_DOMAINS_FALLBACK = [
    DomainRecord(name="domain-hub-ai", displayName="KG_AI 도메인", nodeCount=499797),
    DomainRecord(name="domain-hub-sym", displayName="KG_SYM 도메인", nodeCount=29215),
    DomainRecord(name="domain-hub-reference", displayName="KG_REFERENCE 도메인", nodeCount=5961),
    DomainRecord(name="domain-hub-cs", displayName="KG_CS 도메인", nodeCount=4302),
    DomainRecord(name="domain-hub-projects", displayName="KG_PROJECTS 도메인", nodeCount=3451),
    DomainRecord(name="domain-hub-infra", displayName="KG_INFRA 도메인", nodeCount=2412),
    DomainRecord(name="domain-hub-creative", displayName="KG_CREATIVE 도메인", nodeCount=2081),
    DomainRecord(name="domain-hub-apt", displayName="KG_APT 도메인", nodeCount=1716),
    DomainRecord(name="domain-hub-import", displayName="KG_Import 도메인", nodeCount=275),
    DomainRecord(name="domain-hub-orphan", displayName="KG_ORPHAN 도메인", nodeCount=219),
    DomainRecord(name="domain-hub-333", displayName="KG_333 도메인", nodeCount=74),
    DomainRecord(name="domain-hub-imported", displayName="KG_Imported 도메인", nodeCount=6),
    DomainRecord(name="domain-hub-unlabeled", displayName="KG_UNLABELED 도메인", nodeCount=0),
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
# node/rel/label magnitudes measured 2026-06-08 (post-Occam AI-domain cleanup;
# the old 582,630 was the pre-cleanup count). Used only when the KG is down.
_STATS_FALLBACK = StatsContract(
    nodes=90808, rels=614376, labels=3249, relTypes=4682,
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

# --- Research surface (live view of the crystallized body in the KG) -------- #
# Single-label count(n) hits Neo4j's count-store (O(1)); chaining one WITH per
# label keeps the whole summary to a single cheap row.
_RESEARCH_SUMMARY_CYPHER = """
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
"""

_FINDINGS_CYPHER = """
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
"""

_LESSONS_CYPHER = """
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
"""

_PAPERS_CYPHER = """
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
"""

_CONSENSUS_CYPHER = """
MATCH (c:Consensus)
WHERE c.name IS NOT NULL
WITH c, coalesce(toString(c.created_at), toString(c.createdAt), '') AS ts
RETURN c.name AS name,
       coalesce(c.summary, c.description, c.statement, c.body, '') AS summary,
       ts AS createdAt
ORDER BY ts DESC, name
LIMIT $limit
"""

# Node neighbors — make any node a doorway. Bare-name match (no global name
# index) is a scan, but capped + cached + 10s-timeout fail-soft. The collected
# neighbor list is capped to $limit; degree is the true (uncapped) degree so the
# client can show "truncated". COUNT{} + scoped CALL keep it one round-trip.
_NEIGHBORS_CYPHER = """
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
"""

# Fallback summary — magnitudes measured 2026-06-08 (used only when KG is down;
# the live path overrides these on every healthy query).
_RESEARCH_SUMMARY_FALLBACK = ResearchSummary(
    findings=12417, lessons=1215, papers=161, validations=608,
    consensus=25, decisions=133, apostles=12, domains=13,
)


def _bounds(limit: int, offset: int) -> tuple[int, int]:
    """Clamp pagination so a client can't ask for an unbounded scan."""
    return max(1, min(int(limit), 100)), max(0, int(offset))


def _as_conf(v: Any) -> str:
    """Confidence is categorical ('HIGH') or numeric (0.82) in the KG → string."""
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_bool(v: Any) -> bool | None:
    return bool(v) if isinstance(v, bool) else (None if v is None else bool(v))


def _sanitize(v: Any) -> Any:
    """Coerce native neo4j scalar types (DateTime/Date/Duration/…) to str while
    PRESERVING list/dict structure (so collected neighbor maps survive)."""
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, list):
        return [_sanitize(x) for x in v]
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    return str(v)


def _safe_map(rows: list[dict], builder, label: str) -> list:
    """Build records row-by-row, dropping (not 500-ing on) any malformed row.

    Defense-in-depth: the Cypher already filters null names, but a single bad
    node must never take down the whole feed (the 'never 500s' guarantee).
    """
    out = []
    for r in rows:
        try:
            out.append(builder(r))
        except Exception as e:  # pragma: no cover - depends on KG data shape
            log.warning("research: dropping malformed %s row (%s)", label, e)
    return out


class KGClient:
    """Async Neo4j client. One driver per process; lazy + fail-soft."""

    def __init__(self) -> None:
        self._driver = None
        self._drivers: dict[str, Any] = {}
        self._breaker = Breaker()  # time-based, not a permanent latch
        self._cache = TTLCache(
            settings.stats_cache_ttl_seconds, settings.cache_max_entries
        )
        self._research_cache = TTLCache(
            settings.research_cache_ttl_seconds, settings.cache_max_entries
        )

    @staticmethod
    def _configured_uris() -> list[str]:
        uris = [settings.neo4j_uri]
        uris.extend(
            u.strip() for u in settings.neo4j_fallback_uris.split(",") if u.strip()
        )
        deduped: list[str] = []
        for uri in uris:
            if uri and uri not in deduped:
                deduped.append(uri)
        return deduped

    async def _get_driver(self, uri: str):
        if uri in self._drivers:
            return self._drivers[uri]
        try:
            driver = AsyncGraphDatabase.driver(
                uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            self._drivers[uri] = driver
            self._driver = driver
            return driver
        except Exception as e:  # pragma: no cover - infra dependent
            log.warning("neo4j driver init failed for %s: %s", uri, e)
            return None

    async def _drop_driver(self, uri: str) -> None:
        driver = self._drivers.pop(uri, None)
        if driver is None:
            return
        try:
            await driver.close()
        except Exception:  # pragma: no cover - cleanup best effort
            pass
        if self._driver is driver:
            self._driver = next(iter(self._drivers.values()), None)

    async def _run(self, cypher: str, **params: Any) -> list[dict] | None:
        if not settings.neo4j_live or self._breaker.is_open():
            return None
        last_error: Exception | None = None
        for uri in self._configured_uris():
            driver = await self._get_driver(uri)
            if driver is None:
                continue
            try:
                async with driver.session() as session:
                    result = await asyncio.wait_for(
                        session.run(cypher, **params),
                        timeout=settings.kg_query_timeout_seconds,
                    )
                    rows = await asyncio.wait_for(
                        self._drain(result), timeout=settings.kg_query_timeout_seconds
                    )
                self._breaker.reset()  # healthy again
                return rows
            except Exception as e:  # pragma: no cover - infra dependent
                last_error = e
                await self._drop_driver(uri)
                log.warning("neo4j query failed for %s: %s", uri, e)
        self._breaker.trip()
        log.warning("neo4j all configured URIs failed, using snapshot: %s", last_error)
        return None

    @staticmethod
    async def _drain(result) -> list[dict]:
        # Sanitize native neo4j types at the boundary so they never reach a
        # str-typed Pydantic contract and 500 — recursively, to keep collected
        # neighbor lists/maps intact.
        return [
            {k: _sanitize(v) for k, v in dict(r).items()}
            async for r in result
        ]

    # --- Raw Cypher proxy (read/write split for external clients) ----------- #
    # Unlike _run (which is gated by neo4j_live + breaker and falls back to a
    # snapshot for the public read endpoints), the proxy MUST connect live and
    # surface errors — a silent fallback would lie to an external write client.
    # Access mode is the real enforcement: a READ tx makes the SERVER reject any
    # write, so the read key cannot mutate the graph even with malicious Cypher.
    async def run_cypher(
        self, cypher: str, params: dict[str, Any], *, write: bool
    ) -> list[dict]:
        last_error: Exception | None = None
        for uri in self._configured_uris():
            driver = await self._get_driver(uri)
            if driver is None:
                continue
            try:
                async with driver.session(
                    default_access_mode=("WRITE" if write else "READ"),
                    database=settings.neo4j_database,
                ) as session:
                    async def _work(tx):
                        result = await tx.run(cypher, params)
                        return await asyncio.wait_for(
                            self._drain(result),
                            timeout=settings.kg_query_timeout_seconds,
                        )

                    runner = session.execute_write if write else session.execute_read
                    rows = await asyncio.wait_for(
                        runner(_work),
                        timeout=settings.kg_query_timeout_seconds,
                    )
                return rows[: settings.kg_proxy_max_rows]
            except Neo4jError:
                # query-level error (bad Cypher / write-in-read tx) — do NOT
                # retry other URIs or swallow it; the client must see why.
                raise
            except Exception as e:  # connection-level — try the next URI
                last_error = e
                await self._drop_driver(uri)
                log.warning("kg proxy query failed for %s: %s", uri, e)
        raise KGUnavailable(str(last_error) if last_error else "no KG URI reachable")

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

    # --- Research surface ---------------------------------------------------- #

    async def get_research_summary(self) -> ResearchSummary:
        return await self._research_cache.get_or_set(
            "research_summary", self._fetch_research_summary
        )

    async def _fetch_research_summary(self) -> ResearchSummary:
        rows = await self._run(_RESEARCH_SUMMARY_CYPHER)
        if rows:
            try:
                r = rows[0]
                return ResearchSummary(
                    findings=int(r["findings"]), lessons=int(r["lessons"]),
                    papers=int(r["papers"]), validations=int(r["validations"]),
                    consensus=int(r["consensus"]), decisions=int(r["decisions"]),
                    apostles=int(r["apostles"]), domains=int(r["domains"]),
                    source="live",
                )
            except Exception as e:  # pragma: no cover - depends on KG row shape
                log.warning("research: summary row malformed, using fallback (%s)", e)
        return _RESEARCH_SUMMARY_FALLBACK

    async def get_findings(
        self, limit: int = 20, offset: int = 0, cycle: str = ""
    ) -> list[FindingRecord]:
        limit, offset = _bounds(limit, offset)
        key = f"findings:{limit}:{offset}:{cycle}"

        async def _produce() -> list[FindingRecord]:
            rows = await self._run(
                _FINDINGS_CYPHER, limit=limit, offset=offset, cycle=cycle
            )
            if rows is None:
                return []
            return _safe_map(rows, lambda r: FindingRecord(
                name=r["name"], finding=r["finding"], axis=r["axis"],
                subAxis=r["subAxis"],
                confidence=_as_conf(r["confidence"]),
                cycleId=r["cycleId"],
                verified=_as_bool(r["verified"]),
                lakatosMechanism=r["lakatosMechanism"],
                citationUrl=r["citationUrl"], createdAt=r["createdAt"],
            ), "finding")

        return await self._research_cache.get_or_set(key, _produce)

    async def get_lessons(
        self, limit: int = 20, offset: int = 0
    ) -> list[LessonRecord]:
        limit, offset = _bounds(limit, offset)
        key = f"lessons:{limit}:{offset}"

        async def _produce() -> list[LessonRecord]:
            rows = await self._run(_LESSONS_CYPHER, limit=limit, offset=offset)
            if rows is None:
                return []
            return _safe_map(rows, lambda r: LessonRecord(**r), "lesson")

        return await self._research_cache.get_or_set(key, _produce)

    async def get_papers(
        self, limit: int = 20, offset: int = 0, domain: str = ""
    ) -> list[PaperRecord]:
        limit, offset = _bounds(limit, offset)
        key = f"papers:{limit}:{offset}:{domain}"

        async def _produce() -> list[PaperRecord]:
            rows = await self._run(
                _PAPERS_CYPHER, limit=limit, offset=offset, domain=domain
            )
            if rows is None:
                return []
            return _safe_map(rows, lambda r: PaperRecord(
                title=r["title"], author=r["author"],
                year=_as_int(r["year"]), journal=r["journal"], doi=r["doi"],
                domain=r["domain"], coreThesis=r["coreThesis"],
                status=r["status"],
            ), "paper")

        return await self._research_cache.get_or_set(key, _produce)

    async def get_consensus(self, limit: int = 25) -> list[ConsensusRecord]:
        limit, _ = _bounds(limit, 0)
        key = f"consensus:{limit}"

        async def _produce() -> list[ConsensusRecord]:
            rows = await self._run(_CONSENSUS_CYPHER, limit=limit)
            if rows is None:
                return []
            return _safe_map(rows, lambda r: ConsensusRecord(**r), "consensus")

        return await self._research_cache.get_or_set(key, _produce)

    async def get_recent(self, limit: int = 30) -> list[RecentItem]:
        """Unified newest-first feed across findings/lessons/consensus.

        Built by merging the (already cached) per-type getters, so it adds no
        new KG round-trips beyond what the dedicated endpoints already cache.
        """
        per = max(8, min(limit, 40))

        async def _safe(coro):
            # one failing source degrades to [] rather than 500-ing the feed
            try:
                return await coro
            except Exception as e:  # pragma: no cover - depends on KG data shape
                log.warning("research: recent sub-feed failed (%s)", e)
                return []

        findings = await _safe(self.get_findings(limit=per))
        lessons = await _safe(self.get_lessons(limit=per))
        consensus = await _safe(self.get_consensus(limit=min(per, 25)))
        items: list[RecentItem] = []
        for f in findings:
            items.append(RecentItem(
                type="finding", name=f.name,
                title=f"{f.axis} · {f.subAxis}".strip(" ·") or "ResearchFinding",
                summary=f.finding, createdAt=f.createdAt,
            ))
        for le in lessons:
            items.append(RecentItem(
                type="lesson", name=le.name,
                title=le.problem or le.name,
                summary=(f"{le.wrongAssumption} → {le.truth}"
                         if le.truth else le.solution),
                createdAt=le.createdAt,
            ))
        for c in consensus:
            items.append(RecentItem(
                type="consensus", name=c.name, title=c.name,
                summary=c.summary, createdAt=c.createdAt,
            ))
        # newest first; blank timestamps sink to the bottom
        items.sort(key=lambda it: it.createdAt or "", reverse=True)
        return items[:limit]

    async def get_neighbors(self, name: str, limit: int = 50) -> NodeNeighbors:
        limit = max(1, min(int(limit), 200))
        key = f"neighbors:{name}:{limit}"

        async def _produce() -> NodeNeighbors:
            rows = await self._run(_NEIGHBORS_CYPHER, name=name, limit=limit)
            if not rows:  # None (KG down) OR [] (node not found) → not found, no 500
                return NodeNeighbors(name=name, found=False, degree=0, neighbors=[])
            r = rows[0]
            degree = int(r["degree"])
            nbrs = [
                GraphNeighbor(
                    direction=x["direction"], type=x["type"],
                    name=x.get("name") or "", labels=x.get("labels") or [],
                )
                for x in (r["neighbors"] or [])
            ]
            return NodeNeighbors(
                name=name, found=True, degree=degree, neighbors=nbrs,
                truncated=degree > limit,
            )

        return await self._research_cache.get_or_set(key, _produce)

    async def close(self) -> None:
        for uri in list(self._drivers):
            await self._drop_driver(uri)
        self._driver = None


kg = KGClient()
