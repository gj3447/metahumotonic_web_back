"""``ooptdd`` command line — stateless, single-shot wrappers over the library.

    ooptdd verify <cid> [--gate spec.yaml] [--backend memory] [--expect-total N]
    ooptdd gate <spec.yaml> [--backend memory]
    ooptdd can-i-deploy <spec.yaml> [<spec.yaml> ...] [--backend memory]
    ooptdd mutate <spec.yaml> --events events.json [--min-score X]
    ooptdd ontology check <onto.yaml> --events events.json [--event-type T] [--closed-world]
    ooptdd ontology compat <old.yaml> <new.yaml> [--mode backward|forward|full]
    ooptdd backends list
    ooptdd backends doctor [--backend B]
    ooptdd monitor <spec.yaml> [--backend B]
    ooptdd verify-chain --records records.json --key-env OOPTDD_SIGNING_KEY [--evolve]
    ooptdd schema [gate|ontology]
    ooptdd version

Governing principle: the core CLI surfaces exactly the *stateless, single-shot,
library-pure* operations — one invocation, no orchestration loop, no source-tree mutation,
no KG/MCP/agent dependency. Anything that iterates a feedback loop, drives an agent, or
needs the KG/Longinus binding lives in ``ooptdd-loop``, not here. (That is *why* this CLI was
thin: the primary delivery vehicle is the pytest plugin; the CLI is a manual re-check shim.)

Settings come from ``[tool.ooptdd]`` in the working-directory ``pyproject.toml``, overridden
by environment variables and the flags below. Exit codes mirror the LTL3 model:
0 = GREEN (ok), 1 = RED (reachable, complete, failed), 2 = INCONCLUSIVE/INFRA (unreachable
or truncated read). Human summaries go to stderr; ``--json`` machine output goes to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .backends import default_registry, get_backend
from .config import from_mapping, load_pyproject
from .domain.model import ENVELOPE_SCHEMA, signature_status, verify_chain
from .domain.ontology import Ontology, check_conformance, ontology_compat
from .domain.ports import backend_caps
from .engine.gate import (
    can_i_deploy,
    compare_strength,
    evaluate,
    green_banner,
    lint_spec,
    load_gate,
    strength_fingerprint,
)
from .engine.verify import verify_gate, verify_trace
from .mutation import mutation_report


def _settings(args):
    s = from_mapping(load_pyproject())
    if getattr(args, "backend", None):
        s.backend = args.backend
    return s


def _backend(args):
    s = _settings(args)
    return get_backend(s.backend, service=s.service, **s.backend_options)


def _exit(ok: bool, reachable: bool, complete: bool = True) -> int:
    """The shared LTL3-aligned exit ladder: 0 GREEN, 2 INFRA (unreachable or truncated read),
    1 RED. A not-clean read can never be a GREEN exit."""
    if ok:
        return 0
    if not reachable or not complete:
        return 2
    return 1


def _emit(payload: dict, args, human: str, level: str = "info") -> None:
    """JSON to stdout under ``--json``; otherwise a one-line human summary to stderr."""
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(human, file=sys.stderr)


def _load_json_file(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_probe(spec: dict):
    """A spec may name an external-oracle probe (``probe: {name: file, root: ...}``); resolve it
    via the probe registry so an ``external:`` check is usable from the CLI. None if absent."""
    p = spec.get("probe")
    if isinstance(p, dict) and p.get("name"):
        from .probes import get_probe
        return get_probe(p["name"], **{k: v for k, v in p.items() if k != "name"})
    return None


# ── verify (pytest summary, or an arbitrary --gate spec) ───────────────────────
def _cmd_verify(args) -> int:
    backend = _backend(args)
    if args.gate:
        gate = load_gate(args.gate)
        res = verify_gate(backend, args.cid, gate, retries=args.retries,
                          delay=args.delay, probe=_resolve_probe(gate))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        v = res["verdict"]
        msg = {"present": "GREEN — arrival confirmed", "absent": "RED — not all expected "
               "events arrived", "inconclusive": "INCONCLUSIVE — store unreachable"}[v]
        print(f"{msg}: {res.get('reasons')}", file=sys.stderr)
        return _exit(res["ok"], v != "inconclusive")
    res = verify_trace(
        backend, args.cid, expect_total=args.expect_total, retries=args.retries,
        delay=args.delay,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    verdict = res["verdict"]
    if res["ok"]:
        print("GREEN - arrival confirmed", file=sys.stderr)
        return 0
    if verdict == "inconclusive":
        print(f"INCONCLUSIVE - store unreachable: {res['reasons']}", file=sys.stderr)
        return 2
    print(f"RED - positive assertion failed: {res['reasons']}", file=sys.stderr)
    return 1


def _cmd_gate(args) -> int:
    backend = _backend(args)
    spec = load_gate(args.spec)
    res = evaluate(backend, spec, probe=_resolve_probe(spec))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("optional_failed"):
        print(f"WARN - optional checks failed (not gating): {res['optional_failed']}",
              file=sys.stderr)
    if res["ok"]:
        print(green_banner(res), file=sys.stderr)
        return 0
    if not res["reachable"]:
        print("INCONCLUSIVE - store unreachable", file=sys.stderr)
        return 2
    if not res.get("complete", True):
        print("INCONCLUSIVE - readback truncated (incomplete evidence)", file=sys.stderr)
        return 2
    if not res.get("probe_reachable", True):
        print("INCONCLUSIVE - external probe unreachable", file=sys.stderr)
        return 2
    if res.get("vacuous"):
        print("RED - vacuous gate: every check is optional/pending, nothing can fail "
              "(mark at least one check gating)", file=sys.stderr)
        return 1
    if res.get("uncorroborated"):
        print("RED - uncorroborated: every gating check is the system's own self-report "
              "(no separate-source external: corroboration); require_corroboration on",
              file=sys.stderr)
        return 1
    print("RED - gate failed", file=sys.stderr)
    return 1


def _cmd_lint(args) -> int:
    findings = lint_spec(load_gate(args.spec))
    if getattr(args, "json", False):
        print(json.dumps({"vacuity": findings}, ensure_ascii=False, indent=2))
    for f in findings:
        print(f"  [{f['severity']}] {f['code']} {f['label']}: {f['message']}", file=sys.stderr)
    high = [f for f in findings if f["severity"] == "high"]
    if high:
        print(f"VACUOUS - {len(high)} blocking finding(s); the gate is weak by construction",
              file=sys.stderr)
        return 1
    print("OK - no vacuity findings" if not findings
          else f"WARN — {len(findings)} strength finding(s)", file=sys.stderr)
    return 0


def _cmd_strength(args) -> int:
    fp = strength_fingerprint(load_gate(args.spec))
    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            json.dump(fp, fh, indent=2)
    if args.baseline:
        cmp = compare_strength(_load_json_file(args.baseline), fp)
        _emit({"fingerprint": fp, **cmp}, args,
              ("WEAKENED — " + "; ".join(cmp["regressions"])) if cmp["weakened"]
              else f"OK — strength held (score {fp['score']} >= baseline {cmp['baseline_score']})")
        return 1 if cmp["weakened"] else 0
    _emit(fp, args, f"strength score={fp['score']} gating={fp['gating']} "
          f"by_strength={fp['by_strength']} min_threshold={fp['min_threshold']}")
    return 0


def _cmd_can_i_deploy(args) -> int:
    backend = _backend(args)
    results = [evaluate(backend, load_gate(spec)) for spec in args.specs]
    decision = can_i_deploy(results)
    _emit(decision, args,
          f"{'DEPLOYABLE' if decision['deployable'] else 'HOLD'} — "
          f"blockers={decision['blockers']} inconclusive={decision['inconclusive']}")
    if decision["deployable"]:
        return 0
    return 1 if decision["blockers"] else 2  # a hard RED blocks; only INFRA holds -> 2


def _cmd_mutate(args) -> int:
    spec = load_gate(args.spec)
    events = _load_json_file(args.events)
    report = mutation_report(events, spec)
    _emit(report, args,
          f"mutation score={report['score']} survivors={report['survivors']} "
          f"(baseline_green={report['baseline_green']})")
    if not report["baseline_green"]:
        return 2  # couldn't even establish a baseline — the score is meaningless
    if args.min_score is not None and report["score"] < args.min_score:
        return 1  # gate too weak: it let mutants through
    return 0


def _cmd_ontology(args) -> int:
    if args.onto_cmd == "check":
        onto = Ontology.from_file(args.ontology)
        res = check_conformance(_load_json_file(args.events), onto,
                                event_type=args.event_type, closed_world=args.closed_world)
        _emit(res, args, f"{'PASS' if res['passed'] else 'FAIL'} — checked={res['checked']} "
              f"violations={len(res['violations'])} unknown={res['unknown']}")
        return 0 if res["passed"] else 1
    # compat
    old, new = Ontology.from_file(args.old), Ontology.from_file(args.new)
    res = ontology_compat(old, new, mode=args.mode)
    _emit(res, args, f"{'COMPATIBLE' if res['compatible'] else 'INCOMPATIBLE'} "
          f"({res['mode']}) — {res['violations']}")
    return 0 if res["compatible"] else 1


def _cmd_backends(args) -> int:
    if args.be_cmd == "list":
        names = default_registry.names()
        _emit({"backends": names}, args, "backends: " + ", ".join(names))
        return 0
    # doctor: construct + probe reachability of the configured backend
    try:
        backend = _backend(args)
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)}, args, f"ERROR — {exc}")
        return 1
    caps = backend_caps(backend)
    info = {"backend": type(backend).__name__, "queryable": caps.queryable,
            "write_only": caps.write_only,
            "default_lookback_s": getattr(backend, "default_lookback_s", None),
            "default_future_buffer_s": getattr(backend, "default_future_buffer_s", None)}
    if not caps.queryable:
        info["reachable"] = None  # write-only: nothing to probe
        _emit(info, args, f"WRITE-ONLY — {info['backend']} has no read side (strict verify "
              "impossible)")
        return 0
    res = backend.query("__ooptdd_doctor_probe__", since_us=0, until_us=1)
    info["reachable"] = res.reachable
    info["error"] = getattr(res, "error", None)  # WHY it failed (401 vs DNS vs unconfigured)
    human = (f"{'OK' if res.reachable else 'UNREACHABLE'} — {info['backend']} "
             f"reachable={res.reachable}")
    if info["error"]:
        human += f" ({info['error']})"
    _emit(info, args, human)
    return 0 if res.reachable else 2


def _cmd_monitor(args) -> int:
    # single-shot: surface the per-check streaming verdict/settled_at the kernel already
    # produces inside evaluate (the anticipatory LTL3 view). A bounded re-poll/watch belongs
    # to ooptdd-loop, not the stateless core.
    backend = _backend(args)
    res = evaluate(backend, load_gate(args.spec))
    view = {"cid": res["cid"], "ok": res["ok"], "reachable": res["reachable"],
            "complete": res.get("complete", True),
            "checks": [{"label": c.get("event") or next((k for k in
                        ("present", "absent", "must_order", "conforms", "heartbeat", "ratio",
                         "invariant")
                        if k in c), "check"),
                        "verdict": c.get("verdict"), "settled_at": c.get("settled_at"),
                        "passed": c["passed"]} for c in res["checks"]]}
    if getattr(args, "json", False):
        print(json.dumps(view, ensure_ascii=False, indent=2))
    else:
        for c in view["checks"]:
            print(f"  {c['verdict']:5} settled@{c['settled_at']}  {c['label']}", file=sys.stderr)
    return _exit(res["ok"], res["reachable"], res.get("complete", True))


def _cmd_verify_chain(args) -> int:
    records = _load_json_file(args.records)
    key = os.getenv(args.key_env)
    if not key:
        print(f"ERROR - signing key env {args.key_env} is unset (secrets are env-only)",
              file=sys.stderr)
        return 2
    if args.single:
        statuses = [signature_status(r, key) for r in records]
        ok = all(s == "valid" for s in statuses)
        _emit({"ok": ok, "statuses": statuses}, args, f"{'OK' if ok else 'TAMPER'} — {statuses}")
        return 0 if ok else 1
    res = verify_chain(records, key, evolve=args.evolve)
    _emit(res, args, f"{'OK' if res['ok'] else 'TAMPER'} — broken_index={res['broken_index']} "
          f"({res['reason']})")
    return 0 if res["ok"] else 1


_GATE_SCHEMA = """gate spec (gates/*.yaml) — keys:
  expect:                       # the list of checks
    - {event: NAME, op: ">="|">"|"=="|"!="|"<="|"<"|gte|gt|eq|ne|lte|lt, count|target: N}
    - {event: NAME, where: {field: value, ...}}      # partial-dict field filter
    - {present: [{event: A}, {event: B, where: {...}}]}   # subset, any order
    - {absent: {where: {level: ERROR}}}              # forbid wing (a.k.a. forbid:)
    - {must_order: [a, b, c], within_s: S}           # sequencing (a.k.a. trajectory:)
    - {heartbeat: NAME, every_s: S}                  # liveness
    - {ratioMetric: {good: {...}, total: {...}}, op: gte, target: 0.99}
    - {invariant: {left: {reduce: sum, field: amount, event: A},   # cross-event conservation
                   right: {reduce: count|sum|min|max|last, field: F, event: B},
                   op: "==", tol: 0.01}}
    - {metamorphic: {relation: equal|scaled|subset|monotone|idempotent,  # oracle-FREE relation
                     a: {event: A}, b: {event: B}, reduce: sum, field: F, factor: 2, tol: 0.01}}
    - {external: {kind: db_row, selector: {...}, op: "==", want: 42}}  # INDEPENDENT oracle (not
                                                     #   the trace) — needs evaluate(probe=...)
    - {conforms: EVENTTYPE, closed_world: true}      # ontology conformance
    - {indicatorRef: NAME}  with top-level indicators: {NAME: {event:.., where:..}}
  optional: true / pending: true / weight: N    (per-check modifiers)
  cid: ... | cid_env: OOPTDD_CID | timeWindow: 1h | threshold: 0.9
  require_corroboration: true    # single-authority gate (no separate-source external:) -> RED
  forbid_errors: true | error_levels: [ERROR, CRITICAL] | allow_errors: [{event: ..}]
"""
_ONTOLOGY_SCHEMA = """ontology file (yaml) — shape:
  closed_world: true|false        # an undeclared in-scope event name is drift
  event_types:
    EVENTNAME:
      required: [attr, ...]        # attrs that must be present
      constraints: {attr: {enum: [...], type: number|int|str|bool, min: N, max: N}}
      additional_properties: false # forbid undeclared payload attrs
"""


def _cmd_schema(args) -> int:
    if args.kind == "envelope":  # the machine-readable wire contract — emit the schema doc itself
        print(json.dumps(ENVELOPE_SCHEMA, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    text = _ONTOLOGY_SCHEMA if args.kind == "ontology" else _GATE_SCHEMA
    if getattr(args, "json", False):
        print(json.dumps({"kind": args.kind, "doc": text}, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


def _add_json(p):
    p.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ooptdd", description="logs-as-spec test verification")
    p.add_argument("--version", action="version", version=f"ooptdd {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="assert a cid's trace arrived (pytest summary or --gate)")
    v.add_argument("cid")
    v.add_argument("--gate", help="verify an arbitrary gate spec arrived (not the pytest summary)")
    v.add_argument("--backend")
    v.add_argument("--expect-total", type=int)
    v.add_argument("--retries", type=int, default=4)
    v.add_argument("--delay", type=float, default=1.0)
    v.set_defaults(func=_cmd_verify)

    g = sub.add_parser("gate", help="evaluate a YAML gate spec")
    g.add_argument("spec")
    g.add_argument("--backend")
    g.set_defaults(func=_cmd_gate)

    ln = sub.add_parser("lint", help="static strength audit of a gate spec (catch vacuous gates)")
    ln.add_argument("spec")
    _add_json(ln)
    ln.set_defaults(func=_cmd_lint)

    st = sub.add_parser("strength", help="gate strength fingerprint; --baseline catches weakening")
    st.add_argument("spec")
    st.add_argument("--baseline", help="JSON fingerprint to compare against (exit 1 if weaker)")
    st.add_argument("--write", help="write the fingerprint JSON to this path (a new baseline)")
    _add_json(st)
    st.set_defaults(func=_cmd_strength)

    d = sub.add_parser("can-i-deploy", help="Pact-style multi-gate deploy decision")
    d.add_argument("specs", nargs="+")
    d.add_argument("--backend")
    _add_json(d)
    d.set_defaults(func=_cmd_can_i_deploy)

    m = sub.add_parser("mutate", help="quantify a gate's discriminating power (mutation score)")
    m.add_argument("spec")
    m.add_argument("--events", required=True, help="JSON file: the baseline event list")
    m.add_argument("--min-score", type=float, help="fail (exit 1) if the score is below this")
    _add_json(m)
    m.set_defaults(func=_cmd_mutate)

    o = sub.add_parser("ontology", help="event-ontology conformance / compatibility")
    osub = o.add_subparsers(dest="onto_cmd", required=True)
    oc = osub.add_parser("check", help="validate events against an ontology")
    oc.add_argument("ontology")
    oc.add_argument("--events", required=True)
    oc.add_argument("--event-type")
    oc.add_argument("--closed-world", action="store_true")
    _add_json(oc)
    ok = osub.add_parser("compat", help="is old->new a safe ontology evolution?")
    ok.add_argument("old")
    ok.add_argument("new")
    ok.add_argument("--mode", choices=["backward", "forward", "full"], default="backward")
    _add_json(ok)
    o.set_defaults(func=_cmd_ontology)

    b = sub.add_parser("backends", help="list backends / probe the configured store")
    bsub = b.add_subparsers(dest="be_cmd", required=True)
    bl = bsub.add_parser("list", help="list built-in + entry-point backends")
    _add_json(bl)
    bd = bsub.add_parser("doctor", help="construct the backend and probe reachability")
    bd.add_argument("--backend")
    _add_json(bd)
    b.set_defaults(func=_cmd_backends)

    mon = sub.add_parser("monitor", help="show the per-check streaming verdict/settle of a gate")
    mon.add_argument("spec")
    mon.add_argument("--backend")
    _add_json(mon)
    mon.set_defaults(func=_cmd_monitor)

    vc = sub.add_parser("verify-chain", help="audit a tamper-evident receipt chain (HMAC)")
    vc.add_argument("--records", required=True, help="JSON file: the record list")
    vc.add_argument("--key-env", default="OOPTDD_SIGNING_KEY", help="env var holding the key")
    vc.add_argument("--evolve", action="store_true", help="key-evolving chain (forward secrecy)")
    vc.add_argument("--single", action="store_true", help="per-record signature_status instead")
    _add_json(vc)
    vc.set_defaults(func=_cmd_verify_chain)

    sc = sub.add_parser("schema", help="print the gate/ontology cheat-sheet or the envelope schema")
    sc.add_argument("kind", nargs="?", choices=["gate", "ontology", "envelope"], default="gate")
    _add_json(sc)
    sc.set_defaults(func=_cmd_schema)

    sub.add_parser("version", help="print version").set_defaults(
        func=lambda _a: (print(__version__) or 0)
    )

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        # A user/config error — a spec with no cid (`OOPTDD_CID` unset and no `cid:`), or a
        # missing spec file — is a clean one-line message on the INFRA/usage rung (exit 2), not
        # an uncaught traceback. The verdict rungs (0 GREEN / 1 RED / 2 INFRA) are unaffected.
        print(f"ERROR - {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
