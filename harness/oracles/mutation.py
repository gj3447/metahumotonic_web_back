#!/usr/bin/env python3
"""Mutation oracle — does the property suite actually catch anything?

AGENT_PARADIGM_SPEC_v1 §4-1: "Oracle 이 자산이다. 테스트 *개수*가 아니라 판정
기준의 *질*이다." A green property suite proves nothing on its own; this is how
we find out whether the laws bite.

Each mutant is a small, plausible bug. Every one must be CAUGHT. A SURVIVOR
means either the law is vacuous or the implementation has redundancy no test
can distinguish — both are findings.

It found both on 2026-08-10. Eight mutants died immediately; the ninth survived
because `Cache` had TWO single-flight paths and deleting either alone left the
guarantee standing. Two adversarial LLM reviewers had judged all 67 laws
non-vacuous and missed it. The fix was to the implementation, not the test:
the redundant fast path is gone and the mutant is now lethal.

Slow (one full property run per mutant), so it is not in the default gate set.
Run it when the property suite changes:

    python3 harness/oracles/mutation.py
"""
import subprocess, sys
from pathlib import Path
import os

os.chdir(Path(__file__).resolve().parents[2] / "ts")


# (label, file, find, replace, the law it should break)
MUTANTS = [
 ("lease check removed", "src/agent/WorkGraph.ts",
  "    if (node.writeSet.some((r) => held.has(r))) continue",
  "    // MUTANT: lease gate removed",
  "readySet never returns two nodes with intersecting write-sets"),

 ("descendants includes self", "src/agent/WorkGraph.ts",
  "  const stack = [...(dependents.get(id) ?? [])]",
  "  const stack = [id, ...(dependents.get(id) ?? [])]",
  "descendants is irreflexive"),

 ("single-flight removed", "src/ports/Cache.ts",
  """        if (!claimed) {
          const other = (yield* Ref.get(state)).inFlight.get(key)
          return other === undefined ? yield* produce : yield* Deferred.await(other)
        }""",
  "        void claimed // MUTANT: no join, every caller produces",
  "N concurrent cold misses run the producer once"),

 ("cache unbounded", "src/ports/Cache.ts",
  "        if (entries.size > options.maxEntries) {",
  "        if (false) {",
  "size never exceeds maxEntries"),

 ("XFF leftmost (spoofable)", "src/ports/ClientIp.ts",
  "      const last = parts[parts.length - 1]?.trim()",
  "      const last = parts[0]?.trim()",
  "the RIGHTMOST X-Forwarded-For entry is used"),

 ("trustProxy ignored", "src/ports/ClientIp.ts",
  "  if (input.trustProxy) {",
  "  if (true) {",
  "with trustProxy false, headers are ignored"),

 ("ints not coerced", "src/ports/KgPort.ts",
  "    return Number.isInteger(value) ? neo4j.int(value) : value",
  "    return value",
  "integral numbers become Cypher Integers"),

 ("limiter off-by-one", "src/ports/RateLimiter.ts",
  "            if (kept.length >= policy.maxEvents) {",
  "            if (kept.length > policy.maxEvents) {",
  "exactly maxEvents allows per window"),

 ("dispatch children MERGEd", "src/ports/KgWritePort.ts",
  "                 MATCH (c {name: childName})",
  "                 MERGE (c {name: childName})",
  "endpoints are MATCHed, never MERGEd"),
]

results = []
for label, rel, find, repl, law in MUTANTS:
    p = Path(rel); original = p.read_text(encoding="utf-8")
    if find not in original:
        results.append((label, "SKIP", "anchor not found", law)); continue
    p.write_text(original.replace(find, repl, 1), encoding="utf-8")
    try:
        r = subprocess.run(["npx","vitest","run","test/property.test.ts","--reporter=dot"],
                           capture_output=True, text=True, timeout=600)
        caught = r.returncode != 0
    except subprocess.TimeoutExpired:
        caught = False; r = None
    finally:
        p.write_text(original, encoding="utf-8")
    results.append((label, "CAUGHT" if caught else "SURVIVED", "", law))
    print(f"  {'CAUGHT  ' if caught else 'SURVIVED'} {label:<28} — {law}")

survived = [x for x in results if x[1] == "SURVIVED"]
skipped  = [x for x in results if x[1] == "SKIP"]
print(f"\n  {len(results)-len(survived)-len(skipped)}/{len(results)} mutants caught, "
      f"{len(survived)} survived, {len(skipped)} skipped")
for s in survived: print(f"    SURVIVED: {s[0]} — the law '{s[3]}' did not catch it")
for s in skipped:  print(f"    SKIPPED:  {s[0]} — {s[2]}")
sys.exit(1 if survived else 0)
