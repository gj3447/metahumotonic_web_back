"""ClickHouse backend — the permissively-licensed SQL driver (Tier-2 #6).

Why ClickHouse: the prom12 OSS study found "query portability is a myth" — OTLP
*write* is portable, *read* is per-backend, and of all the read dialects only SQL
ports cleanly across stores (OpenObserve / ClickHouse / SigNoz). ClickHouse is the
strongest SQL fit for ooptdd's count/cardinality-with-filter contract and is
**Apache-2.0** (no AGPL exposure, unlike OpenObserve/Loki). A self-hosted SigNoz
exposes the same ClickHouse tables, so this driver doubles as the SigNoz driver.

It speaks the ClickHouse **HTTP interface** and, like the OpenObserve driver,
selects whole rows (``SELECT *``) so the smart filtering (``where``, ``must_order``,
``present``, counts) stays in Python — identical for every backend. The query is a
**parameterized** statement (``{cid:String}``) so the correlation id can never break
out into SQL.

Configuration is **environment-only** (a published package must not ship a host):
``OOPTDD_CH_URL`` (e.g. ``http://host:8123``) required; optional ``OOPTDD_CH_USER``
(default ``default``), ``OOPTDD_CH_PASSWORD``, ``OOPTDD_CH_DATABASE`` (default
``default``). ``[tool.ooptdd]`` may override the env-var *names* and the table, never
carry secrets.

Expected table (the consumer owns DDL; documented, not created here)::

    CREATE TABLE tests (
      cycle_id   String,
      event      String,
      _timestamp DateTime64(6) DEFAULT now64(6),
      data       String                                  -- the full JSON envelope
    ) ENGINE = MergeTree ORDER BY (cycle_id, _timestamp);
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from .base import QueryResult, _raise_for_status


class ClickHouseBackend:
    default_lookback_s = 3600
    default_future_buffer_s = 300  # +5 min: absorb receive-time / clock-skew race

    def __init__(
        self,
        *,
        table: str = "tests",
        database: str | None = None,
        url_env: str = "OOPTDD_CH_URL",
        user_env: str = "OOPTDD_CH_USER",
        password_env: str = "OOPTDD_CH_PASSWORD",
        timeout: float = 15.0,
        max_rows: int = 1_000_000,
        opener=None,
        **_ignored,
    ):
        self.table = table
        self.database = database or os.getenv("OOPTDD_CH_DATABASE", "default")
        self.url_env = url_env
        self.user_env = user_env
        self.password_env = password_env
        self.timeout = timeout
        # Bound a single read so an unbounded SELECT can't OOM; exceeding it surfaces
        # complete=False (incomplete evidence) instead of silently returning a subset.
        self.max_rows = max_rows
        # opener(request, timeout) injection lets tests exercise this driver with no network.
        self._open = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))

    def _base(self) -> str:
        base = os.getenv(self.url_env, "")
        if not base:
            raise ValueError(
                f"{self.url_env} is required for the clickhouse backend "
                f"(e.g. {self.url_env}=http://<host>:8123). No baked default."
            )
        return base.rstrip("/")

    def _headers(self) -> dict:
        h = {}
        user = os.getenv(self.user_env, "default")
        h["X-ClickHouse-User"] = user
        pw = os.getenv(self.password_env)
        if pw:
            h["X-ClickHouse-Key"] = pw
        return h

    def _post(self, params: dict, body: bytes, headers: dict):
        url = f"{self._base()}/?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        return self._open(req, timeout=self.timeout)

    def ship(self, events: list[dict]) -> None:
        if not events:
            return
        # INSERT … FORMAT JSONEachRow: one JSON object per line. We carry the whole
        # envelope in `data` plus the two indexed columns the schema needs.
        rows = []
        for e in events:
            cid = e.get("cycle_id") or e.get("cid") or e.get("correlation_id") or ""
            rows.append(json.dumps(
                {"cycle_id": cid, "event": e.get("event", ""), "data": json.dumps(e)},
                ensure_ascii=False,
            ))
        body = (f"INSERT INTO {self.table} FORMAT JSONEachRow\n" + "\n".join(rows)).encode()
        headers = {**self._headers(), "Content-Type": "text/plain; charset=utf-8"}
        with self._post({"database": self.database}, body, headers) as r:
            _raise_for_status(r)  # a dropped INSERT must be a loud ship failure, not silent

    def query(self, cid: str, *, since_us: int, until_us: int) -> QueryResult:
        try:
            base = self._base()  # noqa: F841 — validates config before the network call
        except ValueError as exc:
            return QueryResult(reachable=False, error=f"{type(exc).__name__}: {exc}")
        # SELECT * (whole rows) with a *parameterized* cid — injection-safe. Time-window
        # bounding is store-receive-stamped; the cid is the real discriminator (one cid per
        # run), matching the OpenObserve driver. FORMAT JSON yields {"data":[...]}.
        # LIMIT max_rows+1 bounds the read: if more than max_rows rows match we learn the set
        # is incomplete (surfaced as complete=False) instead of unbounded-loading or silently
        # returning a subset. The cid stays a parameter (injection-safe).
        sql = (f"SELECT * FROM {self.table} WHERE cycle_id = {{cid:String}} "
               f"AND _timestamp >= fromUnixTimestamp64Micro({{since:Int64}}) "
               f"AND _timestamp <= fromUnixTimestamp64Micro({{until:Int64}}) "
               f"LIMIT {self.max_rows + 1} FORMAT JSON")
        params = {
            "database": self.database,
            "query": sql,
            "param_cid": cid,
            "param_since": since_us,
            "param_until": until_us,
            "default_format": "JSON",
        }
        headers = {**self._headers(), "Content-Type": "text/plain; charset=utf-8"}
        try:
            with self._post(params, b"", headers) as r:
                _raise_for_status(r)
                payload = json.loads(r.read().decode())
        except Exception as exc:
            return QueryResult(reachable=False, error=f"{type(exc).__name__}: {exc}")
        data = payload.get("data", [])
        complete = len(data) <= self.max_rows
        events = []
        for row in data[:self.max_rows]:
            # rows carry the original envelope in `data`; unwrap it so `where`/counts see
            # the real fields. A row without `data` (custom schema) is passed through as-is.
            raw = row.get("data") if isinstance(row, dict) else None
            if isinstance(raw, str):
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    ev = dict(row)
            else:
                ev = dict(row)
            ev.setdefault("_timestamp", row.get("_timestamp") if isinstance(row, dict) else None)
            ev["_seq"] = len(events)  # deterministic tie-break: preserve server return order
            events.append(ev)
        return QueryResult(reachable=True, events=events, complete=complete)
