"""FileProbe — read a fact from the filesystem, a source separate from the trace store.

This is a reference adapter: it shows the whole shape of "make a probe as you need it". The
filesystem is genuinely independent of where the system shipped its events, so it is honest
``separate_source=True`` corroboration (e.g. assert a manifest the SUT was supposed to WRITE, not
merely log, actually exists with the right content).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..domain.ports import ProbeResult


class FileProbe:
    """``selector``: a path (str), or ``{path, json: "a.b.c"}`` to extract a field from a JSON
    file, or ``{path, exists: true}`` for a bare existence check. An unreadable path is
    ``reachable=False`` (inconclusive). ``root`` optionally prefixes relative paths."""

    def __init__(self, *, root: str | None = None):
        self.root = root

    def probe(self, kind, selector, cid) -> ProbeResult:
        sel = {"path": selector} if isinstance(selector, str) else dict(selector or {})
        path = sel.get("path", "")
        if self.root:
            path = os.path.join(self.root, path)
        p = Path(path)
        ident = str(p)  # the real location read — framework-comparable against the emit endpoint
        if sel.get("exists"):
            return ProbeResult(reachable=True, value=p.exists(),
                               separate_source=True, derived_identity=ident)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return ProbeResult(reachable=False, separate_source=True, derived_identity=ident)
        if sel.get("json"):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                return ProbeResult(reachable=True, value=None, complete=False,
                                   separate_source=True, derived_identity=ident)
            for key in str(sel["json"]).split("."):
                value = value.get(key) if isinstance(value, dict) else None
            return ProbeResult(reachable=True, value=value,
                               separate_source=True, derived_identity=ident)
        return ProbeResult(reachable=True, value=text.strip(),
                           separate_source=True, derived_identity=ident)
