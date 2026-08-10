#!/usr/bin/env python3
"""SPEC §4-4 — `orphan_module(M)`: M is unreachable from an entrypoint.

    "등록되지 않은 모듈은 존재하지 않는 모듈로 취급한다."

This is the gate that would have caught `src/agent/Loop.ts` on the day it was
written: 176 lines, imported by nothing, and 117 green tests said nothing about
it. Dead code cannot fail, which is exactly why it is never right.

Reachability is computed from the PRODUCTION entrypoint only. A module reached
only from a test is still an orphan — that is the T8 (조립 맹점) case the spec
names, and counting test imports would hide precisely the defect we are hunting.

Exit 0 = no orphans. Exit 1 = orphans, listed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "ts" / "src"
ENTRYPOINTS = [SRC / "main.ts"]

# A module may be declared an intentional non-entrypoint export — a library
# surface consumed from outside this tree. Each entry needs a reason, so the
# allowlist cannot quietly become a place to hide dead code.
ALLOWED_ORPHANS: dict[str, str] = {}

IMPORT_RE = re.compile(
    r"""^\s*(?:import|export)\b[^'"]*?from\s+['"](\.[^'"]+)['"]|"""
    r"""^\s*import\s+['"](\.[^'"]+)['"]""",
    re.MULTILINE,
)


def resolve(importer: Path, spec: str) -> Path | None:
    """`./x.js` in source refers to `./x.ts` on disk (NodeNext convention)."""
    target = (importer.parent / spec).resolve()
    for candidate in (
        target.with_suffix(".ts"),
        Path(str(target)[:-3] + ".ts") if str(target).endswith(".js") else target,
        target / "index.ts",
    ):
        if candidate.is_file():
            return candidate.resolve()
    return None


def imports_of(path: Path) -> list[Path]:
    text = path.read_text(encoding="utf-8")
    out: list[Path] = []
    for m in IMPORT_RE.finditer(text):
        spec = m.group(1) or m.group(2)
        if not spec:
            continue
        resolved = resolve(path, spec)
        if resolved is not None:
            out.append(resolved)
    return out


def main() -> int:
    all_modules = {p.resolve() for p in SRC.rglob("*.ts")}
    if not all_modules:
        print("orphan_module: no sources found — refusing to report a vacuous pass")
        return 1

    reachable: set[Path] = set()
    stack = [e.resolve() for e in ENTRYPOINTS if e.is_file()]
    if not stack:
        print(f"orphan_module: no entrypoint found ({[str(e) for e in ENTRYPOINTS]})")
        return 1

    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        stack.extend(imports_of(cur))

    orphans = sorted(all_modules - reachable)
    allowed = {SRC / rel for rel in ALLOWED_ORPHANS}
    unexpected = [p for p in orphans if p not in allowed]

    print(f"orphan_module: {len(reachable)}/{len(all_modules)} modules reachable from main.ts")
    for p in orphans:
        rel = p.relative_to(SRC)
        note = ALLOWED_ORPHANS.get(str(rel))
        mark = "allowed" if note else "ORPHAN "
        print(f"  {mark} {rel}" + (f"  — {note}" if note else ""))

    if unexpected:
        print(
            f"\nFAIL {len(unexpected)} module(s) unreachable from the production entrypoint.\n"
            "     Either wire them in, delete them, or add them to ALLOWED_ORPHANS with a reason.\n"
            "     A module nothing imports is a module that does not exist (SPEC §4-4)."
        )
        return 1

    print("PASS every module is reachable from the production entrypoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
