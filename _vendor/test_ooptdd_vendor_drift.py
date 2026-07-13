"""Drift check for the vendored ooptdd core (copied in by scripts/vendor_ooptdd.py).

Two kinds of drift, two guards:
  * TAMPER — a vendored file edited away from the committed manifest. The manifest guards
    below RED the moment that happens. Pure stdlib, offline.
  * BEHIND — canonical moved on and this copy lagged. The manifest guards are BLIND to it
    (the vendor still matches its own stale snapshot, so it stays green — exactly how a
    consumer silently sits on an old ooptdd). ``test_vendored_matches_canonical_when_present``
    catches it whenever a canonical checkout is reachable via ``OOPTDD_CANONICAL``; offline
    it skips (never a false red), and the manifest guards still cover local integrity.

To sync with upstream, re-run ``python <ooptdd>/scripts/vendor_ooptdd.py <this-repo>`` —
the manifest changes show up as a git diff to review.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

_VENDOR = Path(__file__).resolve().parent           # …/_vendor
_MANIFEST = _VENDOR / "ooptdd_vendor_manifest.json"


def _normalized_sha256(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return hashlib.sha256(("\n".join(lines).rstrip("\n") + "\n").encode()).hexdigest()


def test_vendored_ooptdd_matches_manifest():
    manifest = json.loads(_MANIFEST.read_text())
    drifted = []
    for rel, want in manifest["files"].items():
        path = _VENDOR / "ooptdd" / rel
        assert path.exists(), f"vendored file missing: {rel} (re-vendor)"
        got = _normalized_sha256(path.read_text())
        if got != want:
            drifted.append(rel)
    assert not drifted, (
        f"vendored ooptdd drifted from manifest: {drifted}. "
        "Someone edited the vendored copy directly. Re-vendor from canonical: "
        "python <ooptdd>/scripts/vendor_ooptdd.py <this-repo>"
    )


def test_vendored_tree_matches_manifest_set():
    """The set of vendored ``.py`` files must equal the manifest's set — no extras, none
    missing. The per-file content check above is blind to a file *added* or *removed*
    upstream (a structural drift, e.g. the engine/+domain/ split): an orphaned stale module
    or a newly-required one would slip through. Offline, stdlib — no git needed."""
    manifest = json.loads(_MANIFEST.read_text())
    declared = set(manifest["files"])
    pkg = _VENDOR / "ooptdd"
    present = {
        p.relative_to(pkg).as_posix()
        for p in pkg.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    assert present == declared, (
        f"vendored tree != manifest. extra (orphaned): {sorted(present - declared)}; "
        f"missing (un-vendored): {sorted(declared - present)}. Re-vendor from canonical: "
        "python <ooptdd>/scripts/vendor_ooptdd.py <this-repo>"
    )


def test_vendored_matches_canonical_when_present():
    """The BEHIND guard. The two manifest guards above only prove the vendor matches its own
    committed snapshot — they stay green while canonical moves on, so a consumer can silently
    lag upstream (the real failure mode: a flagship copy stuck on an old version). When a
    canonical ooptdd checkout is reachable via the ``OOPTDD_CANONICAL`` env (dev box / CI with
    the source mounted), every vendored file must equal it. Offline (env unset or path absent)
    this SKIPS — never a false red — and the manifest guards still cover local integrity."""
    root = os.getenv("OOPTDD_CANONICAL")
    canon = Path(root) / "src" / "ooptdd" if root else None
    if canon is None or not canon.exists():
        pytest.skip("OOPTDD_CANONICAL unset or absent — offline; manifest guards cover integrity")
    manifest = json.loads(_MANIFEST.read_text())
    behind = []
    for rel in manifest["files"]:
        c = canon / rel
        assert c.exists(), f"canonical dropped ooptdd/{rel} — re-vendor this repo"
        if _normalized_sha256(c.read_text()) != _normalized_sha256((_VENDOR / "ooptdd" / rel).read_text()):
            behind.append(rel)
    assert not behind, (
        f"vendored ooptdd is BEHIND canonical on {behind}. The manifest guards passed because "
        f"the vendor still matches its own (stale) snapshot — canonical moved on and this copy "
        f"lagged. Re-vendor: python <ooptdd>/scripts/vendor_ooptdd.py <this-repo>"
    )
