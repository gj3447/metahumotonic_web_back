"""Back-compat shim — ``ooptdd.model`` moved to :mod:`ooptdd.domain.model` in 0.3.0.

Re-exports only; new code should import from :mod:`ooptdd.domain.model`.
"""
from __future__ import annotations

from .domain.model import (  # noqa: F401
    ENVELOPE_SCHEMA,
    ENVELOPE_SPEC_VERSION,
    SIG_ALG,
    build_outcome_records,
    build_session_start,
    cloudevents_envelope,
    correlation_keys,
    sign_chain,
    sign_record,
    signature_status,
    validate_cloudevents,
    verify_chain,
    with_trace_context,
)
