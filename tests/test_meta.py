async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_root_lists_endpoints(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "/api/feedback" in r.json()["endpoints"]
    assert "/ready" in r.json()["endpoints"]
    assert "/api/v1/ontology/conflicts" in r.json()["endpoints"]


async def test_ready_is_degraded_tolerant(client):
    # neo4j_live is false in CI → ready, not degraded
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["degraded"] is False
