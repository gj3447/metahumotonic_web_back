# harness/ — the executable spec and the grader

> Implements `THEORY/함수형프로그래밍/AGENT_PARADIGM_SPEC_v1.md` §4–§7 for this
> repository. That spec's own status is **PROPOSED / 무측정 (HC4)** — nothing
> here claims the paradigm is *better*, only that this repo now obeys it.

```bash
./verify check     # the fast loop: typecheck + unit. ~3s (PROMPT P#5 budget)
./verify           # every gate, folded into DONE
./verify --only ts-smoke
./verify --autonomous   # refuses to start without harness/BUDGET.md (B1)
```

## Why this is not just `pytest && npm test`

### 1. Four-valued gates (§4-5)

```text
GREEN         the property holds
RED           the property is violated
INCONCLUSIVE  we could not tell — timeout, missing tool, non-monitorable
NOT_RUN       no attempt was made
```

`INCONCLUSIVE` and `NOT_RUN` **block DONE**. They are not soft passes.

The reason is Bauer–Leucker–Schallhart's monitorability result, quoted in the
spec: a liveness property cannot be decided from a finite trace, so a binary
GREEN/RED gate turns "undecidable here" into a **false GREEN**.

The live example is `target-environment`. This service ships to a docker
runtime on VM100 behind traefik; nothing in this checkout can decide whether
that environment is healthy. So the gate reports `NOT_RUN` and DONE is `NO` —
which is the truth, because the TypeScript tree has never been deployed. A
harness that answered GREEN there would be lying in the most expensive possible
place. The real check exists and is not this: `ops/check-web-back-live.sh`.

### 2. DONE is a conjunction over terms (§5)

```text
DONE(F) = Pure ∧ Reachable ∧ Contract ∧ Executable ∧ Environment ∧ Regression

파일 생성됨  ≠  단위 테스트 통과  ≠  하네스 통과  ≠  DONE
```

A term is as bad as its worst gate, and "we could not tell" ranks worse than
"it passed".

| term | gates | asks |
|---|---|---|
| **Pure** | `ts-typecheck` `ts-unit` `ts-property` | does the calculation hold up |
| **Reachable** | `wiring-orphans` `wiring-shared-composition` | is it wired into the real entrypoint |
| **Contract** | `py-operations-contract` `ts-wire-compat` | do producer and consumer agree |
| **Executable** | `ts-smoke` | does the real binary serve it |
| **Environment** | `target-environment` | does it hold where it ships |
| **Regression** | `py-full-suite` | is everything that worked still working |

### 3. The two wiring gates are the point (§4-3, §4-4)

Both exist because the corresponding defect actually happened here on
2026-08-10, and 117 passing tests said nothing about either.

**`wiring-orphans`** — `orphan_module(M)`: a module unreachable from
`src/main.ts` is treated as nonexistent. It caught `agent/Commanders.ts` on its
first run (161 lines, 6 tests, zero production call sites — deleted). It would
have caught `agent/Loop.ts` the day it was written.

Reachability is computed from the **production** entrypoint only. A module
reached only from a test is still an orphan; counting test imports would hide
exactly the defect being hunted.

**`wiring-shared-composition`** — §4-3: production and harness must consume the
same composition graph. Before `src/server/Composition.ts` existed, the test
file assembled its own app and shared two layers with production out of eleven.
A middleware provided to `HttpApiBuilder.serve` instead of `HttpApiBuilder.api`
then 404'd every prefixed route in production while every test stayed green.

Unifying them immediately found a second thing: one test asserted a `/ready`
503 under a config the real service **refuses to start on**. It was testing a
state production can never reach. That is what §4-3 buys.

Both gates are fire-tested — an injected violation makes them fail — and
`shared_composition` strips comments before matching, because its first version
flagged a *comment* that explained the rule it was enforcing.

### 4. Evidence is machine-generated (§6)

`harness/_receipts/latest.json` holds per-gate verdicts, exit codes, durations,
command lines, output digests, the commit and whether the tree was dirty.
Prose is not evidence.

## Layout

```
harness/
├── BUDGET.md              B1 — the declared budget. --autonomous refuses without it
├── gates/
│   ├── manifest.json      the gate list and which DONE term each feeds
│   ├── orphan_module.py   §4-4
│   └── shared_composition.py  §4-3
├── runner/verify.py       runs gates, folds into DONE, writes the receipt
└── _receipts/latest.json  machine-generated evidence
```

## Authority asymmetry (§7)

The agent may add scenarios, contracts and implementations freely. Changing an
existing answer condition — removing a gate, relaxing a timeout, editing an
expected output — needs separate approval.

> 없으면 AI 는 통과시키려고 채점기를 고친다.

This is currently **prose, not a gate** (`B5` says that means it is not yet
enforced). Making it a write-set deny in the autonomous runner is open work.

## What this harness does NOT claim

- It does not claim the paradigm makes development faster or better. SPEC §9:
  every existing observation of that is confounded, and the 4-arm experiment
  (`EXPERIMENT_4ARM_PREREG_v1.md`) is **preregistered and unexecuted**.
- A GREEN run means the gates that exist passed. It does not mean the gate set
  is complete.
- `Environment` has never been GREEN here, and will not be until something
  actually deploys the TypeScript tree and runs `check-web-back-live.sh`
  against it.
