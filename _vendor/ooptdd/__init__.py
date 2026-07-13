"""ooptdd — logs/traces as the test specification and ground truth.

ooptdd (also written LTDD, "Log-based / observability positive-TDD") inverts the
trust relationship of a normal test: instead of believing a function's return
value (or an agent's "done!"), it asserts that the *events the system actually
emitted* arrived in an external store.

    Red      write the expected event-trace spec (it fails — nothing emits it yet)
    Green    the implementation emits structured events; a verifier polls the
             store and *positively asserts they arrived*
    Refactor the same event contract still holds (golden-trace regression)

The word **positive** is load-bearing: `ship()` returning without raising is a
*claim*, not proof. A separate verifier reads the store back and asserts the
records exist. (Motivating failure: a silent 401 dropped ingest for 22 hours and
every "shipped OK" log lied.)

Public API:
    build_outcome_records   pure: pytest reports -> structured event records
    verify_trace            poll a backend for the pytest summary, return an LTL3 verdict
    verify_gate             poll until an arbitrary gate spec arrives for a cid (generic)
    poll_until_present      the generic, shape-agnostic arrival loop underneath both
    verify_policy           verdict + mode -> build decision
    session_finish          build -> ship -> verify -> policy (the orchestrator)
    evaluate, evaluate_events   run a gate spec (read+judge) / judge already-fetched events
    can_i_deploy            Pact-style multi-gate deploy decision
    check                   @decorator to register a custom gate check-predicate (seam)
    compile_check, LiveMonitorSet   kernel API: rule -> Monitor -> feed a live stream
    get_backend, BackendRegistry    resolve a Backend driver by name
    QuerySpec, TimeWindow, BackendCaps, Clock, SystemClock   the domain ports/value objects
    assert_gate, assert_present   DeepEval-style in-test trace assertions
    Backend, QueryResult, MemoryBackend
"""
from .assertions import (
    TraceAssertionError,
    assert_gate,
    assert_gate_red,
    assert_present,
    explain,
)
from .backends import (
    Backend,
    BackendCaps,
    BackendRegistry,
    Clock,
    MemoryBackend,
    QueryResult,
    QuerySpec,
    SystemClock,
    TimeWindow,
    get_backend,
    memory_reset,
)
from .domain import (
    semconv as _semconv,  # noqa: F401  # registers the "gen_ai" builtin preset (Ontology.register_preset)
)
from .domain.model import (
    Emitter,
    build_event,
    build_outcome_records,
    correlation_keys,
    sign_chain,
    verify_chain,
)
from .domain.ontology import EventType, Ontology, check_conformance
from .domain.ports import ExternalProbe, ProbeResult
from .engine.gate import (
    EVIDENCE_TIERS,
    can_i_deploy,
    check,
    compare_strength,
    evaluate,
    evaluate_events,
    evidence_tier,
    failed_checks,
    green_banner,
    lint_spec,
    load_gate,
    strength_fingerprint,
    unregister,
)
from .engine.monitor import LiveMonitorSet, compile_check
from .engine.verify import (
    poll_until_present,
    session_finish,
    verify_gate,
    verify_policy,
    verify_trace,
)
from .probes import CallableProbe, ProbeRegistry, get_probe

__all__ = [
    "build_outcome_records",
    "build_event",
    "Emitter",
    "correlation_keys",
    "verify_trace",
    "verify_gate",
    "poll_until_present",
    "verify_policy",
    "session_finish",
    "get_backend",
    "memory_reset",
    "BackendRegistry",
    "load_gate",
    "evaluate",
    "evaluate_events",
    "evidence_tier",
    "EVIDENCE_TIERS",
    "green_banner",
    "lint_spec",
    "strength_fingerprint",
    "compare_strength",
    "sign_chain",
    "verify_chain",
    "can_i_deploy",
    "check",
    "unregister",
    "failed_checks",
    "compile_check",
    "LiveMonitorSet",
    "assert_gate",
    "assert_gate_red",
    "assert_present",
    "explain",
    "TraceAssertionError",
    "Backend",
    "QueryResult",
    "MemoryBackend",
    "QuerySpec",
    "TimeWindow",
    "BackendCaps",
    "Clock",
    "SystemClock",
    "Ontology",
    "EventType",
    "check_conformance",
    "ExternalProbe",
    "ProbeResult",
    "CallableProbe",
    "ProbeRegistry",
    "get_probe",
]

__version__ = "0.4.0"
