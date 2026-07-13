from __future__ import annotations

import sys
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import trace
from app.config import settings
from app.main import app
from app.routers.feedback import _limiter
from app.store import store


@pytest.fixture(autouse=True)
def _structlog_no_bleed():
    """Isolate structlog global config across tests. app.main calls
    configure_logging() (cache_logger_on_first_use=True) at import, and modules
    (middleware/kg_proxy) hold module-level get_logger() proxies that cache their
    bound logger on first use. Once cached, a later capture_logs() can't intercept
    → test_request_logging sees zero events (StopIteration). Before each test,
    turn caching off and drop any already-cached proxy so capture_logs() always
    intercepts; restore defaults after.
    """
    import structlog

    structlog.configure(cache_logger_on_first_use=False)
    for _mod in ("app.middleware", "app.routers.kg_proxy"):
        _m = sys.modules.get(_mod)
        _proxy = getattr(_m, "log", None)
        if _proxy is not None and hasattr(_proxy, "_logger"):
            _proxy._logger = None  # force re-bind on next use
    yield
    structlog.reset_defaults()


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
