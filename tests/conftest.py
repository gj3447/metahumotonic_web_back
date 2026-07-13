from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import trace
from app.config import settings
from app.main import app
from app.routers.feedback import _limiter
from app.store import store


@pytest_asyncio.fixture
async def client():
    # fresh rate-limit window + in-memory store + trace backend per test
    _limiter.reset()
    _limiter.max_events = settings.feedback_max_per_window
    _limiter.window_seconds = settings.feedback_window_seconds
    store.memory.clear()
    trace.reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
