"""Pure validation boundary for the public domain listing.

Incomplete KG records are omitted, never filled with invented measurements.
The HTTP adapter must disclose omissions and reject an all-invalid projection.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal, Sequence
from pydantic import ValidationError
from .contracts import DomainRecord

@dataclass(frozen=True)
class DomainProjection:
    items: tuple[DomainRecord, ...]
    source: Literal["live", "snapshot"]
    omitted: int = 0

    @property
    def quality(self) -> str:
        if self.omitted:
            return "partial" if self.items else "unavailable"
        return "complete"

def project_domains(rows: Sequence[Any] | None,
                    fallback: Sequence[DomainRecord]) -> DomainProjection:
    if rows is None:
        return DomainProjection(tuple(fallback), "snapshot")
    valid: list[DomainRecord] = []
    omitted = 0
    for row in rows:
        try:
            valid.append(DomainRecord.model_validate(row))
        except ValidationError:
            # Do not log a Pydantic exception: it includes the input values.
            omitted += 1
    return DomainProjection(tuple(valid), "live", omitted)
