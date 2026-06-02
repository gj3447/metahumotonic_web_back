async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_root_lists_endpoints(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "/api/feedback" in r.json()["endpoints"]
