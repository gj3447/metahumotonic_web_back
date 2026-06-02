"""KG endpoints serve fallback values (no live Neo4j in CI)."""


async def test_stats_shape(client):
    r = await client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    for key in ("nodes", "rels", "labels", "relTypes", "domains", "skills"):
        assert key in data and isinstance(data[key], int)
    assert data["nodes"] > 0


async def test_domains_list(client):
    r = await client.get("/api/domains")
    assert r.status_code == 200
    domains = r.json()
    assert isinstance(domains, list) and domains
    assert {"name", "displayName", "nodeCount"} <= set(domains[0])


async def test_skills_include_seven_commanders(client):
    r = await client.get("/api/skills")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    seven = {"prometheus", "eureka", "longinus", "occam", "naesengmoon", "jaebaeman", "harness"}
    assert seven <= names, f"missing commanders: {seven - names}"
