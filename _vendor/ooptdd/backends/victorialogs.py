"""VictoriaLogs backend — a schema-free log store driver.

Writes events as JSON lines (``POST /insert/jsonline``) and reads them back with
LogsQL (``GET /select/logsql/query``). VictoriaLogs is Apache-2.0, schema-free, and
filters arbitrary fields cheaply by correlation id — a clean fit for ooptdd's
"fetch every event for this cid in a window" read pattern.

Configuration is **environment-only** — no URL is ever baked into code (a published
package must not ship someone's host). Required: ``OOPTDD_VL_URL``
(e.g. ``http://<host>:9428``). Optional basic auth (VictoriaLogs is commonly run
auth-less behind a proxy): ``OOPTDD_VL_USER`` / ``OOPTDD_VL_PASSWORD`` — only sent
when a password is present. ``cycle_id`` is shipped as a stream field so the
``cycle_id:=`` filter is index-cheap.

From the ooptdd-oss prometheus cycle (A12, seed-ooptdd-backend-victorialogs-20260618).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

from .base import QueryResult, _raise_for_status


def _logsql_str(value: str) -> str:
    """Escape a value for a LogsQL double-quoted phrase (``field:="..."``): backslash first,
    then the double-quote. A cid containing a quote can then never break out of the filter
    or be silently mangled — the injection/breakage fix, mirroring ClickHouse's parameter
    binding and OpenObserve's quote-doubling."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_time_us(value) -> int | None:
    """Best-effort RFC3339 (VictoriaLogs ``_time``) -> epoch microseconds. Returns None
    on anything unparseable, so a single odd row never breaks must_order ordering."""
    if not isinstance(value, str) or not value:
        return None
    s = value.replace("Z", "+00:00")
    # fromisoformat rejects nanoseconds; truncate the fractional part to 6 digits.
    if "." in s:
        head, _, tail = s.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                frac += ch
            else:
                rest = tail[i:]
                break
        s = f"{head}.{frac[:6]}{rest}"
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1_000_000)
    except ValueError:
        return None


class VictoriaLogsBackend:
    default_lookback_s = 3600
    default_future_buffer_s = 300  # +5 min: absorb receive-time / clock-skew race
    queryable = True  # LogsQL read side over /select/logsql/query

    def __init__(
        self,
        *,
        url_env: str = "OOPTDD_VL_URL",
        user_env: str = "OOPTDD_VL_USER",
        password_env: str = "OOPTDD_VL_PASSWORD",
        stream_field: str = "cycle_id",
        timeout: float = 15.0,
        max_rows: int = 1_000_000,
        opener=None,
        **_ignored,
    ):
        self.url_env = url_env
        self.user_env = user_env
        self.password_env = password_env
        self.stream_field = stream_field
        self.timeout = timeout
        # LogsQL streams all matches; this bounds how many we ingest so a pathological cid
        # can't OOM. Exceeding it surfaces complete=False rather than silently dropping rows.
        self.max_rows = max_rows
        # opener(request, timeout) injection lets tests exercise this driver offline.
        self._open = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))

    def _base(self) -> str:
        base = os.getenv(self.url_env, "")
        if not base:
            raise ValueError(
                f"{self.url_env} is required for the victorialogs backend "
                f"(e.g. {self.url_env}=http://<host>:9428). No baked default."
            )
        return base.rstrip("/")

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/x-ndjson"}
        pw = os.environ.get(self.password_env)
        if pw:  # auth is optional — VictoriaLogs is often run without it
            user = os.getenv(self.user_env, "")
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        return headers

    def ship(self, events: list[dict]) -> None:
        if not events:
            return
        base = self._base()
        # JSON lines; cycle_id becomes a stream field (cheap to filter), event becomes _msg.
        body = "\n".join(json.dumps(e) for e in events).encode()
        params = urllib.parse.urlencode(
            {"_stream_fields": self.stream_field, "_msg_field": "event"})
        req = urllib.request.Request(
            f"{base}/insert/jsonline?{params}",
            data=body,
            method="POST",
            headers=self._headers(),
        )
        with self._open(req, timeout=self.timeout) as r:
            _raise_for_status(r)  # a dropped ingest must be a loud ship failure, not silent

    def query(self, cid: str, *, since_us: int, until_us: int) -> QueryResult:
        try:
            base = self._base()
        except ValueError as exc:
            return QueryResult(reachable=False, error=f"{type(exc).__name__}: {exc}")
        # LogsQL: exact field match on the correlation id. start/end are unix seconds
        # (VictoriaLogs accepts fractional). SELECT-* equivalent: no field projection, so
        # whole rows come back for the Python-side gate `where:` filters.
        params = urllib.parse.urlencode({
            "query": f'{self.stream_field}:="{_logsql_str(cid)}"',
            "start": f"{since_us / 1_000_000:.6f}",
            "end": f"{until_us / 1_000_000:.6f}",
        })
        headers = {k: v for k, v in self._headers().items() if k == "Authorization"}
        req = urllib.request.Request(
            f"{base}/select/logsql/query?{params}", method="GET", headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as r:
                payload = r.read().decode()
        except Exception as exc:
            return QueryResult(reachable=False, error=f"{type(exc).__name__}: {exc}")
        events = []
        complete = True
        for line in payload.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # skip a malformed row rather than fail the whole read
            if "_timestamp" not in row:
                ts = _parse_time_us(row.get("_time"))
                if ts is not None:
                    row["_timestamp"] = ts
            row["_seq"] = len(events)  # deterministic tie-break: preserve server return order
            events.append(row)
            if len(events) >= self.max_rows:
                complete = False  # ceiling hit — surfaced, never a silent subset
                break
        return QueryResult(reachable=True, events=events, complete=complete)
