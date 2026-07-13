"""ooptdd trace emitter — the 측정(measurement) layer of the PI 3-layer dev stack.

    조율 OMD · 측정 ooptdd/LTDD · 판정 LakatoTree   (delltower_import/CLAUDE.md)

ooptdd = *positive TDD / log-based TDD*: the pipeline ships structured events to a
store, and tests read them **back** and positively assert what actually happened —
instead of trusting a return value (which can lie: `save()` returns an id even when
the durable write silently fell back to memory).

Zero-infra by default: an in-process `MemoryBackend` (tests + offline). Set
`MHB_OOPTDD_OO_URL` (+ `OOPTDD_OO_PASSWORD`) to ship to OpenObserve in prod. If the
vendored ooptdd core (`_vendor/ooptdd`) is missing, every call is a **no-op** —
tracing is best-effort and never breaks the request path.

# KG: project_metahumotonic_web_integrate_core_dev_tech_2026_07_13, project_lakatotree_oo_ptdd_2026_06_14
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("mhb.trace")

# Vendored ooptdd core lives at repo-root/_vendor (self-contained; no PyPI/path-dep).
_VENDOR = Path(__file__).resolve().parent.parent / "_vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

try:
    from ooptdd import MemoryBackend, get_backend  # type: ignore

    _AVAILABLE = True
except Exception as e:  # ooptdd core absent → tracing disabled, service unaffected
    MemoryBackend = None  # type: ignore
    get_backend = None  # type: ignore
    _AVAILABLE = False
    logging.getLogger("mhb.trace").info("ooptdd core unavailable, tracing off: %s", e)

SERVICE = "mhb.web_back"

_backend = None


def _make_backend():
    if not _AVAILABLE:
        return None
    url = os.environ.get("MHB_OOPTDD_OO_URL") or os.environ.get("OOPTDD_OO_URL")
    if url:
        try:
            return get_backend("openobserve")  # reads OOPTDD_OO_* from env
        except Exception as e:  # pragma: no cover - infra dependent
            log.warning("openobserve backend init failed, using memory: %s", e)
    return MemoryBackend()


def backend():
    """The active ooptdd backend (or None when tracing is off). Read events off
    this in tests: `evaluate(trace.backend(), spec, cid=...)`."""
    global _backend
    if _backend is None:
        _backend = _make_backend()
    return _backend


def reset() -> None:
    """Swap in a fresh backend — used per-test so events don't bleed across cases."""
    global _backend
    _backend = _make_backend()


def emit(event: str, cid: str, **attrs) -> None:
    """Ship one structured pipeline event under correlation id `cid`. Best-effort:
    any failure (backend down, ooptdd absent) is swallowed so the caller's path is
    never affected."""
    b = backend()
    if b is None:
        return
    try:
        b.ship(
            [
                {
                    "cid": cid,
                    "correlation_id": cid,
                    "cycle_id": cid,
                    "service": SERVICE,
                    "event": event,
                    **attrs,
                }
            ]
        )
    except Exception as e:  # pragma: no cover - best-effort
        log.debug("trace emit dropped (%s): %s", event, e)
