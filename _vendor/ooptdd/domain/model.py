"""Pure data shaping — no I/O, no pytest, no backend.

`build_outcome_records` turns a list of plain pytest report dicts into the
structured event envelopes ooptdd ships. Keeping this pure means it is trivially
unit-testable and identical across every backend.

Envelope shape (one JSON object per event)::

    {
      "cid": "...", "correlation_id": "...", "cycle_id": "...",   # 3 aliases
      "service": "myapp.tests", "level": "INFO|ERROR",
      "event": "test_outcome" | "test_session",
      ...event-specific fields...
    }

We emit the correlation id under three keys because different stores index
different column names; carrying all three keeps "one query by id" working no
matter which backend you point at.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading

# When one test produces several phase reports (setup/call/teardown) we keep the
# most severe outcome for the session tally.
_RANK = {"failed": 2, "skipped": 1, "passed": 0}

# Anti-fabrication (#2): the session summary IS the green-claim, so it is the forgery
# target. We HMAC a fixed projection of its identity-bearing fields. `cid` is signed so a
# real signed record can't be replayed under a forged correlation id. Per-test outcomes
# are NOT signed (high cardinality) — the verifier cross-checks their count vs the signed
# `total`. The key lives only where CI injects it (OOPTDD_SIGNING_KEY), never in code.
_SIGNED_FIELDS = ("cid", "event", "total", "passed", "failed", "skipped")
SIG_ALG = "hmac-sha256-v1"


def correlation_keys(cid: str) -> dict:
    """The id under every alias a backend might index on."""
    return {"cid": cid, "correlation_id": cid, "cycle_id": cid}


# ── CloudEvents 1.0 floor ──────────────────────────────────────────────────────
# CloudEvents (CNCF) is the vendor-neutral event envelope standard. We don't adopt
# the whole spec — only its *required floor* (4 context attributes) — so an ooptdd
# event is recognizable to any CloudEvents-aware store/router without us reinventing
# id/source/type semantics. Mapping: event->type, service->source, cid->subject.
CE_SPECVERSION = "1.0"
CE_REQUIRED = ("id", "source", "specversion", "type")


def cloudevents_envelope(rec: dict, *, source: str | None = None) -> dict:
    """Project an ooptdd record onto the CloudEvents 1.0 floor (non-destructive copy).

    ``id`` is a deterministic content hash, so re-shipping the same record yields the
    same CloudEvents id (idempotent — no duplicate events on retry). ``source`` defaults
    to the record's ``service``; ``subject`` carries the correlation id.
    """
    src = source or rec.get("service") or "ooptdd"
    cid = rec.get("cid") or rec.get("correlation_id") or rec.get("cycle_id")
    body = json.dumps(
        rec, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode()
    out = dict(rec)
    out.update({
        "id": hashlib.sha256(body).hexdigest()[:32],
        "source": str(src),
        "specversion": CE_SPECVERSION,
        "type": str(rec.get("event", "")),
    })
    if cid is not None:
        out["subject"] = str(cid)
    return out


# ── ooptdd event-envelope wire contract ─────────────────────────────────────────
# A versioned, machine-readable schema for the envelope EVERY shipped record carries — distinct
# from CE_SPECVERSION (the CloudEvents floor above, which versions only the 4 CE context attrs).
# Out-of-process emitters (p333's Rust, omd) previously re-implemented the envelope by imitation
# and drifted; this is the single source of truth they validate against. Stamped into every
# builder record as `spec_version`; the on-disk docs/schema/envelope.schema.json is a mirror kept
# honest by tests/test_wire_contract.py (the package constant is authoritative — the JSON is not
# vendored, so the CLI emits from here, never by reading the file).
ENVELOPE_SPEC_VERSION = "1.0.0"
ENVELOPE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/airobotics-ailab/ooptdd/schema/envelope.schema.json",
    "title": "ooptdd event envelope",
    "type": "object",
    "required": ["spec_version", "cid", "correlation_id", "cycle_id", "service", "level", "event"],
    "properties": {
        "spec_version": {"const": ENVELOPE_SPEC_VERSION},
        "cid": {"type": "string"},
        "correlation_id": {"type": "string"},
        "cycle_id": {"type": "string"},
        "service": {"type": "string"},
        "level": {"type": "string", "enum": ["INFO", "ERROR"]},
        "event": {"type": "string"},
    },
    # Records also carry event-specific payload (duration_s/error/total/sig/trace_id/…); the
    # envelope contract pins the carrier, not the payload — so extra keys are allowed.
    "additionalProperties": True,
}


def with_trace_context(rec: dict, trace_id: str, span_id: str | None = None) -> dict:
    """Attach W3C trace context (``trace_id``/``span_id``) to an event (non-destructive).

    OTel log records carry these so a log line joins to its span; ooptdd uses them as a
    *standard* correlation key alongside ``cid`` — binding an emitted event to the exact
    run/span that produced it. Unlike ``gen_ai.*`` (experimental), trace context is stable.
    """
    out = dict(rec)
    out["trace_id"] = str(trace_id)
    if span_id is not None:
        out["span_id"] = str(span_id)
    return out


def validate_cloudevents(rec: dict) -> list[str]:
    """Violations against the CloudEvents 1.0 floor (each required attr a non-empty
    string). Empty list = conforms. ``type`` must be present *and* non-empty — an event
    with no name is not a valid CloudEvent."""
    out: list[str] = []
    for k in CE_REQUIRED:
        v = rec.get(k)
        if not isinstance(v, str) or not v:
            out.append(f"missing/empty required CloudEvents attr '{k}'")
    return out


def _canonical(rec: dict) -> bytes:
    """Deterministic bytes for the signed-field projection (sig itself excluded)."""
    proj = {k: rec.get(k) for k in _SIGNED_FIELDS}
    return json.dumps(proj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign_record(rec: dict, key: str) -> str:
    """HMAC-SHA256 of the canonical projection (hex)."""
    return hmac.new(key.encode(), _canonical(rec), hashlib.sha256).hexdigest()


def signature_status(rec: dict, key: str | None) -> str:
    """valid | invalid | unsigned | unverifiable.

    unsigned     no sig on the record (sender had no key — offline/no-CI no-op)
    unverifiable sig present but the verifier has no key (can't judge — never a failure)
    valid/invalid  sig present and the verifier recomputed it (constant-time compare)
    """
    have = rec.get("sig")
    if not have:
        return "unsigned"
    if not key:
        return "unverifiable"
    return "valid" if hmac.compare_digest(have, sign_record(rec, key)) else "invalid"


# ── tamper-evident hash chain (Tier-3 #11) ─────────────────────────────────────
# The single-record `sig` catches an edit to *that* record. A hash chain catches more:
# deletion and reordering of receipts too — an agent can't silently drop an inconvenient
# event. Each record's MAC folds in the previous MAC (Schneier-Kelsey / Crosby-Wallach
# tamper-evident logging). With key evolution (k_{i+1}=H(k_i)) a leaked *current* key
# can't forge *earlier* receipts (forward security). Scope to one writer per stream
# (the xdist controller), since the chain needs a single ordered append.
_CHAIN_EXCLUDE = ("sig_chain", "prev_sig")


def _chain_canonical(rec: dict) -> bytes:
    proj = {k: v for k, v in rec.items() if k not in _CHAIN_EXCLUDE}
    return json.dumps(
        proj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode()


def _evolve(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def sign_chain(records: list[dict], key: str, *, evolve: bool = False) -> list[dict]:
    """Return copies of ``records`` carrying a tamper-evident hash chain.

    ``rec["sig_chain"] = HMAC(k_i, canonical(rec) || prev_mac)`` and ``rec["prev_sig"]``
    links to the previous record. ``evolve=True`` ratchets the key forward per record.
    """
    out: list[dict] = []
    prev, k = "", key
    for rec in records:
        r = dict(rec)
        mac = hmac.new(k.encode(), _chain_canonical(r) + prev.encode(), hashlib.sha256).hexdigest()
        r["prev_sig"] = prev
        r["sig_chain"] = mac
        out.append(r)
        prev = mac
        if evolve:
            k = _evolve(k)
    return out


def verify_chain(records: list[dict], key: str, *, evolve: bool = False) -> dict:
    """Verify a hash chain. Returns ``{ok, broken_index, reason}`` — ``broken_index`` is the
    first record whose previous-link or MAC fails (``None`` if intact). A mismatch means an
    edit, a deletion, or a reorder somewhere at or before that index."""
    prev, k = "", key
    for i, rec in enumerate(records):
        if rec.get("prev_sig") != prev:
            return {"ok": False, "broken_index": i,
                    "reason": "prev_link_mismatch_possible_deletion_or_reorder"}
        expect = hmac.new(
            k.encode(), _chain_canonical(rec) + prev.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(str(rec.get("sig_chain", "")), expect):
            return {"ok": False, "broken_index": i, "reason": "chain_mac_mismatch_possible_tamper"}
        prev = str(rec.get("sig_chain", ""))
        if evolve:
            k = _evolve(k)
    return {"ok": True, "broken_index": None, "reason": None}


def build_event(cid: str, event: str, *, service: str = "ooptdd.tests", **attrs) -> dict:
    """The generic emit envelope (pure): one structured event under all three correlation aliases,
    stamped with the wire ``spec_version``, plus a ``service`` and any event-specific ``attrs``.
    This is what a consumer ships instead of hand-rolling a flat dict per verb — the same shape the
    pytest builders produce, so one gate grammar reads them all."""
    return {**correlation_keys(cid), "spec_version": ENVELOPE_SPEC_VERSION,
            "service": service, "event": event, **attrs}


class Emitter:
    """A thin, thread-safe emit seam over an injected backend: ``emit(event, cid, **attrs)`` builds
    one :func:`build_event` and ships it. ``backend`` is duck-typed (anything with ``ship(list)``)
    so this stays a pure-domain leaf — it never imports ``ooptdd.backends``. The lock serializes
    only the ``ship`` call (no re-entrancy/callback), so it cannot deadlock a backend that does its
    own locking."""

    def __init__(self, backend, service: str = "ooptdd.tests"):
        self._backend = backend
        self.service = service
        self._lock = threading.Lock()

    def build(self, event: str, cid: str, **attrs) -> dict:
        return build_event(cid, event, service=self.service, **attrs)

    def emit(self, event: str, cid: str, **attrs) -> dict:
        rec = self.build(event, cid, **attrs)
        with self._lock:
            self._backend.ship([rec])
        return rec


def build_outcome_records(
    reports: list[dict],
    cid: str,
    *,
    service: str = "ooptdd.tests",
    meta: dict | None = None,
    signing_key: str | None = None,
) -> list[dict]:
    """pytest reports -> structured event records (pure function).

    ``reports``: ``[{nodeid, outcome, duration, when[, longrepr]}]``.
    Returns N per-phase ``test_outcome`` events (tracebacks preserved for RCA)
    plus exactly one ``test_session`` summary. The summary tally is computed over
    *distinct* nodeids so a teardown-failure can't double-count (a real bug we hit).

    ``signing_key`` (passed in, never read from env here — keeps this pure): when given,
    the session summary is HMAC-signed so the verifier can detect a forged green receipt.
    """
    meta = meta or {}
    recs: list[dict] = []
    for r in reports:
        outcome = r["outcome"]
        rec = {
            **correlation_keys(cid),
            "spec_version": ENVELOPE_SPEC_VERSION,
            "service": service,
            "level": "ERROR" if outcome == "failed" else "INFO",
            "event": "test_outcome",
            "test": r["nodeid"],
            "outcome": outcome,
            "when": r.get("when", "call"),
            "duration_s": round(float(r.get("duration", 0.0)), 4),
        }
        if outcome == "failed" and r.get("longrepr"):
            rec["error"] = str(r["longrepr"])[:2000]
        recs.append(rec)

    by_test: dict[str, str] = {}  # nodeid -> most severe outcome
    for r in reports:
        prev = by_test.get(r["nodeid"])
        if prev is None or _RANK.get(r["outcome"], 0) > _RANK.get(prev, 0):
            by_test[r["nodeid"]] = r["outcome"]
    passed = sum(1 for o in by_test.values() if o == "passed")
    failed = sum(1 for o in by_test.values() if o == "failed")
    skipped = sum(1 for o in by_test.values() if o == "skipped")
    session = {
        **correlation_keys(cid),
        "spec_version": ENVELOPE_SPEC_VERSION,
        "service": service,
        "level": "ERROR" if failed else "INFO",
        "event": "test_session",
        "total": len(by_test),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        **meta,
    }
    if signing_key:
        session["sig"] = sign_record(session, signing_key)  # over the projection, pre-sig
        session["sig_alg"] = SIG_ALG
    recs.append(session)
    return recs


def build_session_start(
    cid: str,
    *,
    service: str = "ooptdd.tests",
    expected_total: int | None = None,
    meta: dict | None = None,
) -> dict:
    """A heartbeat shipped *before* the run (pure function).

    If the ``test_session`` summary is later lost, the presence of this record lets
    ``verify_trace`` tell "the run started but its summary never arrived" (partial loss)
    from "nothing arrived at all" (total loss) — two very different RCA paths.
    """
    rec = {
        **correlation_keys(cid),
        "spec_version": ENVELOPE_SPEC_VERSION,
        "service": service,
        "level": "INFO",
        "event": "session_start",
        **(meta or {}),
    }
    if expected_total is not None:
        rec["expected_total"] = expected_total
    return rec
