"""Back-compat shim — ``ooptdd.verify`` moved to :mod:`ooptdd.engine.verify` in 0.3.0.

Re-exports only; new code should import from :mod:`ooptdd.engine.verify` or the package root.
"""
from __future__ import annotations

from .engine.verify import (  # noqa: F401
    poll_until_present,
    session_finish,
    verify_gate,
    verify_policy,
    verify_trace,
)
