"""OpenObserve backend — the reference network driver.

Writes events to an OpenObserve stream (``POST /api/{org}/{stream}/_json``) and
reads them back with SQL (``POST /api/{org}/_search``). SQL is first-class here:
"count events where cid = X in a time window" maps cleanly onto it.

Configuration is **environment-only** — no URLs or credentials are ever baked
into code or config files (a published package must not ship someone's host).
Required: ``OOPTDD_OO_URL`` and ``OOPTDD_OO_PASSWORD``. Optional:
``OOPTDD_OO_USER`` (default ``root``), ``OOPTDD_OO_ORG`` (default ``default``).

Settings passed via ``[tool.ooptdd]`` may override the *names* of those env
vars or the org, but never carry secrets.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request

from .base import QueryResult, _raise_for_status


class OpenObserveBackend:
    default_lookback_s = 3600
    default_future_buffer_s = 300  # +5 min: absorb receive-time / clock-skew race
    queryable = True  # SQL read side over /_search

    def __init__(
        self,
        *,
        stream: str = "tests",
        org: str | None = None,
        url_env: str = "OOPTDD_OO_URL",
        user_env: str = "OOPTDD_OO_USER",
        password_env: str = "OOPTDD_OO_PASSWORD",
        timeout: float = 15.0,
        page_size: int = 1000,
        max_rows: int = 1_000_000,
        opener=None,
        **_ignored,
    ):
        self.stream = stream
        self.org = org or os.getenv("OOPTDD_OO_ORG", "default")
        self.url_env = url_env
        self.user_env = user_env
        self.password_env = password_env
        self.timeout = timeout
        # Read-back is paged to completion (offset/size), never silently capped at one
        # page. `max_rows` is only a runaway guard for a pathologically huge cid; hitting
        # it sets QueryResult.truncated so the verdict layer refuses a clean pass rather
        # than undercounting in silence.
        self.page_size = page_size
        self.max_rows = max_rows
        # opener(request, timeout) injection lets tests exercise this driver
        # without a network.
        self._open = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))

    def _endpoint(self) -> tuple[str, str, str]:
        base = os.getenv(self.url_env, "")
        if not base:
            raise ValueError(
                f"{self.url_env} is required for the openobserve backend "
                f"(e.g. {self.url_env}=http://<host>:5080). No baked default."
            )
        pw = os.environ.get(self.password_env)
        if not pw:
            raise ValueError(f"{self.password_env} is required (env-only secret).")
        user = os.getenv(self.user_env, "root")
        auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        return base.rstrip("/"), self.org, auth

    def ship(self, events: list[dict]) -> None:
        if not events:
            return
        base, org, auth = self._endpoint()
        req = urllib.request.Request(
            f"{base}/api/{org}/{self.stream}/_json",
            data=json.dumps(events).encode(),
            method="POST",
            headers={"Authorization": auth, "Content-Type": "application/json"},
        )
        with self._open(req, timeout=self.timeout) as r:
            # Surface a non-2xx so a dropped ingest is a *loud* ship failure (caught by the
            # caller as a warning), not a silent success.
            _raise_for_status(r)

    def query(self, cid: str, *, since_us: int, until_us: int) -> QueryResult:
        try:
            base, org, auth = self._endpoint()
        except ValueError as exc:
            return QueryResult(reachable=False, error=f"{type(exc).__name__}: {exc}")
        # SELECT * so whole rows come back: arbitrary fields (verdict, level, …) for gate
        # `where:` filters and `_timestamp` for `must_order`. The cid is a single-quoted SQL
        # string literal, so it is escaped by doubling embedded quotes — it can never break
        # out of the literal (injection-safe), matching the parameterized ClickHouse driver.
        safe_cid = cid.replace("'", "''")
        sql = f"SELECT * FROM {self.stream} WHERE cycle_id = '{safe_cid}'"
        events: list[dict] = []
        offset = 0
        complete = True
        # Page to completion: OpenObserve caps a single response, so a cid with more events
        # than one page would otherwise be SILENTLY undercounted — the exact silent loss
        # this tool exists to catch. Loop on offset until a short page (no more rows).
        while True:
            body = json.dumps({"query": {
                "sql": sql, "start_time": since_us, "end_time": until_us,
                "from": offset, "size": self.page_size,
            }}).encode()
            req = urllib.request.Request(
                f"{base}/api/{org}/_search",
                data=body,
                method="POST",
                headers={"Authorization": auth, "Content-Type": "application/json"},
            )
            try:
                with self._open(req, timeout=self.timeout) as r:
                    # A non-2xx search is a loud failure, not "0 hits": without this an error
                    # body lacking a `hits` key would read as an empty result set → a false
                    # `absent` (reachable=True, 0 events). Mirrors ship()'s _raise_for_status.
                    _raise_for_status(r)
                    hits = json.loads(r.read().decode()).get("hits", [])
            except Exception as exc:
                # A failure mid-paging means we don't have the complete set: if we already
                # have some rows it's an incomplete read, else fully unreachable.
                err = f"{type(exc).__name__}: {exc}"
                if offset == 0:
                    return QueryResult(reachable=False, error=err)
                return QueryResult(reachable=True, events=events, complete=False, error=err)
            for h in hits:
                h["_seq"] = len(events)  # deterministic tie-break: preserve server return order
                events.append(h)
            if len(hits) < self.page_size:
                break  # short page → the result set is exhausted (complete read)
            offset += len(hits)
            if offset >= self.max_rows:
                complete = False  # runaway guard hit — surfaced, never silent
                break
        return QueryResult(reachable=True, events=events, complete=complete)
