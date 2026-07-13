"""Back-compat shim — ``ooptdd.semconv`` moved to :mod:`ooptdd.domain.semconv` in 0.3.0.

Re-exports only; new code should import from :mod:`ooptdd.domain.semconv`. Importing this
module also runs the preset self-registration (``Ontology.register_preset("gen_ai", ...)``).
"""
from __future__ import annotations

from .domain.semconv import (  # noqa: F401
    GEN_AI_PROVIDERS,
    SEMCONV_VERSION,
    gen_ai_ontology,
)
