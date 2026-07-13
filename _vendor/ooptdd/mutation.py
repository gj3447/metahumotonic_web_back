"""Gate mutation testing — does the gate actually discriminate, or is it vacuously green?

ooptdd's honest limit: *"a gate that only checks existence won't catch a wrong value."*
A gate that asserts ``present: [{event: cycle}]`` passes whether the cycle's verdict is
PASS or NG — it has a blind spot. This module quantifies that. Given a *passing*
(events, gate) pair, it derives the deviations the gate **should** catch and re-runs the
gate on each:

  * ``drop:<x>``     — remove the events satisfying a required expectation. A gate that
                       required them must go RED.
  * ``corrupt:<x>``  — for a ``where``-constrained expectation, change the matched field
                       value. A gate that constrains that value must go RED; one that only
                       checks existence stays GREEN — a **surviving mutant = a blind spot**.
  * ``inject_error`` — add an ERROR-level record. Caught only when the gate forbids errors
                       (``forbid_errors`` / ``OOPTDD_FORBID_ERRORS`` / an ``absent`` rule).

The mutation *score* is caught / total; survivors name exactly which deviation the gate
waved through. Ordering mutations (reorder) are out of scope here: the in-memory backend
stamps one timestamp per ``ship``, so reorder can't be observed — use a backend with real
per-event timestamps for that.

This is the Schemathesis idea (systematic spec-derived negative cases) applied to gates
rather than APIs. From the ooptdd-oss prometheus cycle (A5,
seed-ooptdd-negwing-mutant-allowlist-20260618).
"""
from __future__ import annotations

import os

from .backends.memory import MemoryBackend, reset
from .engine.gate import _matches, _resolve_matcher, evaluate

_SENTINEL = "__ooptdd_mutant__"


def _forbids_errors(spec: dict) -> bool:
    fe = spec.get("forbid_errors")
    if fe is None:
        env = str(os.getenv("OOPTDD_FORBID_ERRORS", "")).strip().lower()
        fe = env in {"1", "true", "yes", "on"}
    if fe:
        return True
    indicators = spec.get("indicators") or {}
    for rule in spec.get("expect", []):
        raw = rule.get("absent", rule.get("forbid"))
        if raw is None:
            continue
        for m in (raw if isinstance(raw, list) else [raw]):
            _, where = _resolve_matcher(m, indicators)
            lvl = str(where.get("level", "")).upper()
            if lvl in {"ERROR", "CRITICAL"} or "level" not in where:
                return True
    return False


def _label(event, where) -> str:
    return event or ("where:" + ",".join(f"{k}={v}" for k, v in where.items())) or "(any)"


def derive_mutations(events: list[dict], spec: dict) -> list[tuple[str, list[dict]]]:
    """Labeled mutant event-lists derived from the gate's own expectations — each a
    deviation the gate ought to catch. Deduplicated by label."""
    indicators = spec.get("indicators") or {}
    out: list[tuple[str, list[dict]]] = []
    seen: set[str] = set()

    def add(label: str, mevents: list[dict]) -> None:
        if label not in seen and mevents != events:
            seen.add(label)
            out.append((label, mevents))

    def cover(matcher: dict) -> None:
        event, where = _resolve_matcher(matcher, indicators)
        # drop: remove every event satisfying this expectation
        add("drop:" + _label(event, where), [e for e in events if not _matches(e, event, where)])
        # corrupt: flip the first where-field so matching events no longer match (value check)
        if where:
            field = next(iter(where))
            add(
                f"corrupt:{event or 'where'}.{field}",
                [{**e, field: _SENTINEL} if _matches(e, event, where) else e for e in events],
            )

    for rule in spec.get("expect", []):
        if rule.get("optional") or rule.get("pending"):
            continue  # non-gating; a gate that ignores it by design isn't a blind spot
        if "present" in rule:
            for m in rule["present"]:
                cover(m)
        elif "must_order" in rule or "trajectory" in rule:
            seq = rule.get("must_order") or rule.get("trajectory")
            if seq:
                cover({"event": seq[0]})  # drop the first required step
        elif not any(k in rule for k in
                     ("absent", "forbid", "ratioMetric", "conforms", "heartbeat")):
            cover(rule)  # a plain count/where rule

    if _forbids_errors(spec):
        add("inject_error", [*events, {"event": "_mutant.error", "level": "ERROR",
                                       "error": "injected mutant fault"}])
    return out


def _run(events: list[dict], spec: dict, run_cid: str) -> bool:
    b = MemoryBackend()
    b.ship([{**e, "cid": run_cid} for e in events])
    return bool(evaluate(b, {**spec, "cid": run_cid})["ok"])


def mutation_report(events: list[dict], spec: dict) -> dict:
    """Run the gate on ``events`` (baseline) and on each derived mutant.

    Returns ``{baseline_green, mutations:[{mutation, caught}], survivors:[label], score, n}``.
    ``baseline_green=False`` means the inputs don't even pass — the score is meaningless
    until you fix that. ``survivors`` are the deviations the gate let through: its blind
    spots. Uses (and resets) the in-memory store; each run gets a unique cid so runs never
    cross-contaminate.
    """
    reset()
    baseline_green = _run(events, spec, "mut-baseline")
    rows = []
    for i, (label, mevents) in enumerate(derive_mutations(events, spec)):
        rows.append({"mutation": label, "caught": not _run(mevents, spec, f"mut-{i}")})
    survivors = [r["mutation"] for r in rows if not r["caught"]]
    score = round((len(rows) - len(survivors)) / len(rows), 3) if rows else 1.0
    return {
        "baseline_green": baseline_green,
        "mutations": rows,
        "survivors": survivors,
        "score": score,
        "n": len(rows),
    }
