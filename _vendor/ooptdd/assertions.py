"""In-test assertion helpers (Tier-2 #9) — DeepEval ``assert_test`` ergonomics.

Use ooptdd directly inside an ordinary pytest test: write a gate spec (or a list of
expectations), evaluate it against a backend, and raise on RED — so a *trace
expectation* becomes a first-class pytest assertion, the way DeepEval's
``assert_test(case, metrics)`` turns an LLM metric into a unit test.

This is distinct from the session-level plugin, which never fails the build
("observation does not override the verdict"). These are assertions you opted into
explicitly: a real, reachable miss (RED) raises ``TraceAssertionError``; an
unreachable store (``inconclusive``) is skipped unless ``strict_infra=True`` —
keeping the rule that an infra outage is not a falsification.
"""
from __future__ import annotations

from .backends import Backend, get_backend
from .engine.gate import _label, evaluate, failed_checks


class TraceAssertionError(AssertionError):
    """Raised when a gate evaluates RED — a real miss against a reachable store."""


def _gating_failures(res: dict) -> list[str]:
    return [_label(c) for c in res["checks"]
            if not c["passed"] and not c["optional"] and not c["pending"]]


def assert_gate(spec: dict, *, backend: Backend | None = None, ontology=None,
                strict_infra: bool = False) -> dict:
    """Evaluate ``spec`` and raise ``TraceAssertionError`` on RED. Returns the result dict.

    ``backend`` defaults to the zero-infra memory backend. ``strict_infra`` makes an
    unreachable store fail too (default False → inconclusive is skipped).
    """
    backend = backend or get_backend("memory")
    res = evaluate(backend, spec, ontology=ontology)
    if not res["reachable"] or not res.get("complete", True):
        # Store unreachable OR a truncated/incomplete read: inconclusive evidence (?), not a
        # falsification (⊥). Skip unless the caller opted into ``strict_infra`` — an infra
        # outage or an undercounted read is never a RED, the same rule the verdict layer uses
        # (verify_policy / evaluate_events). Treating a truncated read as RED here would make
        # an infra hiccup a hard assertion failure.
        if strict_infra:
            raise TraceAssertionError(
                f"store unreachable or read incomplete for cid={res['cid']} (inconclusive)"
            )
        return res
    if not res["ok"]:
        raise TraceAssertionError(
            f"trace gate RED (cid={res['cid']}): {_gating_failures(res)}"
        )
    return res


def assert_gate_red(spec: dict, *, backend: Backend | None = None, ontology=None,
                    strict_infra: bool = False) -> dict:
    """The inverse of :func:`assert_gate`: raise unless the gate is genuinely RED — a reachable,
    complete miss. A GREEN gate raises ``TraceAssertionError`` (a passing gate does not satisfy
    "assert red"). An unreachable/incomplete store is INCONCLUSIVE — it cannot confirm RED, so it
    is skipped (``pytest.skip``) rather than counted as red, unless ``strict_infra``. Returns the
    result on a confirmed RED (useful for asserting the specific failing kinds)."""
    backend = backend or get_backend("memory")
    res = evaluate(backend, spec, ontology=ontology)
    if not res["reachable"] or not res.get("complete", True):
        if strict_infra:
            raise TraceAssertionError(
                f"store unreachable or read incomplete for cid={res['cid']} — cannot confirm RED"
            )
        import pytest  # only reached in a test context; keeps assertions import-light otherwise
        pytest.skip(f"inconclusive (store unreachable/incomplete) — cannot confirm RED "
                    f"for cid={res['cid']}")
    if res["ok"]:
        raise TraceAssertionError(f"expected trace gate RED but it was GREEN (cid={res['cid']})")
    return res


def explain(result: dict) -> str:
    """A one-line human summary of a gate result: GREEN, INCONCLUSIVE, or RED with the failing
    checks named by their stable ``kind`` — the readable form of :func:`failed_checks`."""
    if not result.get("reachable", True) or not result.get("complete", True):
        return f"INCONCLUSIVE (store unreachable/incomplete) cid={result.get('cid')}"
    if result.get("ok"):
        return f"GREEN cid={result.get('cid')}"
    parts = [f"{c.get('kind', '?')}:{_label(c)}" for c in failed_checks(result)]
    return f"RED cid={result.get('cid')}: " + ", ".join(parts)


def assert_present(cid: str, *matchers: dict, backend: Backend | None = None,
                   strict_infra: bool = False) -> dict:
    """Assert each matcher (``{"event":..., "where":...}``) matched ≥1 event, any order.

    Shorthand for a single ``present`` gate — the everyday "did these events happen?"
    check, DeepEval-style: ``assert_present(cid, {"event": "execute_tool"})``.
    """
    spec = {"cid": cid, "expect": [{"present": list(matchers)}]}
    return assert_gate(spec, backend=backend, strict_infra=strict_infra)
