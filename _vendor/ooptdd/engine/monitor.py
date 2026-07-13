"""The evaluation kernel: streaming LTL₃ / MTL monitor automata.

Every gate check compiles to a :class:`Monitor` — a small automaton that is fed the
event stream **in store-timestamp order, one event at a time**. After each event the
monitor reports a *three-valued* LTL₃ verdict over the prefix seen so far (Bauer,
Leucker & Schallhart, TOSEM 2011):

    SAT  (⊤)  no extension of this prefix can falsify the property   → settled true
    VIOL (⊥)  no extension of this prefix can satisfy it             → settled false
    PEND (?)  the prefix is consistent with both outcomes            → still undecided

A monitor commits to ⊤/⊥ the instant the verdict becomes *inevitable* (e.g. a ``>=N``
count latches SAT the moment the Nth match arrives; an ``absent`` latches VIOL on the
first offender; a ``must_order`` latches VIOL on the first timestamp inversion) and
records the stream index where that happened (``settled_at``). When the stream ends
with a monitor still PEND, it is **collapsed** to a concrete pass/fail using the prefix
as if it were complete — the only sound choice for a bounded run.

The collapse reproduces the original count-based verdict *exactly*, so a gate's final
``ok``/RED is unchanged; what changed is that evaluation is now a genuine incremental
monitor (with anticipatory ⊤/⊥/? and a settle point) instead of a batch count over a
list. The same monitors can therefore be driven from a live event feed — see
:func:`run_monitor` (drive the whole prefix) and :meth:`Monitor.step` (one event).

This module is the kernel: it owns the matching/ordering/comparison primitives. The
gate layer (:mod:`ooptdd.gate`) compiles specs into monitors and re-exports the
primitives for backward compatibility.
"""
from __future__ import annotations

import operator
from collections.abc import Callable

# ── three-valued LTL₃ verdict domain ────────────────────────────────────────────
SAT = "sat"    # ⊤  settled true  — no extension can falsify
VIOL = "viol"  # ⊥  settled false — no extension can satisfy
PEND = "pend"  # ?  undecided     — both outcomes still reachable

# Symbolic comparison operators are native; the OpenSLO/Keptn word forms
# (gte/gt/eq/lte/lt[/ne]) are accepted as aliases so a spec reads like an SLO objective.
_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge, ">": operator.gt, "==": operator.eq,
    "!=": operator.ne, "<=": operator.le, "<": operator.lt,
}
_OP_ALIASES = {
    "gte": ">=", "ge": ">=", "gt": ">", "eq": "==", "ne": "!=",
    "lte": "<=", "le": "<=", "lt": "<",
}


def _norm_op(op) -> str:
    """Map an OpenSLO word operator to its symbolic form; pass symbols through."""
    return _OP_ALIASES.get(str(op), str(op))


def _want(rule: dict):
    """``target`` (OpenSLO) is an alias for ``count``; default 1."""
    if "target" in rule:
        return rule["target"]
    return rule.get("count", 1)


def _matches(ev: dict, event: str | None, where: dict) -> bool:
    """An event matches when its name equals ``event`` (if given) and every ``where``
    field equals the event's value (partial-dict — unlisted keys ignored). ``event=None``
    matches any name."""
    if event is not None and ev.get("event") != event:
        return False
    return all(ev.get(k) == v for k, v in where.items())


def _resolve_matcher(m: dict, indicators: dict) -> tuple[str | None, dict]:
    """Resolve a matcher to ``(event, where)``, expanding an ``indicatorRef`` against the
    spec's ``indicators`` (SLI layer). Inline ``event``/``where`` override/extend it."""
    if "indicatorRef" in m:
        base = indicators.get(m["indicatorRef"], {})
        event = m.get("event", base.get("event"))
        where = {**(base.get("where") or {}), **(m.get("where") or {})}
        return event, where
    return m.get("event"), (m.get("where") or {})


def _matcher_label(event: str | None, where: dict) -> str:
    return event or ("where:" + ",".join(f"{k}={v}" for k, v in where.items())) or "(any)"


def _brief(ev: dict) -> dict:
    """A capped view of an offending event — enough for an RCA to name what leaked
    without dumping a whole payload into the verdict."""
    keys = ("event", "level", "message", "msg", "error", "exc", "_timestamp")
    return {k: ev[k] for k in keys if k in ev} or {"event": ev.get("event")}


def stream_key(ev: dict):
    """Sort key putting events without a store timestamp first, then by ``_timestamp``, then by the
    backend's per-event ``_seq`` — the tie-break that keeps same-batch events (which share one
    wall-clock ``_timestamp``) in ship order. Events without ``_seq`` sort as 0 (concurrent)."""
    ts = ev.get("_timestamp")
    seq = ev.get("_seq")
    return (ts is None, ts if ts is not None else 0, seq if seq is not None else 0)


# ── monitor automata ─────────────────────────────────────────────────────────── #

class Monitor:
    """Base class. Subclasses implement :meth:`step` (consume one event, possibly
    latching a verdict) and :meth:`collapse` (final result dict at end-of-stream)."""

    def __init__(self) -> None:
        self._verdict = PEND
        self._settled_at: int | None = None

    # -- LTL₃ verdict bookkeeping ------------------------------------------------- #
    def _latch(self, verdict: str, idx: int) -> None:
        """First inevitable ⊤/⊥ wins and is permanent (anticipatory semantics)."""
        if self._verdict == PEND:
            self._verdict = verdict
            self._settled_at = idx

    @property
    def verdict(self) -> str:
        return self._verdict

    @property
    def settled_at(self) -> int | None:
        return self._settled_at

    def step(self, ev: dict, idx: int) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def collapse(self, reachable: bool) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def _stamp(self, result: dict) -> dict:
        """Attach the three-valued verdict + settle index to a check result."""
        result["verdict"] = self._verdict
        result["settled_at"] = self._settled_at
        return result


class CountMonitor(Monitor):
    """``event/where op target`` cardinality. Anticipatory by operator:

    ``>=``/``>`` are monotone-true (counts only grow), so they latch SAT the instant
    the threshold is crossed; ``==``/``<=``/``<`` can be irrecoverably broken by one
    more match, so they latch VIOL when exceeded and otherwise stay PEND (collapsing to
    the exact count comparison at end-of-stream)."""

    def __init__(self, event, where, op, want):
        super().__init__()
        self.event, self.where, self.op, self.want = event, where, op, want
        self.got = 0
        # want<=0 with `>=` is satisfied by the empty prefix.
        if op == ">=" and want <= 0:
            self._latch(SAT, -1)

    def step(self, ev, idx):
        if not _matches(ev, self.event, self.where):
            return
        self.got += 1
        if self.op in (">=", ">") and _OPS[self.op](self.got, self.want):
            self._latch(SAT, idx)
        elif self.op == "==" and self.got > self.want:
            self._latch(VIOL, idx)
        elif self.op == "<=" and self.got > self.want:
            self._latch(VIOL, idx)
        elif self.op == "<" and self.got >= self.want:
            self._latch(VIOL, idx)

    def collapse(self, reachable):
        return self._stamp({
            "event": self.event, "where": self.where, "op": self.op,
            "want": self.want, "got": self.got,
            "passed": reachable and _OPS[self.op](self.got, self.want),
            # `>= n` with n<=0 is satisfied by any prefix incl. the empty one — a failure-
            # incapable check. Flagged so the gate excludes it from `gating` (a tautology must
            # not make a gate assert-anything) and lint can warn at author time.
            "tautological": self.op == ">=" and self.want <= 0,
        })


class PresentMonitor(Monitor):
    """Subset-present (``testfixtures.check_present``): each matcher must match ≥1 event.
    Monotone-true — latches SAT once every matcher has fired; order is not asserted."""

    def __init__(self, matchers, indicators):
        super().__init__()
        self._resolved = [_resolve_matcher(m, indicators) for m in matchers]
        self.labels = [_matcher_label(e, w) for e, w in self._resolved]
        self._hit = [False] * len(self._resolved)

    def step(self, ev, idx):
        for i, (event, where) in enumerate(self._resolved):
            if not self._hit[i] and _matches(ev, event, where):
                self._hit[i] = True
        if all(self._hit):
            self._latch(SAT, idx)

    def collapse(self, reachable):
        missing = [lbl for lbl, hit in zip(self.labels, self._hit, strict=True) if not hit]
        return self._stamp({
            "present": self.labels, "missing": missing,
            "passed": reachable and not missing,
        })


class AbsentMonitor(Monitor):
    """Forbid wing — each matcher must match ZERO events. Monotone-false: latches VIOL on
    the first non-allowlisted offender. Offenders are surfaced (capped) for the RCA."""

    def __init__(self, matchers, indicators, allow=None):
        super().__init__()
        self._resolved = [_resolve_matcher(m, indicators) for m in matchers]
        self._allow = [_resolve_matcher(a, indicators) for a in (allow or [])]
        self.labels = [_matcher_label(e, w) for e, w in self._resolved]
        self.offenders: list[dict] = []

    def step(self, ev, idx):
        if any(_matches(ev, a_ev, a_w) for a_ev, a_w in self._allow):
            return
        if any(_matches(ev, event, where) for event, where in self._resolved):
            self.offenders.append(ev)
            self._latch(VIOL, idx)

    def collapse(self, reachable):
        return self._stamp({
            "absent": self.labels, "violations": len(self.offenders),
            "offending": [_brief(o) for o in self.offenders[:5]],
            "passed": reachable and not self.offenders,
        })


class OrderMonitor(Monitor):
    """``must_order`` sequencing: every name must occur and first-occurrence timestamps
    must be non-decreasing in the listed order. First-occurrence times never change once
    set, so a timestamp inversion (or an over-bound gap under MTL ``within_s``) is an
    inevitable VIOL, and once every name has fired in order it is an inevitable SAT."""

    def __init__(self, seq, within_s=None):
        super().__init__()
        self.seq = list(seq)
        self.within_s = within_s
        self.firsts: dict = {name: None for name in self.seq}
        # Ordering is compared on a composite (ts, seq) key so same-batch events (identical ts) are
        # ordered by the backend's per-event seq — not vacuously "concurrent". `firsts` stays plain
        # ts (its reported shape is a contract); the seq only sharpens the *order* comparison.
        self._keys: dict = {name: None for name in self.seq}

    def _firsts_list(self):
        return [(name, self.firsts[name]) for name in self.seq]

    def _order_keys(self):
        return [self._keys[name] for name in self.seq]

    def _ordered_so_far(self) -> bool:
        known = [k for k in self._order_keys() if k is not None]
        return all(known[i] <= known[i + 1] for i in range(len(known) - 1))

    def _gaps_exceeded(self):
        out = []
        if self.within_s is None:
            return out
        bound = self.within_s * 1_000_000
        fl = self._firsts_list()
        for i in range(len(fl) - 1):
            a, b = fl[i][1], fl[i + 1][1]
            if a is not None and b is not None and b - a > bound:
                out.append(f"{fl[i][0]}->{fl[i + 1][0]}")
        return out

    def step(self, ev, idx):
        name = ev.get("event")
        ts = ev.get("_timestamp")
        if name not in self.firsts or ts is None:
            return  # only timestamped, in-sequence events move the automaton
        if self.firsts[name] is None:
            self.firsts[name] = ts
            s = ev.get("_seq")
            self._keys[name] = (ts, s if s is not None else 0)
        # inevitable VIOL: a known first-occurrence is now out of order, or a bounded gap
        # has already been exceeded — neither can be undone by later events.
        if not self._ordered_so_far() or self._gaps_exceeded():
            self._latch(VIOL, idx)
            return
        if all(v is not None for v in self.firsts.values()) and not self._gaps_exceeded():
            self._latch(SAT, idx)

    def collapse(self, reachable):
        fl = self._firsts_list()
        missing = [name for name, ts in fl if ts is None]
        keys = self._order_keys()  # (ts, seq) composite so a same-batch inversion is not "ordered"
        ordered = not missing and all(
            keys[i] <= keys[i + 1] for i in range(len(keys) - 1)
        )
        gaps = self._gaps_exceeded() if ordered else []
        chk = {
            "must_order": list(self.seq), "missing": missing, "ordered": ordered,
            "firsts": dict(self.firsts),
            "passed": reachable and ordered and not gaps,
        }
        if self.within_s is not None:
            chk["within_s"] = self.within_s
            chk["gaps_exceeded"] = gaps
        return self._stamp(chk)


class HeartbeatMonitor(Monitor):
    """Liveness ``G[0,every_s] F event`` (MTL): the event must fire at least once and never
    go silent longer than ``every_s`` between beats. An over-long gap is an inevitable VIOL
    (the gap is fixed once both beats are seen); the property is never inevitably SAT over an
    unbounded future, so it stays PEND until violated or the stream ends."""

    def __init__(self, name, every_s):
        super().__init__()
        self.name = name
        self.every_s = float(every_s)
        self.beats = 0
        self.last_ts: int | None = None
        self.max_gap_us = 0

    def step(self, ev, idx):
        if ev.get("event") != self.name or ev.get("_timestamp") is None:
            return
        ts = ev["_timestamp"]
        self.beats += 1
        if self.last_ts is not None:
            gap = ts - self.last_ts
            self.max_gap_us = max(self.max_gap_us, gap)
            if gap > self.every_s * 1_000_000:
                self._latch(VIOL, idx)
        self.last_ts = ts

    def collapse(self, reachable):
        if self.beats == 0:
            return self._stamp({
                "heartbeat": self.name, "every_s": self.every_s, "beats": 0,
                "max_gap_s": None, "passed": False, "reason": "no_beat",
            })
        ok = self.max_gap_us <= self.every_s * 1_000_000
        return self._stamp({
            "heartbeat": self.name, "every_s": self.every_s, "beats": self.beats,
            "max_gap_s": round(self.max_gap_us / 1_000_000, 6),
            "passed": reachable and ok,
        })


class RatioMonitor(Monitor):
    """OpenSLO ratioMetric ``good/total`` vs ``target``. The ratio is non-monotone (both
    numerator and denominator can grow), so the monitor stays PEND and collapses to the
    final ratio comparison; a zero denominator can't form a ratio → not a pass."""

    def __init__(self, good, total, op, want, indicators):
        super().__init__()
        self.g_event, self.g_where = _resolve_matcher(good, indicators)
        self.t_event, self.t_where = _resolve_matcher(total, indicators)
        self.op, self.want = op, want
        self.good = 0
        self.total = 0

    def step(self, ev, idx):
        if _matches(ev, self.g_event, self.g_where):
            self.good += 1
        if _matches(ev, self.t_event, self.t_where):
            self.total += 1

    def collapse(self, reachable):
        if self.total == 0:
            return self._stamp({
                "ratio": "good/total", "good": self.good, "total": 0, "value": None,
                "op": self.op, "want": self.want, "passed": False,
                "reason": "ratio_total_zero",
            })
        value = self.good / self.total
        return self._stamp({
            "ratio": "good/total", "good": self.good, "total": self.total,
            "value": value, "op": self.op, "want": self.want,
            "passed": reachable and _OPS[self.op](value, self.want),
        })


_REDUCERS = {"sum": sum, "min": min, "max": max, "last": lambda vs: vs[-1]}


class InvariantMonitor(Monitor):
    """Cross-event conservation invariant: ``reduce(field@left) op reduce(field@right)``. Makes a
    *relation between events* expressible (e.g. ``sum(amount@payment) == sum(amount@shipment)``,
    ``count(request) == count(response)``) — the first primitive that catches a value-CONSISTENCY
    bug the count/where checks are blind to (a payment emitted with no/zero amount, a half that
    doesn't balance its whole).

    Honesty boundary: this is INTRA-TRACE and single-authority — it catches inconsistency BETWEEN
    emitted events, **not** emit-vs-truth (both sides are still the system's own self-report).

    Non-monotone (both reductions can move), so it never latches — stays PEND and collapses to the
    final comparison. ``==`` compares within ``tol``. If either side matched ZERO events (or a
    non-count side collected no numeric value), there is nothing to relate → not a pass
    (``invariant_no_evidence``): a conservation law over a stream that never emitted either side
    must not read green."""

    def __init__(self, left, right, op, tol, indicators):
        super().__init__()
        self.l_event, self.l_where = _resolve_matcher(left, indicators)
        self.r_event, self.r_where = _resolve_matcher(right, indicators)
        self.l_reduce, self.r_reduce = left.get("reduce", "sum"), right.get("reduce", "sum")
        self.l_field, self.r_field = left.get("field"), right.get("field")
        self.op, self.tol = op, float(tol)
        self._l_vals: list = []
        self._r_vals: list = []
        self._l_n = self._r_n = 0

    def step(self, ev, idx):
        if _matches(ev, self.l_event, self.l_where):
            self._l_n += 1
            v = ev.get(self.l_field)
            if self.l_reduce != "count" and isinstance(v, (int, float)) and not isinstance(v, bool):
                self._l_vals.append(v)
        if _matches(ev, self.r_event, self.r_where):
            self._r_n += 1
            v = ev.get(self.r_field)
            if self.r_reduce != "count" and isinstance(v, (int, float)) and not isinstance(v, bool):
                self._r_vals.append(v)

    @staticmethod
    def _reduce(reduce_, vals, n):
        if reduce_ == "count":
            return n
        return _REDUCERS[reduce_](vals) if vals else None

    def collapse(self, reachable):
        lv = self._reduce(self.l_reduce, self._l_vals, self._l_n)
        rv = self._reduce(self.r_reduce, self._r_vals, self._r_n)
        base = {
            "invariant": (f"{self.l_reduce}({self.l_field or self.l_event}) {self.op} "
                          f"{self.r_reduce}({self.r_field or self.r_event})"),
            "left": lv, "right": rv, "op": self.op, "tol": self.tol,
        }
        if self._l_n == 0 or self._r_n == 0 or lv is None or rv is None:
            return self._stamp({**base, "passed": False, "reason": "invariant_no_evidence"})
        passed = abs(lv - rv) <= self.tol if self.op == "==" else _OPS[self.op](lv, rv)
        return self._stamp({**base, "passed": reachable and passed})


class MetamorphicMonitor(Monitor):
    """A metamorphic RELATION between two reductions over two matched event sets in the SAME stream
    — the oracle-FREE escape (Chen, Cheung & Yiu 1998): a relation that holds iff the computation is
    correct, needing NO absolute oracle. idempotency (run twice -> same total: ``equal``), scaling
    (2x input -> 2x output: ``scaled`` factor), ``subset`` / ``monotone``. Catches a class of
    wrong-but-strong behavior (a stub that ignores its input no longer scales) with no external
    truth — but BOTH sides are still the system's own emit, so a fault COMMON to both runs
    (deterministic-same-wrong: every retry double-charges by the same factor) is invisible: it
    raises *difficulty*, not grounding. Non-monotone, so it never latches; zero evidence on a side
    is ``metamorphic_no_evidence`` (a no-data run cannot pass)."""

    def __init__(self, a, b, relation, reduce_, field, factor, tol, indicators):
        super().__init__()
        self.a_event, self.a_where = _resolve_matcher(a, indicators)
        self.b_event, self.b_where = _resolve_matcher(b, indicators)
        self.relation, self.reduce, self.field = relation, reduce_, field
        self.factor, self.tol = float(factor), float(tol)
        self._a_vals: list = []
        self._b_vals: list = []
        self._a_n = self._b_n = 0

    def _take(self, ev, event, where, vals):
        if _matches(ev, event, where):
            v = ev.get(self.field)
            if self.reduce != "count" and isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(v)
            return 1
        return 0

    def step(self, ev, idx):
        self._a_n += self._take(ev, self.a_event, self.a_where, self._a_vals)
        self._b_n += self._take(ev, self.b_event, self.b_where, self._b_vals)

    def _reduce(self, vals, n):
        return n if self.reduce == "count" else (_REDUCERS[self.reduce](vals) if vals else None)

    def collapse(self, reachable):
        av, bv = self._reduce(self._a_vals, self._a_n), self._reduce(self._b_vals, self._b_n)
        base = {"metamorphic": self.relation, "relation": self.relation, "reduce": self.reduce,
                "left": av, "right": bv, "tol": self.tol}
        if self.relation == "scaled":
            base["factor"] = self.factor
        if self._a_n == 0 or self._b_n == 0 or av is None or bv is None:
            return self._stamp({**base, "passed": False, "reason": "metamorphic_no_evidence"})
        if self.relation in ("equal", "idempotent"):
            ok = abs(av - bv) <= self.tol
        elif self.relation == "scaled":
            ok = abs(bv - self.factor * av) <= self.tol
        elif self.relation == "subset":
            ok = bv <= av + self.tol
        elif self.relation == "monotone":
            ok = bv >= av - self.tol
        else:
            ok = False
        return self._stamp({**base, "passed": reachable and ok})


class ConformsMonitor(Monitor):
    """Ontology conformance: events of the target type must satisfy their EventType (required
    attrs, value constraints); in closed-world an undeclared in-scope event name is drift.
    Each violation is monotone-false, so the monitor latches VIOL on the first one."""

    def __init__(self, target, ontology, closed_world=None):
        super().__init__()
        self.target = target
        self.ontology = ontology
        self.closed_world = closed_world
        self.checked = 0
        self.violations: list[dict] = []
        self.unknown: list[str] = []
        self._scope_all = target in (None, "*")
        if ontology is not None:
            self._cw = ontology.closed_world if closed_world is None else closed_world
        else:
            self._cw = bool(closed_world)

    def step(self, ev, idx):
        if self.ontology is None:
            return
        name = ev.get("event")
        if not self._scope_all and name != self.target:
            return
        et = self.ontology.get(name)
        if et is None:
            if self._cw and (self._scope_all or name == self.target):
                self.unknown.append(name)
                self.violations.append({"event": name, "index": idx,
                                        "problems": ["unknown_event_type (closed-world drift)"]})
                self._latch(VIOL, idx)
            return
        self.checked += 1
        problems = et.validate(ev)
        if problems:
            self.violations.append({"event": name, "index": idx, "problems": problems})
            self._latch(VIOL, idx)

    def collapse(self, reachable):
        if self.ontology is None:
            return self._stamp({
                "conforms": self.target, "passed": False, "checked": 0,
                "violations": [{"problems": ["ontology_not_loaded "
                                "(set `ontology:` in the spec or pass ontology=)"]}],
                "unknown": [],
            })
        return self._stamp({
            "conforms": self.target, "passed": reachable and not self.violations,
            "checked": self.checked, "violations": self.violations,
            "unknown": self.unknown,
        })


def run_monitor(monitor: Monitor, events: list[dict], reachable: bool) -> dict:
    """Drive ``monitor`` over the whole event prefix (in stream order) and collapse.

    The events are consumed one at a time so the monitor's anticipatory verdict and
    ``settled_at`` reflect a real incremental run; the returned dict is the collapsed
    check result. (Callers that already sorted the stream pass it through unchanged.)
    """
    for idx, ev in enumerate(events):
        monitor.step(ev, idx)
    return monitor.collapse(reachable)


# ── public kernel API: compile a gate rule -> Monitor -> feed a stream ──────────
# This is the single source of truth for "which automaton does this rule denote". The
# batch path (gate.evaluate) and a live/resident path (LiveMonitorSet, fed an event stream
# as it arrives) both go through compile_check, so they can never disagree. Built-in rule
# vocabulary only — custom @check predicates are a gate-layer concern, not kernel monitors.

def compile_check(rule: dict, *, indicators: dict | None = None, ontology=None,
                  allow: list | None = None) -> Monitor:
    """Compile one gate rule into its :class:`Monitor` (the automaton it denotes).

    Detection mirrors the gate's historical key precedence (``forbid``→absent,
    ``trajectory``→must_order); a rule with no predicate keyword is a count check.
    """
    indicators = indicators or {}
    if "absent" in rule or "forbid" in rule:
        raw = rule.get("absent", rule.get("forbid"))
        matchers = raw if isinstance(raw, list) else [raw]
        return AbsentMonitor(matchers, indicators, allow=allow)
    if "heartbeat" in rule:
        return HeartbeatMonitor(rule["heartbeat"], rule["every_s"])
    if "must_order" in rule or "trajectory" in rule:
        seq = rule.get("must_order") or rule.get("trajectory")
        return OrderMonitor(seq, within_s=rule.get("within_s"))
    if "present" in rule:
        return PresentMonitor(rule["present"], indicators)
    if "ratioMetric" in rule:
        spec = rule["ratioMetric"]
        return RatioMonitor(spec.get("good", {}), spec.get("total", {}),
                            _norm_op(rule.get("op", ">=")), float(_want(rule)), indicators)
    if "conforms" in rule:
        return ConformsMonitor(rule["conforms"], ontology, closed_world=rule.get("closed_world"))
    if "invariant" in rule:
        spec = rule["invariant"]
        return InvariantMonitor(spec.get("left", {}), spec.get("right", {}),
                                _norm_op(spec.get("op", rule.get("op", "=="))),
                                spec.get("tol", rule.get("tol", 0.0)), indicators)
    if "metamorphic" in rule:
        spec = rule["metamorphic"]
        rel = spec.get("relation", "equal")
        return MetamorphicMonitor(spec.get("a", {}), spec.get("b", {}),
                                  "equal" if rel == "idempotent" else rel,
                                  spec.get("reduce", "sum"), spec.get("field"),
                                  spec.get("factor", 1.0), spec.get("tol", 0.0), indicators)
    event, where = _resolve_matcher(rule, indicators)
    return CountMonitor(event, where, _norm_op(rule.get("op", ">=")), int(_want(rule)))


class LiveMonitorSet:
    """A bank of monitors fed a *live* event stream incrementally — the resident/live-mode
    counterpart to the one-shot :func:`run_monitor`. Build it from compiled checks, call
    :meth:`feed` per event as it arrives, read :meth:`verdicts` for the anticipatory
    ⊤/⊥/? at any time, and :meth:`collapse` for the final per-check result dicts.

    The live path and the batch path share :func:`compile_check`, so feeding the same events
    one-by-one here yields the same verdicts/settle points as a batch ``run_monitor``."""

    def __init__(self, monitors: list[Monitor]):
        self.monitors = list(monitors)
        self._idx = 0

    @classmethod
    def from_rules(cls, rules: list[dict], *, indicators: dict | None = None,
                   ontology=None, allow: list | None = None) -> LiveMonitorSet:
        return cls([compile_check(r, indicators=indicators, ontology=ontology, allow=allow)
                    for r in rules])

    def feed(self, ev: dict, idx: int | None = None) -> None:
        """Consume one event (auto-incrementing the stream index if not given)."""
        i = self._idx if idx is None else idx
        for m in self.monitors:
            m.step(ev, i)
        self._idx = i + 1

    def verdicts(self) -> list[str]:
        """The current three-valued verdict (sat/viol/pend) of each monitor."""
        return [m.verdict for m in self.monitors]

    def collapse(self, reachable: bool = True, complete: bool = True) -> list[dict]:
        """The final per-check result dicts (reachable AND complete gates a clean pass)."""
        return [m.collapse(reachable and complete) for m in self.monitors]
