"""Back-compat shim — ``ooptdd.ontology`` moved to :mod:`ooptdd.domain.ontology` in 0.3.0.

Re-exports only; new code should import from :mod:`ooptdd.domain.ontology` or the root.
"""
from __future__ import annotations

from .domain.ontology import (  # noqa: F401
    EventType,
    Ontology,
    check_conformance,
    ontology_compat,
)
