#!/usr/bin/env python3
"""The harness runner — SPEC §4 Gate, §5 DONE, §6 machine-generated evidence.

Runs every gate in `harness/gates/manifest.json`, folds the results into the
six DONE terms, and writes a receipt.

Three things make this different from `pytest && npm test`:

1. **Four-valued.** A gate is GREEN / RED / INCONCLUSIVE / NOT_RUN (§4-5).
   A binary gate turns a property it cannot decide into a false GREEN; that is
   the specific failure §4-5 exists to prevent. `Environment` is the live
   example here — it cannot be decided from this checkout, so it reports
   NOT_RUN and blocks DONE, which is the honest answer.

2. **DONE is a conjunction over terms, not a test count** (§5).
   `파일 생성됨 ≠ 단위테스트 통과 ≠ 하네스 통과 ≠ DONE`.

3. **The evidence is machine-generated** (§6). The receipt holds exit codes,
   durations, command lines and output digests — not prose.

Usage:
    python3 harness/runner/verify.py            # everything
    python3 harness/runner/verify.py --quick    # skip slow gates
    python3 harness/runner/verify.py --autonomous   # refuses without harness/BUDGET.md (B1)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "harness" / "gates" / "manifest.json"
BUDGET = ROOT / "harness" / "BUDGET.md"
RECEIPTS = ROOT / "harness" / "_receipts"

GREEN, RED, INCONCLUSIVE, NOT_RUN = "GREEN", "RED", "INCONCLUSIVE", "NOT_RUN"

# Only GREEN advances a DONE term. This ordering is the whole point: a term is
# as bad as its worst gate, and "we could not tell" is worse than "it passed".
SEVERITY = {GREEN: 0, INCONCLUSIVE: 1, NOT_RUN: 2, RED: 3}

QUICK_SKIP = {"py-full-suite"}


def sh(cmd: list[str], cwd: Path, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "CI": "1", "FORCE_COLOR": "0"},
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        # A timeout is NOT a failure of the property — we simply did not find
        # out. Reporting it RED would be a claim we cannot support.
        return -1, f"__TIMEOUT__ after {timeout}s"
    except FileNotFoundError as e:
        return -2, f"__MISSING_TOOL__ {e}"


def run_gate(gate: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    cmd = gate.get("cmd")

    if cmd is None:
        return {
            "id": gate["id"], "term": gate["term"], "verdict": NOT_RUN,
            "seconds": 0.0, "cmd": None,
            "why": " ".join(gate.get("notRunReason", ["no command defined"])),
        }

    code, out = sh(cmd, ROOT / gate.get("cwd", "."))

    if out.startswith("__TIMEOUT__") or out.startswith("__MISSING_TOOL__"):
        verdict, why = INCONCLUSIVE, out.strip()
    elif code == gate.get("inconclusiveExit", -999):
        # The gate reached its own conclusion that it could not conclude — e.g.
        # the target environment is unreachable. That is not a pass and not a
        # failure, and flattening it to either would be a lie (SPEC §4-5).
        verdict, why = INCONCLUSIVE, out.strip()[-600:]
    elif code == 0:
        verdict, why = GREEN, ""
    else:
        verdict, why = RED, out.strip()[-1200:]

    # A gate declared non-monitorable can never be GREEN, whatever it printed.
    if not gate.get("monitorable", True) and verdict == GREEN:
        verdict = INCONCLUSIVE
        why = "declared non-monitorable: a finite local trace cannot decide this"

    return {
        "id": gate["id"], "term": gate["term"], "verdict": verdict,
        "seconds": round(time.time() - started, 2),
        "cmd": " ".join(cmd), "cwd": gate.get("cwd", "."),
        "outputSha256": hashlib.sha256(out.encode()).hexdigest()[:16],
        "why": why,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the slow regression gates")
    ap.add_argument("--autonomous", action="store_true", help="refuse to start without a budget (B1)")
    ap.add_argument("--only", default="", help="run one gate by id")
    args = ap.parse_args()

    # B1 as a gate, not as prose (B5). An autonomous run without a declared
    # budget does not start.
    if args.autonomous and not BUDGET.exists():
        print("REFUSED  harness/BUDGET.md is missing — B1 forbids budgetless autonomous runs")
        return 2

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    gates = manifest["gates"]
    if args.only:
        gates = [g for g in gates if g["id"] == args.only]
        if not gates:
            print(f"no gate with id {args.only!r}")
            return 2
    if args.quick:
        gates = [g for g in gates if g["id"] not in QUICK_SKIP]

    # The smoke gate runs the compiled output, so build once up front.
    if any(g.get("requiresBuild") for g in gates):
        code, out = sh(["npm", "run", "build", "--silent"], ROOT / "ts")
        if code != 0:
            print("BUILD FAILED\n" + out[-2000:])
            return 1

    print(f"harness: {len(gates)} gates\n")
    results = [run_gate(g) for g in gates]
    for r in results:
        mark = {GREEN: "ok  ", RED: "FAIL", INCONCLUSIVE: "??  ", NOT_RUN: "--  "}[r["verdict"]]
        print(f"  {mark} {r['id']:<28} {r['term']:<12} {r['verdict']:<13} {r['seconds']:>6.1f}s")
        if r["verdict"] in (RED, INCONCLUSIVE) and r["why"]:
            first = r["why"].splitlines()[0][:150]
            print(f"       └─ {first}")

    # --- fold gates into DONE terms (§5) ---------------------------------
    terms: dict[str, str] = {t: NOT_RUN for t in manifest["doneTerms"]}
    for r in results:
        if SEVERITY[r["verdict"]] > SEVERITY[terms[r["term"]]]:
            terms[r["term"]] = r["verdict"]
        elif terms[r["term"]] == NOT_RUN and r["verdict"] == GREEN:
            terms[r["term"]] = GREEN

    done = all(v == GREEN for v in terms.values())

    print("\n  DONE(F) = Pure ∧ Reachable ∧ Contract ∧ Executable ∧ Environment ∧ Regression")
    print("  " + "  ".join(f"{t}={v}" for t, v in terms.items()))
    print(f"\n  DONE: {'YES' if done else 'NO'}")
    if not done:
        blocking = [t for t, v in terms.items() if v != GREEN]
        print(f"  blocked by: {', '.join(f'{t}({terms[t]})' for t in blocking)}")

    RECEIPTS.mkdir(exist_ok=True)
    receipt = {
        "schema": "harness-receipt/v1",
        "manifestVersion": manifest["version"],
        "quick": args.quick,
        "gates": results,
        "terms": terms,
        "done": done,
        "commit": sh(["git", "rev-parse", "HEAD"], ROOT)[1].strip(),
        "dirty": bool(sh(["git", "status", "--porcelain"], ROOT)[1].strip()),
    }
    out_path = RECEIPTS / "latest.json"
    out_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  receipt: {out_path.relative_to(ROOT)}")

    # Exit non-zero on RED. INCONCLUSIVE/NOT_RUN block DONE but do not fail the
    # command — otherwise `Environment` (undecidable here) would make every run
    # red forever and the signal would be ignored.
    return 1 if any(r["verdict"] == RED for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
