"""Research surface — fail-soft in CI (no Neo4j) + live row-mapping via monkeypatch.

CI has no live Neo4j, so the bare endpoints exercise the fallback path (summary =
measured magnitudes, list feeds = []). The monkeypatch tests inject synthetic KG
rows to cover the record-building + recent-merge/sort logic the fallback can't.
"""

from __future__ import annotations

import pytest

from app.kg import kg


@pytest.fixture(autouse=True)
def _clear_research_cache():
    # research feeds are cached; clear so monkeypatched rows aren't masked
    kg._research_cache.clear()
    yield
    kg._research_cache.clear()


# --------------------------------------------------------------------------- #
# Fail-soft path (no live KG): never 500, shapes hold, counts are positive.    #
# --------------------------------------------------------------------------- #

async def test_summary_shape_and_positive(client):
    r = await client.get("/api/research/summary")
    assert r.status_code == 200
    data = r.json()
    keys = ("findings", "lessons", "papers", "validations",
            "consensus", "decisions", "apostles", "domains")
    for k in keys:
        assert k in data and isinstance(data[k], int)
    assert data["findings"] > 0 and data["apostles"] == 12


@pytest.mark.parametrize("path", [
    "/api/research/findings",
    "/api/research/lessons",
    "/api/research/papers",
    "/api/research/consensus",
    "/api/research/recent",
])
async def test_list_endpoints_never_500(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert isinstance(r.json(), list)  # [] under fallback is fine


async def test_agent_feed_shape(client):
    r = await client.get("/api/research/agent")
    assert r.status_code == 200
    data = r.json()
    assert data["doctrine_url"].endswith("/llms.txt")
    assert "metahumotonic.com" in data["canonical_source"]
    assert isinstance(data["summary"], dict) and data["summary"]["findings"] > 0
    assert isinstance(data["recent_findings"], list)
    assert isinstance(data["recent_lessons"], list)
    assert data["how_to_cite"]


async def test_pagination_bounds_enforced(client):
    assert (await client.get("/api/research/findings?limit=0")).status_code == 422
    assert (await client.get("/api/research/findings?limit=101")).status_code == 422
    assert (await client.get("/api/research/findings?offset=-1")).status_code == 422
    assert (await client.get("/api/research/findings?limit=5&offset=0")).status_code == 200


async def test_offset_capped_against_cache_busting(client):
    # ?offset can't be used to mint unbounded distinct cache keys / scan deep
    assert (await client.get("/api/research/findings?offset=10001")).status_code == 422
    assert (await client.get("/api/research/findings?offset=10000")).status_code == 200


async def test_summary_source_is_snapshot_on_fallback(client):
    # no live KG in CI → summary must honestly mark itself snapshot, not live
    r = await client.get("/api/research/summary")
    assert r.json()["source"] == "snapshot"


async def test_summary_source_is_live_when_kg_answers(client, monkeypatch):
    async def fake_run(cypher, **params):
        if "ResearchFinding) WITH count" in cypher or "RETURN findings" in cypher:
            return [{
                "findings": 12500, "lessons": 1220, "papers": 161, "validations": 610,
                "consensus": 25, "decisions": 133, "apostles": 12, "domains": 13,
            }]
        return None

    monkeypatch.setattr(kg, "_run", fake_run)
    r = await client.get("/api/research/summary")
    data = r.json()
    assert data["source"] == "live" and data["findings"] == 12500


# --------------------------------------------------------------------------- #
# Live row-mapping (monkeypatched _run): cover record build + recent sort.     #
# --------------------------------------------------------------------------- #

async def test_findings_row_mapping(client, monkeypatch):
    rows = [{
        "name": "rf-1", "finding": "faithful 선의공리 lands no nontrivial theory",
        "axis": "FORMAL VALUE THEORY", "subAxis": "monotone",
        "confidence": 0.82, "cycleId": "cycle-x", "verified": True,
        "lakatosMechanism": "monster-barring",
        "citationUrl": "https://example.org/p", "createdAt": "2026-06-03",
    }]

    async def fake_run(cypher, **params):
        return rows if "ResearchFinding" in cypher else None

    monkeypatch.setattr(kg, "_run", fake_run)
    r = await client.get("/api/research/findings?limit=5")
    assert r.status_code == 200
    data = r.json()
    assert data[0]["name"] == "rf-1"
    assert data[0]["confidence"] == "0.82"  # numeric kept as string
    assert data[0]["verified"] is True


async def test_recent_merges_and_sorts_newest_first(client, monkeypatch):
    async def fake_run(cypher, **params):
        if "ResearchFinding" in cypher:
            return [{
                "name": "rf-old", "finding": "old finding", "axis": "A",
                "subAxis": "", "confidence": None, "cycleId": "", "verified": None,
                "lakatosMechanism": "", "citationUrl": "", "createdAt": "2026-05-01",
            }]
        if "Lesson" in cypher:
            return [{
                "name": "lesson-new", "problem": "P", "solution": "S",
                "wrongAssumption": "WA", "truth": "T", "category": "agent",
                "severity": "high", "lakatosMechanism": "", "createdAt": "2026-06-07",
            }]
        if "Consensus" in cypher:
            return [{"name": "cons-mid", "summary": "C", "createdAt": "2026-06-01"}]
        return None

    monkeypatch.setattr(kg, "_run", fake_run)
    r = await client.get("/api/research/recent?limit=10")
    assert r.status_code == 200
    items = r.json()
    order = [it["name"] for it in items]
    assert order == ["lesson-new", "cons-mid", "rf-old"]  # newest createdAt first
    types = {it["type"] for it in items}
    assert types == {"finding", "lesson", "consensus"}
    lesson = next(it for it in items if it["type"] == "lesson")
    assert "WA → T" in lesson["summary"]  # wrong→truth pair rendered


async def test_drain_sanitizes_native_neo4j_types():
    """Regression: Consensus.created_at is a neo4j DateTime; the boundary must
    stringify it (it 500'd before — caught only by the live smoke test)."""

    class FakeDateTime:  # mimics neo4j.time.DateTime (not str/int/float/bool)
        def __str__(self):
            return "2026-05-28T08:25:02.397000000+00:00"

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def __aiter__(self):
            self._it = iter(self._rows)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    rows = [dict(name="c", createdAt=FakeDateTime(), n=3, ok=True, blank=None)]
    out = await kg._drain(FakeResult(rows))
    assert out[0]["createdAt"] == "2026-05-28T08:25:02.397000000+00:00"
    assert isinstance(out[0]["createdAt"], str)
    assert out[0]["n"] == 3 and out[0]["ok"] is True and out[0]["blank"] is None


async def test_null_name_finding_row_is_dropped_not_500(client, monkeypatch):
    """HIGH regression: the live KG has :ResearchFinding nodes with null name on
    the default page. A null name must be dropped (200), never 500 the feed."""

    async def fake_run(cypher, **params):
        if "count(n) AS findings" in cypher:
            return None  # summary → fallback (not under test here)
        if "ResearchFinding" in cypher:
            return [
                {"name": None, "finding": "ghost row", "axis": "", "subAxis": "",
                 "confidence": None, "cycleId": "", "verified": None,
                 "lakatosMechanism": "", "citationUrl": "", "createdAt": "2026-05-24"},
                {"name": "rf-real", "finding": "real row", "axis": "", "subAxis": "",
                 "confidence": None, "cycleId": "", "verified": None,
                 "lakatosMechanism": "", "citationUrl": "", "createdAt": "2026-06-01"},
            ]
        return None

    monkeypatch.setattr(kg, "_run", fake_run)
    for path in ("/api/research/findings", "/api/research/recent", "/api/research/agent"):
        r = await client.get(path)
        assert r.status_code == 200, f"{path} 500'd on a null-name row"
    names = [f["name"] for f in (await client.get("/api/research/findings")).json()]
    assert names == ["rf-real"]  # null-name dropped, real one kept


async def test_drain_preserves_nested_lists_and_maps():
    """The neighbors query returns a collected list-of-maps — the sanitizer must
    keep that structure (only temporal SCALARS get stringified, recursively)."""

    class FakeDateTime:
        def __str__(self):
            return "2026-06-08T00:00:00Z"

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def __aiter__(self):
            self._it = iter(self._rows)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    rows = [dict(
        degree=3,
        neighbors=[
            {"direction": "out", "type": "ABOUT", "name": "gaptopic:x",
             "labels": ["KnowledgeHub", "GapTopicHub"], "when": FakeDateTime()},
        ],
    )]
    out = await kg._drain(FakeResult(rows))
    nb = out[0]["neighbors"]
    assert isinstance(nb, list) and isinstance(nb[0], dict)        # structure kept
    assert nb[0]["labels"] == ["KnowledgeHub", "GapTopicHub"]      # inner list kept
    assert nb[0]["when"] == "2026-06-08T00:00:00Z"                 # inner DateTime stringified


async def test_neighbors_endpoint(client, monkeypatch):
    async def fake_run(cypher, **params):
        if "COUNT { (n)--() } AS degree" in cypher:
            return [{
                "degree": 2,
                "neighbors": [
                    {"direction": "out", "type": "ABOUT",
                     "name": "gaptopic:CHU", "labels": ["GapTopicHub"]},
                    {"direction": "in", "type": "HAS_RESEARCH",
                     "name": "lesson-x", "labels": ["Lesson"]},
                ],
            }]
        return None

    monkeypatch.setattr(kg, "_run", fake_run)
    r = await client.get("/api/research/neighbors?name=rf-1&limit=50")
    assert r.status_code == 200
    d = r.json()
    assert d["found"] is True and d["degree"] == 2 and d["truncated"] is False
    assert {n["type"] for n in d["neighbors"]} == {"ABOUT", "HAS_RESEARCH"}


async def test_neighbors_not_found_and_failsoft(client, monkeypatch):
    async def fake_run(cypher, **params):
        return None  # node not found / KG down

    monkeypatch.setattr(kg, "_run", fake_run)
    r = await client.get("/api/research/neighbors?name=does-not-exist")
    assert r.status_code == 200
    d = r.json()
    assert d["found"] is False and d["degree"] == 0 and d["neighbors"] == []


async def test_neighbors_requires_name_and_caps_limit(client):
    assert (await client.get("/api/research/neighbors")).status_code == 422  # name required
    assert (await client.get("/api/research/neighbors?name=x&limit=201")).status_code == 422


async def test_ttlcache_is_bounded_lru():
    from app.cache import TTLCache

    c = TTLCache(ttl_seconds=999, max_entries=2)
    calls = {"n": 0}

    async def producer():
        calls["n"] += 1
        return calls["n"]

    await c.get_or_set("a", producer)
    await c.get_or_set("b", producer)
    await c.get_or_set("c", producer)  # evicts "a" (oldest)
    assert len(c._store) == 2 and len(c._locks) <= 2
    assert "a" not in c._store and "c" in c._store
    # "a" was evicted → recompute (producer runs again), proving no unbounded keep
    n_before = calls["n"]
    await c.get_or_set("a", producer)
    assert calls["n"] == n_before + 1
