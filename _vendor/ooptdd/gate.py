"""Back-compat shim — ``ooptdd.gate`` moved to :mod:`ooptdd.engine.gate` in 0.3.0.

Importing ``ooptdd.gate`` keeps working (code written against 0.2.x is not broken); new
code should import from :mod:`ooptdd.engine.gate` or the package root (``from ooptdd import
evaluate``). This module only re-exports.
"""
from __future__ import annotations

from .engine.gate import (  # noqa: F401
    _KEY_PROBES,
    CHECK_REGISTRY,
    EVIDENCE_TIERS,
    CheckCtx,
    _detect_check_key,
    _label,
    _matches,
    _resolve_matcher,
    can_i_deploy,
    check,
    duration_s,
    evaluate,
    evaluate_events,
    evidence_tier,
    failed_checks,
    load_gate,
    unregister,
)
