from unittest.mock import AsyncMock
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.kg import kg

@pytest.mark.asyncio
async def test_one_malformed_domain_does_not_destroy_public_list(monkeypatch):
    rows = [dict(name="valid", displayName="Valid", nodeCount=3, description=""),
            dict(name="incomplete", displayName=None, nodeCount=None, description="")]
    monkeypatch.setattr(kg, "_run", AsyncMock(return_value=rows))
    kg._cache.clear()
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        response = await c.get("/api/domains")
    assert response.status_code == 200
    assert [row["name"] for row in response.json()] == ["valid"]
    assert response.headers["X-Data-Quality"] == "partial"
    assert response.headers["X-Records-Omitted"] == "1"

@pytest.fixture(autouse=True)
def clear_domain_cache():
    kg._cache.clear()
    yield
    kg._cache.clear()

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"name": "bad", "displayName": None, "nodeCount": None},
    {"name": "bad", "displayName": "Bad", "nodeCount": "unknown"},
    {"name": None, "displayName": "Bad", "nodeCount": 4},
    {}, None,
])
async def test_invalid_domain_fields_are_isolated(monkeypatch, bad):
    rows=[dict(name="good", displayName="Good", nodeCount=0),bad]
    monkeypatch.setattr(kg,"_run",AsyncMock(return_value=rows))
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
        r=await c.get("/api/domains")
    assert r.status_code==200
    assert r.json()==[dict(name="good",displayName="Good",nodeCount=0,description="")]
    assert r.headers["X-Records-Omitted"]=="1"
    assert r.headers["X-Data-Source"]=="live"
    assert r.headers["Cache-Control"]=="no-store"

@pytest.mark.asyncio
async def test_all_invalid_is_explicit_unavailable_not_fake_empty_success(monkeypatch):
    monkeypatch.setattr(kg,"_run",AsyncMock(return_value=[{}]))
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
        r=await c.get("/api/domains")
    assert r.status_code==503
    assert r.json()=={"reason":"domain_data_unavailable"}
    assert r.headers["X-Data-Quality"]=="unavailable"

@pytest.mark.asyncio
@pytest.mark.parametrize("rows,source", [([],"live"),(None,"snapshot")])
async def test_empty_live_is_distinct_from_unavailable_snapshot(monkeypatch,rows,source):
    monkeypatch.setattr(kg,"_run",AsyncMock(return_value=rows))
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
        r=await c.get("/api/domains")
    assert r.status_code==200
    assert r.headers["X-Data-Source"]==source
    assert r.headers["X-Records-Omitted"]=="0"
    assert (r.json()==[]) == (rows==[])

@pytest.mark.asyncio
async def test_cache_preserves_quality_metadata(monkeypatch):
    run=AsyncMock(return_value=[dict(name="good",displayName="Good",nodeCount=2),{}])
    monkeypatch.setattr(kg,"_run",run)
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
        a=await c.get("/api/domains"); b=await c.get("/api/domains")
    assert a.json()==b.json()
    assert b.headers["X-Data-Quality"]=="partial"
    assert b.headers["X-Records-Omitted"]=="1"
    assert run.await_count==1

def test_projection_is_pure_and_does_not_invent_missing_values():
    import copy
    from app.domain_projection import project_domains
    rows=[dict(name="bad",displayName=None,nodeCount=None)]
    before=copy.deepcopy(rows)
    r=project_domains(rows,[])
    assert rows==before
    assert r.items==() and r.omitted==1

@pytest.mark.asyncio
async def test_malformed_input_values_are_not_logged(monkeypatch,caplog):
    import logging
    marker="SYNTHETIC_PRIVATE_INPUT_MUST_NOT_LEAK"
    monkeypatch.setattr(kg,"_run",AsyncMock(return_value=[dict(name=marker,displayName=None,nodeCount=None)]))
    with caplog.at_level(logging.WARNING,logger="mhb.kg"):
        await kg.get_domain_projection()
    assert marker not in caplog.text
    assert "omitted=1" in caplog.text
