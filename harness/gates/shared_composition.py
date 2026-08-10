#!/usr/bin/env python3
"""SPEC §4-3 — production and harness must consume the SAME composition graph.

    "각자 배선을 따로 가지면 '하네스에서는 호출되는데 실제 진입점에서는 안 불리는'
     결함이 구조적으로 생긴다."

On 2026-08-10 that defect happened here verbatim. The test file assembled its
own app (two shared layers out of eleven), so providing an API-level middleware
to `HttpApiBuilder.serve` instead of `HttpApiBuilder.api` dropped every prefixed
route in production while 117 tests stayed green.

The fix was `src/server/Composition.ts`. This gate keeps it fixed: exactly one
module may define the wiring, and everything else — production entrypoint and
harness alike — must go through it.

Exit 0 = one composition. Exit 1 = it has split again.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TS = ROOT / "ts"
COMPOSITION = TS / "src" / "server" / "Composition.ts"
ENTRYPOINT = TS / "src" / "main.ts"

# Constructing the app, as opposed to consuming it. Only Composition.ts may.
WIRING_CALLS = [
    r"HttpApiBuilder\.api\s*\(",
    r"HttpApiBuilder\.serve\s*\(",
]

# The pieces of the wiring itself. A caller importing these is re-assembling
# rather than substituting, which is what §4-3 forbids.
WIRING_IMPORTS = ["HandlersLive", "OperatorPlaneNoStore", "apiLayer"]

failures: list[str] = []


def strip_comments_and_strings(src: str) -> str:
    """Blank out comments and string bodies before pattern matching.

    A gate that fires on prose is a gate someone eventually disables. The first
    version of this file flagged `Middleware.ts` because a COMMENT there
    mentions `HttpApiBuilder.serve(middleware)` while explaining why the
    middleware does not go there — the gate was reading documentation as code.

    Small state machine rather than a regex: `//` inside a string (a URL) and a
    quote inside a comment both break the naive version.
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if quote is not None:
            if ch == "\\":
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch + nxt == "//":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch + nxt == "/*":
            while i < n and src[i : i + 2] != "*/":
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        if ch in "\"'`":
            quote = ch
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def code_of(path: Path) -> str:
    return strip_comments_and_strings(path.read_text(encoding="utf-8"))


def sources() -> list[Path]:
    return sorted(
        [p for p in (TS / "src").rglob("*.ts")] + [p for p in (TS / "test").rglob("*.ts")]
    )


def main() -> int:
    if not COMPOSITION.is_file():
        print(f"FAIL the single composition module is missing: {COMPOSITION.relative_to(ROOT)}")
        return 1

    # --- 1. only Composition.ts constructs the app ------------------------
    for path in sources():
        if path == COMPOSITION:
            continue
        text = code_of(path)
        for pattern in WIRING_CALLS:
            if re.search(pattern, text):
                call = pattern.replace(chr(92), "").replace(r"s*(", "(")
                failures.append(
                    f"{path.relative_to(TS)} calls {call} — "
                    "only src/server/Composition.ts may construct the app"
                )

    # --- 2. nobody imports the wiring pieces directly ---------------------
    for path in sources():
        if path == COMPOSITION:
            continue
        text = code_of(path)
        for name in WIRING_IMPORTS:
            if re.search(rf"import\s+\{{[^}}]*\b{name}\b[^}}]*\}}", text):
                failures.append(
                    f"{path.relative_to(TS)} imports {name} — go through "
                    "serveLayer()/webHandlerLayer() instead of re-wiring"
                )

    # --- 3. the entrypoint consumes it ------------------------------------
    entry = code_of(ENTRYPOINT)
    if "serveLayer" not in entry:
        failures.append("src/main.ts does not use serveLayer() from Composition.ts")

    # --- 4. the harness consumes it ---------------------------------------
    harness_users = [
        p for p in (TS / "test").rglob("*.ts")
        if "webHandlerLayer" in code_of(p)
    ]
    http_tests = [
        p for p in (TS / "test").rglob("*.ts")
        if "new Request(" in code_of(p)
    ]
    for p in http_tests:
        if p not in harness_users:
            failures.append(
                f"{p.relative_to(TS)} drives HTTP but does not use webHandlerLayer() — "
                "it is assembling its own app"
            )

    shared = "webHandlerLayer" in code_of(COMPOSITION)
    print(f"shared_composition: 1 wiring module, {len(harness_users)} harness consumer(s), "
          f"entrypoint {'ok' if 'serveLayer' in entry else 'MISSING'}, "
          f"harness plane {'exported' if shared else 'MISSING'}")

    if failures:
        print(f"\nFAIL the composition graph has split ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        print("\n     SPEC §4-3: adapters and observation may differ; the wiring may not.")
        return 1

    print("PASS production and harness consume the same composition graph")
    return 0


if __name__ == "__main__":
    sys.exit(main())
