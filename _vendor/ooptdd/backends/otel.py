"""OTLP backend — the portable *write* path (best-effort, optional).

OpenTelemetry's OTLP is the one ingest protocol every major store accepts, so
emitting events as OTLP LogRecords is the strategic way to stay
backend-neutral *on write*. The catch (see ``docs/research`` C1/C3): there is no
portable *query* protocol — LogQL, TraceQL, ES-DSL and SQL all differ — so this
driver ships via OTLP but cannot, by itself, read back. Pair it with a
store-specific reader, or use it only where you trust ingest.

Requires the ``otel`` extra (``pip install ooptdd[otel]``).
"""
from __future__ import annotations

import os

from .base import QueryResult


class OtelBackend:
    default_lookback_s = 3600
    default_future_buffer_s = 300
    queryable = False  # OTLP is write-only — no read side, so arrival can't be verified here

    def __init__(
        self,
        *,
        service: str = "ooptdd.tests",
        endpoint_env: str = "OTEL_EXPORTER_OTLP_ENDPOINT",
        simple: bool = False,
        exporter=None,
        **_ignored,
    ):
        self.service = service
        self.endpoint_env = endpoint_env
        # OTel test-exporter discipline (research E #14): a Batch processor buffers and
        # exports off-thread, which makes "ship then read back" timing flaky — the same
        # reason the OTel SDKs tell you to use a *simple* (synchronous) processor in tests.
        # ``simple=True`` swaps to SimpleLogRecordProcessor so each emit exports inline;
        # combined with the force_flush below, a hermetic test sees a deterministic ingest.
        self.simple = simple
        # An injectable log-record exporter: the default (None) ships OTLP over the wire; tests
        # (and the write-only conformance kit) pass an in-memory exporter to capture what shipped.
        self._exporter = exporter
        self._logger = None

    def _ensure(self):
        if self._logger is not None:
            return
        try:
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import (
                BatchLogRecordProcessor,
                SimpleLogRecordProcessor,
            )
            from opentelemetry.sdk.resources import Resource
        except ImportError as exc:  # pragma: no cover - exercised only with extra
            raise RuntimeError(
                "the otel backend needs `pip install ooptdd[otel]`"
            ) from exc
        exporter = self._exporter
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            exporter = OTLPLogExporter()
        provider = LoggerProvider(resource=Resource.create({"service.name": self.service}))
        proc = SimpleLogRecordProcessor if self.simple else BatchLogRecordProcessor
        provider.add_log_record_processor(proc(exporter))
        # Instance-scoped logger, NOT the process-global set_logger_provider: a driver instance
        # owns its own provider so an injected exporter actually receives this instance's emits
        # (the global is a singleton — a second backend would silently emit to the first's).
        self._provider = provider
        self._logger = provider.get_logger(__name__)

    def ship(self, events: list[dict]) -> None:
        if not events:
            return
        # The endpoint is only needed for the default OTLP exporter; an injected exporter
        # (e.g. an in-memory one in conformance tests) ships nowhere over the wire.
        if self._exporter is None and not os.getenv(self.endpoint_env):
            raise ValueError(f"{self.endpoint_env} is required for the otel backend.")
        self._ensure()
        from opentelemetry._logs import SeverityNumber

        scalar = (str, int, float, bool)
        for ev in events:
            sev = SeverityNumber.ERROR if ev.get("level") == "ERROR" else SeverityNumber.INFO
            # Modern logs API: emit the fields directly and let the SDK build the LogRecord. The
            # old ``emit(LogRecord(...))`` form and the ``LogRecord`` import location both moved
            # across opentelemetry-sdk releases, silently breaking this driver — now caught by the
            # write-only conformance test (tests/test_otel_backend.py).
            self._logger.emit(
                body=ev.get("event", "event"),
                severity_number=sev,
                attributes={k: v for k, v in ev.items() if isinstance(v, scalar)},
            )
        self._provider.force_flush()

    def query(self, cid: str, *, since_us: int, until_us: int) -> QueryResult:
        # OTLP has no read side; queries are store-specific. Return inconclusive.
        return QueryResult(reachable=False)
