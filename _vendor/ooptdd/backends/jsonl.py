"""JSONL backend — persistent, cross-process, zero-infra queryable store.

:class:`~ooptdd.backends.memory.MemoryBackend` 는 process-global 이라 한 프로세스 안에서만
ship→query 가 보인다. 에이전트 루프가 여러 프로세스/세션에 걸치면(legion 한 프로세스가 ship,
ooptdd verify 가 다른 프로세스에서 read) MemoryBackend 로는 positive-arrival 을 검증할 수 없다.
JSONL backend 는 한 파일을 store 로 써서 **외부 서비스 0, 의존성 0** 으로 cross-process
read+write 를 준다 — OTLP(write-only) 갭의 fallback 읽기 경로.

honesty(METHODOLOGY single-authority residue): Phoenix/Langfuse 같은 관측 store 어댑터는
라이브 엔드포인트로 wire-format 을 검증해야 green-and-blind(self-consistency≠correctness) 를
피한다. 그 전까지 이 로컬 queryable backend 로 3치 LTL₃ positive-arrival 을 *실제로* 돌린다.

reachable/complete 정직:
  - 파일 없음 = store 는 도달 가능, 아직 아무것도 ship 안 됨 → reachable=True, [] (absent ⊥).
  - 파일 읽기 IO 실패 = store 에 못 물음 → reachable=False (inconclusive ?).

# KG: ooptdd-jsonl-queryable-backend-2026-06-27 (durable-engines-deepdive: DBOS Postgres-only 와
#     동형의 'infra 최소' 원칙을 read 경로에 적용)
"""
from __future__ import annotations

import itertools
import json
import os
import time

from .base import BackendCaps, QueryResult

# process-global monotonic sequence per shipped event — breaks same-batch wall-clock ties (see
# backends/memory.py). Survives across ships; a fresh process restarts at 0 (fine, ordering is
# only compared within one query's events).
_SEQ = itertools.count()


class JsonlBackend:
    """파일(JSON Lines) 기반 영속 queryable backend. cid 는 동등 비교(=injection 불가)."""

    default_lookback_s = 3600
    default_future_buffer_s = 0
    queryable = True
    # 전체 파일을 한 번에 읽어 Python 에서 필터 → 항상 complete (paging 없음), where 는 상위에서.
    caps = BackendCaps(queryable=True, paginates=False, supports_where=True)

    def __init__(
        self,
        *,
        path: str | None = None,
        path_env: str = "OOPTDD_JSONL_PATH",
        **_ignored,
    ):
        self.path = path or os.getenv(path_env, "")
        self.path_env = path_env
        if not self.path:
            raise ValueError(
                f"{path_env} (or path=) is required for the jsonl backend "
                f"(e.g. {path_env}=/tmp/ooptdd-events.jsonl)."
            )

    def identity(self) -> str:
        """relocation 감지용 안정 identity (절대경로). ports.backend_identity 가 읽는다."""
        return f"jsonl:{os.path.abspath(self.path)}"

    def ship(self, events: list[dict]) -> None:
        if not events:
            return
        now_us = int(time.time() * 1_000_000)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # append-only: 한 줄 = {_stored_us, _stored_seq, ev}. ensure_ascii=False 로 한글 보존.
        with open(self.path, "a", encoding="utf-8") as f:
            for ev in events:
                rec = {"_stored_us": now_us, "_stored_seq": next(_SEQ), "ev": ev}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def query(self, cid: str, *, since_us: int, until_us: int) -> QueryResult:
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return QueryResult(reachable=True, events=[])  # store 도달 가능, 아직 비어있음(absent)
        except OSError:
            return QueryResult(reachable=False)  # store 에 못 물음 → inconclusive
        hits: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 동시 append 의 부분 마지막 줄 등은 관대히 skip
            ts = rec.get("_stored_us", 0)
            seq = rec.get("_stored_seq", 0)
            ev = rec.get("ev", {})
            ev_cid = ev.get("cid") or ev.get("correlation_id") or ev.get("cycle_id") or ""
            if ev_cid == cid and since_us <= ts <= until_us:
                # store-receive time 을 _timestamp 로 스탬프 (must_order 통일, memory/OO 와 동일);
                # _seq 로 같은-배치(동일 ts) 이벤트의 순서를 보존해 tie-blindness 를 깬다.
                hits.append({**ev, "_timestamp": ts, "_seq": seq})
        return QueryResult(reachable=True, events=hits, complete=True)
