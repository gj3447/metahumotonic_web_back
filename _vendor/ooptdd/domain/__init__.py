"""Domain layer — pure data and the ports the engine depends on.

No I/O, no pytest, no backend driver, no network. This layer owns:

  - :mod:`ooptdd.domain.ports`     the ``Backend`` port + ``QueryResult`` (DIP boundary)
  - :mod:`ooptdd.domain.model`     pytest-report → structured event records, signing, chains
  - :mod:`ooptdd.domain.ontology`  EventType vocabulary + conformance + schema compatibility
  - :mod:`ooptdd.domain.semconv`   shipped ontology presets (OTel GenAI), self-registering

Dependency rule: ``domain`` imports only from ``domain`` and the stdlib. The engine and the
adapters depend on it; it depends on neither. The architecture fitness test enforces this.
"""
from .model import build_outcome_records, build_session_start
from .ontology import EventType, Ontology, check_conformance
from .ports import Backend, QueryResult

__all__ = [
    "Backend",
    "QueryResult",
    "build_outcome_records",
    "build_session_start",
    "EventType",
    "Ontology",
    "check_conformance",
]
