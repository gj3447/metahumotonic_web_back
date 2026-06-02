"""Observability (PROM16 C6): structured JSON logging + Prometheus metrics.

Metrics: `prometheus-fastapi-instrumentator` exposes `/metrics` (request count,
latency histogram, in-progress) for the cluster's Prometheus to scrape.
Logs: structlog renders JSON to stdout (pod logs → log pipeline).
Both are toggleable; no external collector required.
"""

from __future__ import annotations

import logging

from .config import settings


def configure_logging() -> None:
    import structlog

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def instrument(app) -> None:
    if not settings.metrics_enabled:
        return
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(
        should_group_status_codes=True,
        excluded_handlers=["/metrics", "/health", "/ready"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
