"""Gate runner — evaluate a YAML trace spec against a backend.

A gate is the *Red* artifact: you write what you expect to observe before the
code emits it. It is plain data in your repo (the agent only proposes it; the
store is the judge), and it is intentionally count-based — existence and
cardinality, the assertions that are robust on eventually-consistent stores.

Spec format (``gates/*.yaml``)::

    cid_env: OOPTDD_CID        # or:  cid: a-literal-correlation-id
    service: myapp.tests       # optional, informational
    timeWindow: 1h             # optional rolling readback window (OpenSLO style:
                               #   30s/5m/2h/1d or bare seconds); default = backend's
    indicators:                # optional SLI layer — *how to select* (named, reusable)
      ng_cycles: {event: cycle, where: {verdict: NG}}
      done:      {event: cycle, where: {verdict: PASS}}
    expect:                    # the SLO layer — *what counts as green* (criteria)
      - event: test_session
        op: ">="              # symbolic (>= > == <= <) OR OpenSLO words (gte/gt/eq/lte/lt)
        count: 1
      - event: test_outcome
        op: gte                #   `target:` is an alias for `count:`
        target: 5
      - indicatorRef: ng_cycles # reuse a named indicator; criteria stay here
        op: eq
        target: 0
      - ratioMetric:           # good/total ratio (OpenSLO ratioMetric)
          good:  {indicatorRef: done}
          total: {event: cycle}
        op: gte
        target: 0.99
      - present:               # subset-present, ANY order (testfixtures order_matters=False);
          - {event: a}         #   each matcher must match >=1 event. The default "did these
          - {event: b, where: {station: A}}   #   happen?" check — order is NOT asserted.
      - must_order: [a, b, c]  # each must occur, first-occurrence times non-decreasing
      - absent:                # the negative wing — matching events must NOT occur (count 0).
          where: {level: ERROR}   #   the mirror of `present`. Offenders are surfaced so an
                               #   error log becomes a hard failure, not green-and-noisy.
      - event: optional_stream # optional: a threshold miss does NOT fail the gate,
        op: ">="               #   but it IS surfaced (and an unreachable store is still
        count: 1               #   INFRA, reported via `reachable`, never a clean pass)
        optional: true
    forbid_errors: true        # optional (spec-level): inject an implicit ERROR/CRITICAL
                               #   `absent` into the gate (default = env OOPTDD_FORBID_ERRORS;
                               #   set false here to opt a spec out). Levels via `error_levels:`.
    allow_errors:              # optional (spec-level) allowlist — these matched errors are
      - {event: zdf.drop}      #   exempt (known-benign), so they don't flip the gate.

Counting is done over the events the backend returns for ``cid`` — no
backend-specific query language, so the same gate runs on memory, OpenObserve, or
any future driver. ``where`` filters on arbitrary event fields (e.g. ``verdict``,
``level``) by partial-dict equality — only the listed keys must match, like
``pytest-structlog``'s ``log.has(evt, **ctx)``. ``must_order`` checks sequencing
using each event's ``_timestamp`` (store-receive time); ``present`` asserts a
subset occurred in *any* order (``testfixtures.check_present(order_matters=False)``).

The vocabulary (``op: gte``, ``target``, ``timeWindow``, ``indicators``/``indicatorRef``,
``ratioMetric``) is deliberately aligned with **OpenSLO** and **Keptn** SLO specs so
a gate reads like an SLO objective and the SLI ("how to query") is decoupled from the
SLO ("what is green") and reusable. Symbolic operators and ``count`` remain first-class —
the alignment is additive, the evaluation logic is unchanged.

Evaluation is **streaming**: each check compiles to an LTL₃/MTL monitor automaton
(:mod:`ooptdd.monitor`) that is fed the event prefix in store-timestamp order and reports
a three-valued verdict (``sat``/``viol``/``pend``) plus the index at which it settled. The
final collapsed pass/fail is identical to the historical count comparison; what the gate
gains is a real incremental monitor with anticipatory verdicts, surfaced per check as
``verdict``/``settled_at``.
"""
from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.model import verify_chain
from ..domain.ports import (
    Backend,
    Clock,
    QuerySpec,
    SystemClock,
    TimeWindow,
    backend_identity,
    fetch,
)
from .monitor import (  # the evaluation kernel
    _OPS,
    _matches,  # noqa: F401  re-exported for backward compat (ooptdd.mutation)
    _norm_op,
    _resolve_matcher,  # noqa: F401  re-exported for backward compat (ooptdd.mutation)
    compile_check,
    run_monitor,
    stream_key,
)

# ---- check-predicate registry (the extension seam) -------------------------- #
# Each gate check kind (present/absent/conforms/...) is a handler registered under its
# spec keyword, not a branch in a central if-elif. New predicates register via
# ``@check("<key>")`` WITHOUT editing ``evaluate()`` — the pluggy/hypothesis registration
# pattern (a string-keyed single-dispatch table), absorbed here. The registry is also a
# structural-assertion surface: every dispatched key must resolve to a registered handler.


@dataclass(frozen=True)
class CheckCtx:
    """Cross-cutting context a check handler may need — built once per :func:`evaluate`
    call, so handlers take ``(events, rule, ctx)`` instead of 4-6 positional args."""

    reachable: bool
    indicators: dict
    ontology: object | None = None
    allow_errors: list | None = None
    probe: object | None = None  # an ExternalProbe (independent oracle) for `external:` checks
    cid: str | None = None


CheckFn = Callable[[list, dict, CheckCtx], dict]
CHECK_REGISTRY: dict[str, CheckFn] = {}


def check(*keys: str) -> Callable[[CheckFn], CheckFn]:
    """Register a check handler under one or more spec keywords. Decoration-time only (a
    dict insert, no I/O). A duplicate key raises — guarding the silent-overwrite failure."""
    def deco(fn: CheckFn) -> CheckFn:
        for k in keys:
            if k in CHECK_REGISTRY:
                raise ValueError(f"duplicate check predicate {k!r}")
        for k in keys:
            CHECK_REGISTRY[k] = fn
        return fn
    return deco


def unregister(key: str) -> CheckFn | None:
    """Remove a check predicate (inverse of :func:`check`); returns the handler or None. Lets a
    test drop a custom key it registered, and makes a built-in overridable (unregister then
    re-register) — the duplicate-key guard in ``check`` otherwise forbids it."""
    return CHECK_REGISTRY.pop(key, None)


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def duration_s(v) -> int | None:
    """Parse an OpenSLO-style rolling window: ``30s`` / ``5m`` / ``2h`` / ``1d`` /
    bare seconds (int or numeric string). ``None`` -> ``None`` (use backend default)."""
    if v is None:
        return None
    if isinstance(v, bool):  # guard: bool is an int subclass
        raise ValueError(f"invalid timeWindow: {v!r}")
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower()
    if s and s[-1] in _UNITS and s[:-1].isdigit():
        return int(s[:-1]) * _UNITS[s[-1]]
    return int(s)  # bare numeric string -> seconds


def load_gate(path: str, *, cid: str | None = None) -> dict:
    import yaml  # PyYAML (declared dependency)

    with open(path) as fh:
        try:
            spec = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            # YAMLError is NOT a ValueError, so it would escape the CLI's clean-error handler as an
            # uncaught traceback (exit 1) — re-raise as ValueError so a malformed spec is exit 2.
            raise ValueError(f"malformed gate spec {path}: {exc}") from exc
    if cid is not None:
        spec["cid"] = cid  # explicit override of the file's cid/cid_env (no monkeypatch needed)
    return spec


def _label(chk: dict) -> str:
    """Human handle for a check (used to surface optional failures)."""
    if "label" in chk:  # a custom check/rule may name itself; honored for both result & rule dicts
        return str(chk["label"])
    if "external" in chk:
        return "external:" + str(chk.get("external"))
    if "metamorphic" in chk:
        return "metamorphic:" + str(chk.get("metamorphic"))
    if "invariant" in chk:
        inv = chk["invariant"]
        return "invariant:" + (inv if isinstance(inv, str) else "expr")
    if "conforms" in chk:
        return "conforms:" + str(chk["conforms"])
    if "heartbeat" in chk:
        return f"heartbeat:{chk['heartbeat']}@{chk.get('every_s')}s"
    if "must_order" in chk:
        return "must_order:" + ">".join(chk["must_order"])
    if "present" in chk:
        return "present:" + ",".join(chk["present"])
    if "absent" in chk:
        return "absent:" + ",".join(chk["absent"])
    if "ratio" in chk:
        return f"ratio:{chk['ratio']}{chk.get('op', '')}{chk.get('want', '')}"
    if chk.get("event"):
        return str(chk["event"])
    where = chk.get("where") or {}
    return "where:" + ",".join(f"{k}={v}" for k, v in where.items()) if where else "(any)"


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"} if v is not None else False


# ---- registered check handlers (thin adapters over the kernel's compile_check) ---- #
# Every built-in handler compiles its rule to the right Monitor via the kernel's single
# source of truth (compile_check) and drives it over the (already stream-ordered) events.
# The batch path here and the live path (monitor.LiveMonitorSet) therefore share one
# rule->automaton compiler and can never diverge. Custom @check predicates (user-registered)
# remain free to return their own dicts — they are a gate-layer seam, not kernel monitors.


def _run(rule: dict, events: list, ctx: CheckCtx) -> dict:
    monitor = compile_check(rule, indicators=ctx.indicators, ontology=ctx.ontology,
                            allow=ctx.allow_errors)
    return run_monitor(monitor, events, ctx.reachable)


@check("absent")
def _check_absent(events: list, rule: dict, ctx: CheckCtx) -> dict:
    return _run(rule, events, ctx)


@check("heartbeat")
def _check_heartbeat(events: list, rule: dict, ctx: CheckCtx) -> dict:
    return _run(rule, events, ctx)


@check("must_order")
def _check_must_order(events: list, rule: dict, ctx: CheckCtx) -> dict:
    return _run(rule, events, ctx)


@check("present")
def _check_present(events: list, rule: dict, ctx: CheckCtx) -> dict:
    return _run(rule, events, ctx)


@check("ratioMetric")
def _check_ratio(events: list, rule: dict, ctx: CheckCtx) -> dict:
    return _run(rule, events, ctx)


@check("conforms")
def _check_conforms(events: list, rule: dict, ctx: CheckCtx) -> dict:
    return _run(rule, events, ctx)


@check("invariant")
def _check_invariant(events: list, rule: dict, ctx: CheckCtx) -> dict:
    return _run(rule, events, ctx)


@check("metamorphic")
def _check_metamorphic(events: list, rule: dict, ctx: CheckCtx) -> dict:
    return _run(rule, events, ctx)  # within-run: pure, two matched subsets of the one stream


@check("external")
def _check_external(events: list, rule: dict, ctx: CheckCtx) -> dict:
    """The independent-oracle check: assert against an external fact (ctx.probe), NOT the system's
    own emit. A missing probe is a loud misconfiguration (never a silent green); an unreachable
    probe is inconclusive (surfaced via ``probe_reachable=False``, never a strict fail)."""
    spec = rule["external"]
    op = _norm_op(spec.get("op", "=="))
    base = {"external": spec.get("kind", "?"), "op": op, "want": spec.get("want"),
            "selector": spec.get("selector")}
    if ctx.probe is None:
        return {**base, "passed": False, "probe_reachable": None,
                "reason": "no_external_probe_configured"}
    res = ctx.probe.probe(spec.get("kind"), spec.get("selector"), ctx.cid)
    if not res.reachable or not getattr(res, "complete", True):
        return {**base, "passed": False, "probe_reachable": False, "value": None,
                "reason": "external_probe_unreachable"}
    value, want = res.value, spec.get("want")
    if want is None:
        passed = value is not None  # the external fact merely has to EXIST
    elif op == "==":
        try:
            passed = abs(value - want) <= float(spec.get("tol", 0.0))
        except TypeError:
            passed = value == want
    else:
        passed = _OPS[op](value, want)
    return {**base, "value": value, "probe_reachable": True,
            "separate_source": bool(getattr(res, "separate_source", False)),
            "derived_identity": getattr(res, "derived_identity", None), "passed": bool(passed)}


def _eval_count(events: list, rule: dict, ctx: CheckCtx) -> dict:
    """The default check (no predicate keyword): a :class:`CountMonitor` over the rule's
    event/where compared with op/target. The documented fallback when no registered
    predicate key is present."""
    return _run(rule, events, ctx)


# Ordered (spec_key -> canonical registry key) probes: preserve the historical if-elif
# priority and the keyword synonyms (forbid->absent, trajectory->must_order). Probed
# before the generic registry scan so built-in precedence is deterministic even for a
# (degenerate) multi-keyword rule.
_KEY_PROBES = (
    ("absent", "absent"), ("forbid", "absent"),
    ("heartbeat", "heartbeat"),
    ("must_order", "must_order"), ("trajectory", "must_order"),
    ("present", "present"),
    ("ratioMetric", "ratioMetric"),
    ("conforms", "conforms"),
    ("invariant", "invariant"),
    ("metamorphic", "metamorphic"),
    ("external", "external"),
)


def _detect_check_key(rule: dict) -> str | None:
    """The registry key for ``rule`` (``None`` -> the default count check). Built-in keys
    win in historical order; an externally-registered custom key is matched after."""
    for spec_key, canon in _KEY_PROBES:
        if spec_key in rule:
            return canon
    for key in CHECK_REGISTRY:
        if key in rule:
            return key
    return None


# ---- strength / scope signal (honesty, not an oracle) ----------------------- #
# A GREEN gate must not be misread as "the system is correct": it only proves the events the
# author NAMED arrived with the asserted shape. `_strength` classifies a check's discriminating
# power (from the rule alone), so a gate can self-report HOW HARD it asserted — an all
# `existence-only` gate proved tokens were emitted, pinned no field, ordered nothing, forbade
# nothing. Higher strength is still author-vs-author (the `where` value descends from the same
# mental model) — a harder self-check, NOT an external oracle (see METHODOLOGY "log-free zones").
_STRENGTH_BY_KEY = {
    "absent": "forbid", "must_order": "ordered", "ratioMetric": "ratio",
    "heartbeat": "liveness", "conforms": "conformance", "invariant": "invariant",
    "metamorphic": "metamorphic", "external": "external",
}

# Discriminating-power weight per strength class — basis of the scalar strength score that turns
# "the agent weakened the gate to win" into a measurable REGRESSION (see strength_fingerprint).
# `external` ranks highest: it is the only class whose input is NOT the system's own self-report.
_STRENGTH_RANK = {
    "existence-only": 1, "bounded": 2, "threshold": 2,
    "value-pinned": 3, "ordered": 3, "forbid": 3,
    "ratio": 4, "liveness": 4, "conformance": 4, "invariant": 5, "metamorphic": 5, "external": 6,
}


def _strength(rule: dict) -> str:
    """Discriminating-power class of a check (pure, total over every registry key + the default
    count). Low→high: existence-only < bounded < value-pinned/ordered/forbid/threshold <
    ratio/liveness/conformance."""
    d = rule.get("strength")  # a custom check may declare its own discriminating-power class
    if isinstance(d, str) and d:
        return d
    key = _detect_check_key(rule)
    if key in _STRENGTH_BY_KEY:
        return _STRENGTH_BY_KEY[key]
    if key == "present":
        ms = rule.get("present") or []
        return "value-pinned" if any(m.get("where") for m in ms) else "existence-only"
    # default count check (and any custom predicate without a richer shape)
    if rule.get("where"):
        return "value-pinned"
    if rule.get("threshold") is not None:
        return "threshold"
    tight = _norm_op(rule.get("op", ">=")) in ("==", "!=", "<=", "<")
    return "bounded" if tight else "existence-only"


def _rule_event_names(rule: dict) -> set[str]:
    """Event names a single gate rule asserts on, best-effort across every check shape — used to
    measure how much of the OBSERVED stream the gate actually names (the closed-world signal)."""
    names: set[str] = set()
    for key in ("present", "absent", "forbid"):
        v = rule.get(key)
        for m in (v if isinstance(v, list) else [v] if isinstance(v, dict) else []):
            if isinstance(m, dict):
                names.add(m.get("event"))
    for key in ("must_order", "trajectory"):
        for part in rule.get(key) or []:
            names.add(part if isinstance(part, str) else
                      part.get("event") if isinstance(part, dict) else None)
    for container, sides in (("ratioMetric", ("good", "total")), ("invariant", ("left", "right")),
                             ("metamorphic", ("a", "b"))):
        c = rule.get(container)
        if isinstance(c, dict):
            for side in sides:
                if isinstance(c.get(side), dict):
                    names.add(c[side].get("event"))
    names.add(rule.get("heartbeat"))
    if isinstance(rule.get("conforms"), str):
        names.add(rule["conforms"])
    names.add(rule.get("event"))
    for n in rule.get("events") or []:  # a custom check may declare the event names it asserts on
        names.add(n if isinstance(n, str) else None)
    return {n for n in names if isinstance(n, str) and n}


def _check_charged(chk: dict) -> bool:
    """Did a check actually SEE matching evidence (positive confirmation), vs pass on absence /
    emptiness? The charge-ratio over gating checks measures how much of a green is backed by
    observed events rather than by nothing happening — distinct from stream-coverage."""
    if "charged" in chk:  # a custom handler may report its own charge (present only if it set it;
        return bool(chk["charged"])  # the engine's own chk['charged'] is assigned AFTER this call)
    if "got" in chk:
        return chk["got"] > 0
    if "present" in chk:
        return len(chk.get("missing", [])) < len(chk.get("present", []))
    if "must_order" in chk:
        return any(v is not None for v in chk.get("firsts", {}).values())
    if "ratio" in chk:
        return chk.get("total", 0) > 0
    if "invariant" in chk:
        return chk.get("reason") != "invariant_no_evidence"
    if "metamorphic" in chk:
        return chk.get("reason") != "metamorphic_no_evidence"
    if "external" in chk:
        return chk.get("probe_reachable") is True and chk.get("value") is not None
    if "heartbeat" in chk:
        return chk.get("beats", 0) > 0
    if "conforms" in chk:
        # evidence = it validated a declared-type event OR saw a closed-world drift offender
        # (`unknown`). ontology_not_loaded has unknown==[] so it stays uncharged (it saw nothing).
        return chk.get("checked", 0) > 0 or bool(chk.get("unknown"))
    if "absent" in chk:
        return chk.get("violations", 0) > 0  # absent is "charged" only if it SAW an offender
    return False


def _resolve_cid(spec: dict) -> str:
    if spec.get("cid"):
        return str(spec["cid"])
    env = spec.get("cid_env", "OOPTDD_CID")
    cid = os.getenv(env)
    if not cid:
        raise ValueError(f"gate needs a cid: set `cid:` in the spec or export {env}")
    return cid


def evaluate(
    backend: Backend,
    spec: dict,
    *,
    lookback_s: int | None = None,
    future_buffer_s: int | None = None,
    ontology=None,
    clock: Clock | None = None,
    probe=None,
    cid: str | None = None,
) -> dict:
    """Run a gate spec once: read the backend, then judge the events.

    Returns ``{ok, reachable, complete, cid, checks:[...], optional_failed:[labels]}``.
    ``ok`` is true iff the store was reachable, the read was complete, and every *required*
    check passed; optional checks that miss are in ``optional_failed`` but never flip ``ok``.
    ``reachable=False`` (store unreachable / INFRA) and ``complete=False`` (truncated read)
    each keep ``ok`` false regardless — neither is a clean pass.

    Each check is evaluated by a streaming monitor (:mod:`ooptdd.monitor`) fed the event
    prefix in store-timestamp order; the per-check dict carries the three-valued ``verdict``
    and the ``settled_at`` stream index alongside the collapsed ``passed``.

    The readback window comes from ``lookback_s`` (arg) else the spec's ``timeWindow``
    (OpenSLO rolling window) else the backend default. ``clock`` (a :class:`Clock`) is
    injectable so the window is deterministic under test; it defaults to the system clock.
    This function owns the *read*; :func:`evaluate_events` owns the *judgement* and is the
    seam the arrival-poller (:func:`ooptdd.engine.verify.verify_gate`) reuses per poll.
    """
    cid = cid if cid is not None else _resolve_cid(spec)  # kwarg overrides spec cid/cid_env
    if ontology is None and spec.get("ontology"):
        from ..domain.ontology import Ontology  # file-first; offline, no KG dependency
        ontology = Ontology.from_file(spec["ontology"])
    if lookback_s is None:
        lookback_s = duration_s(spec.get("timeWindow", spec.get("time_window")))
    lookback_s = backend.default_lookback_s if lookback_s is None else lookback_s
    future_buffer_s = (
        backend.default_future_buffer_s if future_buffer_s is None else future_buffer_s
    )
    window = TimeWindow.around_now(clock or SystemClock(), lookback_s, future_buffer_s)
    res = fetch(backend, QuerySpec(cid=cid, window=window))
    # getattr default keeps duck-typed/older result objects (no `complete` field) working.
    return evaluate_events(
        spec, res.events, reachable=res.reachable,
        complete=getattr(res, "complete", True), ontology=ontology, cid=cid, probe=probe,
        # emit provenance: WHO/WHERE these events came from — stamped into oracle{} (so a green
        # is never a SILENT self-agreement) and used to demote a probe that re-reads this endpoint.
        emit_backend=type(backend).__name__, emit_identity=backend_identity(backend),
    )


def evaluate_events(
    spec: dict,
    events: list[dict],
    *,
    reachable: bool,
    complete: bool = True,
    ontology=None,
    cid: str | None = None,
    probe=None,
    emit_backend: str | None = None,
    emit_identity: str | None = None,
) -> dict:
    """Judge an already-fetched event set against a gate spec (no I/O).

    This is the post-read half of :func:`evaluate`, split out so the same monitor dispatch
    runs over a one-shot query *and* over each freshly-polled prefix in the arrival loop —
    a verified arrival and a gate evaluation can therefore never diverge. ``reachable`` /
    ``complete`` come from the :class:`~ooptdd.domain.ports.QueryResult`; a not-reachable or
    not-complete read is never a clean pass.
    """
    cid = cid if cid is not None else _resolve_cid(spec)
    indicators = spec.get("indicators") or {}
    # Consume events in store-timestamp order so the monitors' first-occurrence /
    # sequencing / liveness automata see the true arrival stream.
    events = sorted(events, key=stream_key)
    rules = list(spec.get("expect", []))
    # The negative wing: forbid ERROR/CRITICAL records for this cid. Default-ON via the
    # OOPTDD_FORBID_ERRORS env so consumers opt in without editing every spec; a spec can
    # override with `forbid_errors: false` (opt out) or exempt known-benign ones via
    # `allow_errors:`. Without it, a cycle whose good events all arrived but which also
    # logged an error reads as green-and-noisy — the field-error blind spot.
    fe = spec.get("forbid_errors")
    if fe is None:
        fe = _truthy(os.getenv("OOPTDD_FORBID_ERRORS"))
    if fe:
        levels = spec.get("error_levels") or ["ERROR", "CRITICAL"]
        rules.append({"absent": [{"where": {"level": lv}} for lv in levels],
                      "_auto": "forbid_errors"})
    # pin_service (opt-in spec key): assert every counted event carries service==<value>, so a
    # service-drifted or service-missing emitter reds the gate. Injected as a conservation
    # invariant — count(all) == count(where service==pinned) — reusing the existing monitor.
    ps = spec.get("pin_service")
    if ps:
        rules.append({"invariant": {"left": {"reduce": "count"},
                                    "right": {"where": {"service": ps}, "reduce": "count"},
                                    "op": "=="},
                      "label": f"pin_service={ps}", "_auto": "pin_service"})
    # Dispatch each rule through the check-predicate registry (the extension seam):
    # detect the rule's predicate keyword, look up its handler (else the default count
    # check). A check passes only over a *clean* read — reachable AND complete; a truncated
    # read (complete=False) is incomplete evidence and gates every check, exactly like an
    # unreachable store. The top-level result still reports the true reachable/complete.
    ctx = CheckCtx(reachable=reachable and complete, indicators=indicators,
                   ontology=ontology, allow_errors=spec.get("allow_errors"),
                   probe=probe, cid=cid)
    checks = []
    for rule in rules:
        key = _detect_check_key(rule)
        handler = CHECK_REGISTRY[key] if key is not None else _eval_count
        chk = handler(events, rule, ctx)
        # Enforce the check-result contract at the seam (not as a deep KeyError three sites later):
        # every handler must return a dict carrying a bool 'passed'. Names the handler + key so a
        # custom-check author sees exactly what to fix.
        if not isinstance(chk, dict) or "passed" not in chk:
            raise ValueError(
                f"check handler {getattr(handler, '__name__', repr(handler))!r} (key={key!r}) "
                f"returned a result without the required 'passed' key; a check result must be a "
                f"dict with a bool 'passed'. Got: {chk!r}"
            )
        chk["optional"] = bool(rule.get("optional", False))
        # Pact "pending pacts": a `pending` expectation is verified and surfaced but does
        # NOT gate the build — for an event whose emitter isn't wired yet. Once it passes,
        # drop the flag to promote it to a hard gate (see `pending_satisfied`).
        chk["pending"] = bool(rule.get("pending", False))
        chk["weight"] = float(rule.get("weight", 1.0))  # promptfoo per-assertion weight
        chk["strength"] = _strength(rule)  # discriminating-power class (signal, not an oracle)
        chk["kind"] = key or "count"  # stable identity for programmatic RED diagnosis
        if "label" in rule and "label" not in chk:
            chk["label"] = rule["label"]  # a rule-declared label follows into the result
        # A declared `separate_source=True` is DEMOTED to derived-self when the probe's own
        # derived_identity equals the emit endpoint: it provably re-read the system's own store, so
        # the independence claim is false (relocation, not corroboration). Asymmetric on purpose — a
        # derived identity can FALSIFY a declared True but never PROMOTE a missing one, so an honest
        # source whose identity we cannot derive (derived_identity=None, or no emit_identity to
        # compare) keeps its declared bool. This makes `separate_source` checkable, not just trust.
        _di = chk.get("derived_identity")
        _same_endpoint = (
            chk["strength"] == "external" and _di is not None and emit_identity is not None
            and str(_di).rstrip("/") == str(emit_identity).rstrip("/")
        )
        if _same_endpoint and chk.get("separate_source"):
            chk["demoted_same_endpoint"] = True  # self-explaining: the probe re-read the emit store
        _effective_separate = bool(chk.get("separate_source")) and not _same_endpoint
        # grounding: where the truth comes from — only a (non-demoted) separate-source `external`
        # check is CORROBORATED by an independent oracle; everything else (incl. a probe re-reading
        # the system's own store) is DERIVED-SELF. Orthogonal to strength.
        chk["grounding"] = ("corroborated" if chk["strength"] == "external"
                            and _effective_separate else "derived-self")
        chk["charged"] = _check_charged(chk)  # did it see matching evidence (vs pass on absence)?
        checks.append(chk)
    # A check gates only if it is neither optional (#10) nor pending (Pact). Optional/pending
    # misses are surfaced separately so a silently-degraded stream never reads as clean.
    # `not c.get("tautological")` excludes a failure-incapable check (e.g. `count >= 0`): it is
    # not optional/pending, but it can never fail, so counting it as gating would let a gate whose
    # ONLY check is `count >= 0` read GREEN while asserting nothing. `scope.total` still counts it.
    gating = [c for c in checks
              if not c["optional"] and not c["pending"] and not c.get("tautological")]
    # A gate must assert something that can FAIL to be a clean pass. The old `bool(checks)` guard
    # only caught ZERO checks; a gate whose every check is optional/pending (gating==0) ALSO
    # asserts nothing that can fail and must equally not be GREEN — this is the agent-loop's free
    # weakening move (mark-optional / mark-pending to turn a gate green). `vacuous` is the reason.
    asserts_anything = bool(gating)
    vacuous = bool(checks) and not asserts_anything
    threshold = spec.get("threshold")
    if threshold is None:
        # all-or-nothing (default, unchanged): every gating check must pass.
        required_ok = all(c["passed"] for c in gating)
        score = None
    else:
        # promptfoo test-level threshold: pass iff the *weighted* pass-ratio meets it
        # (a quorum of expected events, not strict unanimity).
        wtot = sum(c["weight"] for c in gating)
        score = (sum(c["weight"] for c in gating if c["passed"]) / wtot) if wtot else 1.0
        required_ok = score >= float(threshold)
    # A store we never reached (reachable=False) is INFRA and a truncated read
    # (complete=False) is incomplete evidence — neither is ever a clean pass (CLI exit 2). And a
    # gate that asserts nothing GATING (empty expect, or every check optional/pending) is never a
    # clean pass either (`asserts_anything`, above). `scope` reports what — and how hard — this
    # verdict actually asserted, so GREEN cannot be misread as "the system is correct": it is a
    # closed-world claim over the events the author NAMED, not over un-named behavior.
    # Stream charge-coverage: of the event TYPES that actually arrived for this cid, how many
    # does the gate even name? `unasserted_observed` are events the system emitted that NO check
    # observes — a measured slice of the closed-world gap (the rest, un-emitted paths, stays
    # invisible). A green gate that names 1 of 9 arrived types is technically green and almost
    # blind; this puts a number on it.
    observed = {e.get("event") for e in events if e.get("event")}
    asserted = set().union(*(_rule_event_names(r) for r in rules)) if rules else set()
    named = observed & asserted
    # An `external:` check whose probe was unreachable surfaces probe_reachable=False so the verdict
    # layer maps it to inconclusive (?), never a strict fail — the same honesty as an unreachable
    # store. A missing probe (no_external_probe_configured) is a loud RED, not a silent green.
    probe_reachable = not any(c.get("probe_reachable") is False for c in checks)
    # oracle provenance: how many GATING checks are CORROBORATED by an independent source (only
    # `external:`) vs DERIVED-SELF (the system's own emit). single_authority=True is the meta-
    # blind-spot made visible: this green is the system agreeing with itself. charge: how many
    # gating checks actually SAW matching evidence (vs passed on absence) — distinct from coverage.
    # Corroboration is an ACHIEVEMENT, not a check kind: a separate-source `external:` the probe
    # could not reach OR that REFUTED the system (passed=False) corroborates nothing — counting it
    # would issue the "independently corroborated" receipt the oracle denied, and would let a
    # refuting oracle satisfy require_corroboration. So the corroboration must have actually passed.
    corroborated = sum(
        1 for c in gating if c.get("grounding") == "corroborated" and c.get("passed")
    )
    charged = sum(1 for c in gating if c.get("charged"))
    # require_corroboration (spec key or env OOPTDD_REQUIRE_CORROBORATION, default OFF): promote the
    # single_authority SIGNAL to a GATE — a gate whose every check is the system's own self-report
    # (zero separate-source corroboration) is not a clean pass. A fixable misconfiguration (RED),
    # not inconclusive: add a separate-source `external:` or accept self-consistency by leaving OFF.
    rc = spec.get("require_corroboration")
    if rc is None:
        rc = _truthy(os.getenv("OOPTDD_REQUIRE_CORROBORATION"))
    rc = bool(rc)
    uncorroborated = rc and asserts_anything and corroborated == 0
    # require_signature (spec key or env OOPTDD_REQUIRE_SIGNATURE, default OFF): promote emit
    # PROVENANCE to a GATE, mirroring require_corroboration above. When ON, the events for this cid
    # must form an intact tamper-evident hash chain (domain.model.sign_chain) under the key in
    # OOPTDD_SIGNING_KEY — an unsigned injected event breaks the prev-link and a post-sign edit
    # breaks the MAC, so a forged GREEN (a positive check satisfied by an off-chain event) is no
    # longer a clean pass. This closes the gap that require_signature was ONLY enforced on the
    # pytest-summary path (engine.verify), never on the gate path every consumer counts domain
    # events through. Strict like that path: required-but-unverifiable (no key) or an unsigned /
    # broken stream is `unauthenticated`, never a silent green. Backend-stamped `_*` fields (e.g.
    # `_timestamp`, added on read after signing) are excluded from the canonical form. Keyless/OFF
    # is unchanged — opt-in. `authenticated` is True/False when enforced, None when not.
    rs = spec.get("require_signature")
    if rs is None:
        rs = _truthy(os.getenv("OOPTDD_REQUIRE_SIGNATURE"))
    rs = bool(rs)
    authenticated = None
    if rs and asserts_anything:
        sig_key = os.getenv("OOPTDD_SIGNING_KEY")
        if sig_key:
            chain = [{k: v for k, v in e.items() if not k.startswith("_")} for e in events]
            authenticated = bool(verify_chain(chain, sig_key)["ok"])
        else:
            authenticated = False  # required but unverifiable (no key) — never a clean pass
    unauthenticated = rs and asserts_anything and authenticated is not True
    result = {
        "ok": reachable and complete and asserts_anything and required_ok
        and not uncorroborated and not unauthenticated,
        "reachable": reachable,
        "complete": complete,
        "probe_reachable": probe_reachable,
        "vacuous": vacuous,
        "uncorroborated": uncorroborated,
        "unauthenticated": unauthenticated,
        "authenticated": authenticated,
        "cid": cid,
        "checks": checks,
        "oracle": {
            "gating": len(gating),
            "corroborated": corroborated,
            "derived_self": len(gating) - corroborated,
            "single_authority": bool(gating) and corroborated == 0,
            "enforced": rc,
            # emit provenance: WHERE this verdict's events came from, so a single_authority green is
            # never SILENT — a reviewer sees the self-agreement without reading the spec.
            # emit_backend is the driver class; emit_identity the framework-derived endpoint.
            # relocated counts gating `external:` checks whose separate_source claim was demoted.
            "emit_backend": emit_backend,
            "emit_identity": emit_identity,
            "relocated": sum(1 for c in gating if c.get("demoted_same_endpoint")),
            "signature_enforced": rs,
            # verdict provenance: was the negative wing (forbid ERROR/CRITICAL) enforced for this
            # run? Stamped like `enforced`/`signature_enforced` so a judge reads the posture off the
            # receipt instead of re-deriving it from the (env-dependent) spec.
            "forbid_errors": bool(fe),
        },
        "scope": {
            "gating": len(gating),
            "optional": sum(1 for c in checks if c["optional"]),
            "pending": sum(1 for c in checks if c["pending"]),
            "total": len(checks),
            "asserts_anything": asserts_anything,
            "by_strength": dict(Counter(c["strength"] for c in gating)),
            "observed_event_types": len(observed),
            "named_event_types": len(named),
            "unasserted_observed": sorted(observed - asserted)[:10],
            "stream_coverage": (len(named) / len(observed)) if observed else None,
            "charged": charged,
            "charge_ratio": (charged / len(gating)) if gating else None,
            "uncharged": [_label(c) for c in gating if not c.get("charged")][:10],
        },
        "optional_failed": [_label(c) for c in checks if c["optional"] and not c["passed"]],
        "pending_failed": [_label(c) for c in checks if c["pending"] and not c["passed"]],
        "pending_satisfied": [_label(c) for c in checks if c["pending"] and c["passed"]],
    }
    if score is not None:
        result["score"] = score
        result["threshold"] = float(threshold)
    return result


def failed_checks(result: dict) -> list[dict]:
    """The GATING checks that failed — the RED contributors, for programmatic diagnosis. Excludes
    optional/pending checks (they never gate ``ok``). Each carries a stable ``kind`` so a consumer
    keys off ``c["kind"]`` instead of string-matching the raw check shape."""
    return [c for c in result.get("checks", [])
            if not c.get("passed") and not c.get("optional") and not c.get("pending")]


def green_banner(result: dict) -> str:
    """One honest line for a GREEN gate: WHAT (scope) and HOW HARD (strength) it actually
    asserted, so green is not read as "the system is correct". Pure — shared by the CLI."""
    sc = result.get("scope", {})
    bys = sc.get("by_strength") or {}
    profile = " ".join(f"{k}={v}" for k, v in sorted(bys.items())) or "none"
    line = (
        f"GREEN closed-world over {sc.get('total', 0)} named expectation(s): "
        f"{sc.get('gating', 0)} gating, {sc.get('optional', 0)} optional, "
        f"{sc.get('pending', 0)} pending [by-strength: {profile}]. Certifies the named events "
        "ARRIVED with the asserted shape; does NOT certify the system is correct (un-named "
        f"behavior is unobserved). (cid={result.get('cid')})"
    )
    if sc.get("gating") and set(bys) <= {"existence-only"}:
        line += (" WARNING: every gating check is existence-only — proves tokens were emitted, "
                 "not that they had any effect.")
    if sc.get("stream_coverage") is not None:
        line += (f" Stream-coverage: {sc.get('named_event_types')}/"
                 f"{sc.get('observed_event_types')} arrived event-type(s) named")
        un = sc.get("unasserted_observed") or []
        line += f" ({len(un)} arrived UNOBSERVED: {','.join(un[:5])})." if un else "."
    orc = result.get("oracle") or {}
    if orc.get("gating"):
        line += (" Oracle: single authority — 0 checks corroborated by an independent source "
                 "(add an `external:` check to break self-consistency)."
                 if orc.get("single_authority")
                 else f" Oracle: {orc.get('corroborated')}/{orc.get('gating')} independently"
                      " corroborated.")
    if sc.get("charge_ratio") is not None:
        line += f" Charge: {sc.get('charged')}/{sc.get('gating')} gating check(s) saw evidence."
    return line


def lint_spec(spec: dict) -> list[dict]:
    """Static, offline strength audit of a gate spec — the "pseudo-tested gate" detector, run
    BEFORE any events, so a vacuously-satisfiable gate is caught at author time, not after a green
    run. Pure. Returns findings ``[{code, severity, label, message}]`` (``high`` = vacuous/blocking,
    ``medium`` = weak):

    - **VAC0** no expectations at all (`expect:` empty).
    - **VAC1** zero *gating* checks — every check optional/pending; the gate can never fail.
    - **VAC2** `threshold < 1.0` with no `justification:` — a quorum that licenses silent drops.
    - **VAC3** a gating `existence-only` check — proves a token arrived, pins no field/order/forbid.
    """
    rules = list(spec.get("expect", []))
    if not rules:
        return [{"code": "VAC0", "severity": "high", "label": "(spec)",
                 "message": "empty `expect:` — gate declares no expectations, asserts nothing."}]
    out: list[dict] = []
    gating = [r for r in rules if not r.get("optional") and not r.get("pending")]
    if not gating:
        out.append({"code": "VAC1", "severity": "high", "label": "(spec)",
                    "message": "no gating checks — every check is optional/pending; the gate can "
                               "never fail (vacuous). Mark at least one check gating."})
    t = spec.get("threshold")
    if t is not None and float(t) < 1.0 and not spec.get("justification"):
        out.append({"code": "VAC2", "severity": "high", "label": "(spec)",
                    "message": f"threshold {t} < 1.0 silently licenses dropping up to "
                               f"{(1 - float(t)) * 100:.0f}% of expectations every run; add a "
                               "`justification:` field if this quorum is intentional."})
    for i, r in enumerate(gating):
        _cnt = r.get("count", r.get("want", 1))
        if r.get("op") == ">=" and isinstance(_cnt, int) and _cnt <= 0:
            out.append({"code": "VAC4", "severity": "high", "label": _label(r),
                        "message": f"check #{i} ({_label(r)}) is `count >= {_cnt}` — counts are "
                                   "non-negative, so it is always satisfied and can never fail "
                                   "(tautology). Use `>= 1`, a `where`, or a real threshold."})
            continue
        if _strength(r) == "existence-only":
            out.append({"code": "VAC3", "severity": "medium", "label": _label(r),
                        "message": f"check #{i} ({_label(r)}) is existence-only — proves a token "
                                   "arrived, pins no field/order/forbid. Add a `where`, "
                                   "`must_order`, `absent`, or `invariant` to discriminate."})
    return out


def strength_fingerprint(spec: dict) -> dict:
    """A scalar + profile summary of a gate's discriminating power, computed from the spec alone
    (pure). It is the basis for catching a *weakening* — dropping a `where`, marking a check
    optional/pending, lowering a `threshold` — as a strength REGRESSION the way CI catches a
    coverage drop, which directly counters the agent-loop's incentive to win by weakening the gate.
    A quorum `threshold < 1` scales the score down (it licenses dropping expectations)."""
    rules = list(spec.get("expect", []))
    gating = [r for r in rules if not r.get("optional") and not r.get("pending")]
    strengths = [_strength(r) for r in gating]
    threshold = float(spec.get("threshold", 1.0))
    raw = sum(_STRENGTH_RANK.get(s, 1) for s in strengths)
    return {
        "gating": len(gating),
        "by_strength": dict(Counter(strengths)),
        "min_threshold": threshold,
        "score": round(raw * threshold, 4),
        # Enforcement posture (spec-declared, pure): the negative/provenance wings that DON'T
        # show up in `expect` strength. Disabling any of these — or WIDENING the allow_errors
        # allowlist — weakens the gate without moving the strength score, so compare_strength
        # must diff them; this closes the hole where an agent flips `require_signature: true`
        # to false (or drops the key) for an unchanged fingerprint.
        "enforcement": {
            "require_signature": bool(spec.get("require_signature")),
            "require_corroboration": bool(spec.get("require_corroboration")),
            "forbid_errors": bool(spec.get("forbid_errors")),
            "allow_errors": len(spec.get("allow_errors") or []),
        },
    }


def compare_strength(baseline: dict, current: dict) -> dict:
    """Did ``current`` get WEAKER than ``baseline``? Returns ``{weakened, regressions[], ...}`` —
    a non-empty ``regressions`` list (fewer gating checks, a lower score/threshold, or a stronger
    check class that disappeared) is a strength regression to fail in CI."""
    regs: list[str] = []
    if current["gating"] < baseline["gating"]:
        regs.append(f"gating checks dropped {baseline['gating']} -> {current['gating']}")
    if current["score"] < baseline["score"]:
        regs.append(f"strength score dropped {baseline['score']} -> {current['score']}")
    if current["min_threshold"] < baseline["min_threshold"]:
        regs.append(f"threshold lowered {baseline['min_threshold']} -> {current['min_threshold']}")
    bb, cb = baseline.get("by_strength", {}), current.get("by_strength", {})
    for cls in ("invariant", "ratio", "conformance", "liveness",
                "ordered", "forbid", "value-pinned"):
        if cb.get(cls, 0) < bb.get(cls, 0):
            regs.append(f"{cls} checks dropped {bb.get(cls, 0)} -> {cb.get(cls, 0)}")
    # Enforcement-axis downgrade: disabling a required wing (signature / corroboration /
    # forbid_errors) or WIDENING the allow_errors allowlist weakens the gate without touching
    # the strength score. Guarded on both sides being present so a pre-enforcement baseline
    # (an old fingerprint JSON without this key) never false-flags.
    be, ce = baseline.get("enforcement"), current.get("enforcement")
    if isinstance(be, dict) and isinstance(ce, dict):
        for axis in ("require_signature", "require_corroboration", "forbid_errors"):
            if be.get(axis) and not ce.get(axis):
                regs.append(f"{axis} enforcement dropped {be.get(axis)} -> {ce.get(axis)}")
        if ce.get("allow_errors", 0) > be.get("allow_errors", 0):
            regs.append(f"allow_errors widened {be.get('allow_errors', 0)} "
                        f"-> {ce.get('allow_errors', 0)}")
    return {"weakened": bool(regs), "regressions": regs,
            "baseline_score": baseline["score"], "current_score": current["score"]}


#: The assertion-strength ladder (LakatoTree element ``elem-ooptdd-assert-strength-ladder``),
#: low→high. Unlike per-check ``_strength`` (one rule's discriminating power), this grades a whole
#: VERDICT by the strongest *kind of evidence* it actually mustered.
EVIDENCE_TIERS = ("local_pass", "emitted", "arrived", "queryable_causal", "external_verdict")


def evidence_tier(result: dict) -> str:
    """Where a verdict sits on the assertion-strength ladder — the formal answer to "what ladder
    prevents fake-green": you can SEE which rung a green reached, computed from its own honesty
    fields (``scope`` charge, per-check ``strength``, ``oracle`` corroboration).

    - ``local_pass``       nothing asserted (vacuous) or the store was unreachable — proves only
                           "the test ran". The fake-green floor.
    - ``emitted``          gating checks exist but none positively witnessed evidence
                           (``charge_ratio == 0``): every one passed on absence/emptiness. Named,
                           not confirmed arrived.
    - ``arrived``          ≥1 gating check positively saw matching evidence (``charge_ratio > 0``):
                           the named events actually landed in the store.
    - ``queryable_causal`` a cross-event consistency relation holds (a passing ``invariant`` /
                           ``metamorphic`` check) — value consistency between events, not counts.
    - ``external_verdict`` an independent oracle corroborated (a separate-source ``external:``
                           check passed): the only rung whose input is NOT the system's own emit.

    Returns the HIGHEST rung the evidence reaches. Orthogonal to ``ok``/RED — it grades the
    evidence on offer, so a green that only reaches ``emitted`` is loudly weak.
    """
    scope = result.get("scope") or {}
    oracle = result.get("oracle") or {}
    if not scope.get("asserts_anything") or not result.get("reachable"):
        return "local_pass"
    if (oracle.get("corroborated") or 0) > 0:
        return "external_verdict"
    passing = {c.get("strength") for c in result.get("checks", []) if c.get("passed")}
    if passing & {"invariant", "metamorphic"}:
        return "queryable_causal"
    if (scope.get("charge_ratio") or 0) > 0:
        return "arrived"
    return "emitted"


def can_i_deploy(results: list[dict]) -> dict:
    """Pact ``can-i-deploy`` for ooptdd: may we ship, given a set of gate results?

    Yes iff every gate was reachable, complete, and ``ok``. ``pending`` checks never block
    (that is their purpose). A gate that was reachable-but-RED is a hard blocker; one that
    was unreachable OR read incompletely (truncated) is inconclusive — an INFRA hold, not a
    clean pass. Returns ``{deployable, blockers:[cid], inconclusive:[cid], pending:{cid:[..]}}``.
    """
    def _incomplete(r: dict) -> bool:
        return not r["reachable"] or not r.get("complete", True)

    blockers = [r["cid"] for r in results if r["reachable"] and r.get("complete", True)
                and not r["ok"]]
    inconclusive = [r["cid"] for r in results if _incomplete(r)]
    pending = {r["cid"]: r["pending_failed"] for r in results if r.get("pending_failed")}
    return {
        "deployable": not blockers and not inconclusive,
        "blockers": blockers,
        "inconclusive": inconclusive,
        "pending": pending,
    }
