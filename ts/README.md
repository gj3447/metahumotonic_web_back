# metahumotonic-web-back — Effect-TS port

A functional TypeScript implementation of this service, built on
[Effect](https://effect.website) and `@effect/platform`'s `HttpApi`.

**It sits beside the Python service. It does not replace it.** `app/` is
untouched and still the deployed implementation. Nothing here is wired into
`Dockerfile`, `deploy/`, `pyproject.toml`, or CI.

An audit on 2026-08-10 found ten divergences from `app/`, including two
security defects and a privacy regression. **All ten are fixed and verified**
— see *Divergences found and closed*. The wire contract now matches on every
point that was measured, but the port still covers only the stateless third of
the service, so "swap it in" remains a decision nobody has made.

```
metahumotonic_web_back/
├── app/          Python / FastAPI   ← live, unchanged
└── ts/           TypeScript / Effect ← this
```

## Run it

```bash
cd ts
npm install
npm run typecheck     # tsc --noEmit
npm test              # vitest — 127 tests, no infra required
npm run smoke         # start the real compiled server, check every route
npm run dev           # tsx watch — http://localhost:8000 (docs at /docs)
npm start             # build, then run the compiled output
```

Zero infrastructure by default: no Neo4j, no Mongo, no Redis, no Postgres. The
service degrades to the canonical snapshot exactly as the Python does.

To read the live KG:

```bash
MHB_NEO4J_LIVE=true MHB_NEO4J_PASSWORD=… npm run dev
```

Every `MHB_*` variable from `app/config.py` is honoured, with the same names and
the same defaults.

## Why there is no web framework here

`@effect/platform`'s `HttpApi` *is* the framework. `src/api/Api.ts` declares
every endpoint once, and from that single declaration you get:

- handler signatures the compiler enforces — a missing or mistyped handler is a
  build error, not a 404 someone finds in production,
- a fully typed client, derivable with `HttpApiClient`,
- the OpenAPI document served at `/docs`.

Adding Express/Fastify/NestJS underneath would add a layer that only duplicates
work the types already do. (NestJS in particular fights Effect: two competing
DI systems.) Hono is a reasonable choice if you want to migrate gradually —
route with Hono, put Effect underneath — but the endpoint is `HttpApi`.

## Layout

```
src/
├── domain/          pure — no I/O, no Effect runtime needed to test
│   ├── Contracts.ts   Schema DTOs, a 1:1 port of app/contracts.py
│   └── Errors.ts      every failure as a tagged type with its HTTP status
├── ports/           capabilities, each an interface + one or more Layers
│   ├── KgPort.ts      Neo4j reads; snapshot layer and live-Bolt layer
│   ├── KgWritePort.ts the agent's write path: dry-run first, preflight, readback
│   ├── SchemaGuard.ts the frozen SchemaRegistry, mirrored client-side
│   ├── Cypher.ts      the queries, copied verbatim from app/kg.py
│   ├── Snapshot.ts    the canonical fallback constants (measured 2026-06-08)
│   ├── FeedbackStore.ts, RateLimiter.ts, Breaker.ts, Auth.ts, Ids.ts
├── agent/           the graph substrate (see below)
│   ├── WorkGraph.ts   pure: DAG, hyperedges, topo sort, ready-set, write-set leases
│   ├── Scheduler.ts   DAG executor: dependency order, lease-aware concurrency,
│   │                  subtree-scoped failure
│   ├── Traversal.ts   the KG as the agent's move set (bounded BFS)
│   ├── Commanders.ts  measurement-driven conditional dispatch
│   └── Loop.ts        frontier convergence + loop-until-dry, over the graph
├── api/Api.ts       the whole HTTP surface, declared once
├── server/          handlers + the composition root
└── main.ts          entrypoint
```

## The agent paradigm is a graph, not a loop

The first cut of `agent/Loop.ts` was `(state: A, index: number) => A` with
`Object.is` convergence — and **nothing imported it**. Dead code cannot be
wrong, which is why it was never right. An `index: number` is a confession
that the work is one-dimensional; this system's work is not:

- APT/SP decomposes an anchor into a **DAG of spans**, not a list
- a dispatch is a **hyperedge** — one parent, N children, one fan-in
- two nodes may run together only if their **write-sets are disjoint**
  (the lesson OMD paid for)
- the seven commanders use **measurement-driven conditional dispatch**
  (canon `7cmd-…-2026-05-30`): the service graph is resolved at runtime, so an
  edge is *computed*, not imported
- convergence over a search is **frontier empty / K dry rounds**, never state
  equality — and dedup must be against everything *seen*, not everything
  *kept*, or rejected nodes reappear each round and the loop never terminates

`src/agent/` now models all of that, and it is reachable:

| endpoint | exercises |
|---|---|
| `GET /api/agent/walk` | `Traversal` — bounded BFS over the live KG |
| `GET /api/agent/plan` | `WorkGraph` — the same neighbourhood as a DAG: topo order, ready-set, frontier, cycle/dangling check |
| `GET /api/agent/explore` | `Loop` + `Scheduler` — rounds to convergence with real I/O per node |
| `POST /api/agent/record` | `KgWritePort` — the write path (see below). Gated on the **write** key |

Live run against the SYMPOSIUM KG, 2026-08-10:

```
/api/agent/walk    → 5 visited, stoppedBecause: Exhausted, unexplored: 0
/api/agent/plan    → 5 nodes, cyclic: false, dangling: 0, readySet: [seed]
/api/agent/explore → rounds 3, admitted 9, stoppedBecause: Dry, failed 0
/api/agent/explore?seed=<nonexistent> → rounds 2, admitted 1, Dry
```

Every terminal condition is named. There is no path out of the loop that means
"it just ended".

One honest gap: `Commanders.ts` is the *mechanism* for measurement-driven
dispatch, covered by tests, but it has **no HTTP surface and no real roster**.
The actual commanders (occam, 나생문, 롱기누스, …) live in `PI/bhgman_tool` in
Python. Wiring a fabricated roster to an endpoint would be theatre; the roster
belongs to whoever owns those engines.

## The write path — closing the loop

Reading the KG is half a feedback loop. `verdict → root cause → lesson → next
action` needs the last arrow to land somewhere durable, and until now the agent
could only read.

What makes this more than "run a Cypher": **the schema mirrors the database's
own triggers.** This KG runs 17 APOC triggers that reject a bad write at commit
time with a `TransactionHookFailed` blob — I hit three of them by hand while
building this. Each is now a type:

| trigger | mirrored as |
|---|---|
| `t_researchfinding_citation_required` | `CitationEvidence`, a required union — a URL, or a stated reason there isn't one |
| `t_lesson_lakatos_mechanism_required_v27` | required enum of the canonical five |
| `t_lesson_name_not_null` | `minLength(1)` + no-whitespace on `NodeName` |
| `t_schema_freeze_label_v2` | `SchemaGuard`, preflight |
| `t_schema_freeze_reltype_v1` | `SchemaGuard`, preflight |

So a violation is a 400 naming the offending field, at the request boundary,
instead of a stack trace after the transaction did its work:

```
POST /api/agent/record   {"relType": "FINDING_OF"}
→ 400 refused 1/1 statement(s): a -[:FINDING_OF]-> b —
       relationship types not in SchemaRegistry: FINDING_OF
```

(`FINDING_OF` does not exist; `HAS_FINDING` is the real one. That is the exact
mistake I made by hand earlier, now unmakeable.)

Four house rules, each of them a scar:

1. **`dryRun` defaults to true.** Touching canon is opt-in.
2. **Preflight before the transaction** against the live registry
   (3,453 labels / 4,938 relationship types, loaded once at startup).
   Fail-closed: if the registry is unreadable, nothing is admissible.
3. **Read back, always.** *A skipped readback creates a duplicate, not a
   missing node.* Anything planned but absent afterwards lands in `missing`
   and `committed` goes false.
4. **Never MERGE an endpoint into existence.** Hubs, link endpoints and
   dispatch children are all `MATCH`ed. Merging them is how orphans appear.

Deliberately **additive only** — no supersede, archive, delete, or re-label.
Retiring canon is Occam's job and needs a human verdict.

Live commit, 2026-08-10:

```
POST /api/agent/record  (dryRun:false, write key)
→ committed: true
  readback: ["rf-ts-agent-write-path-mirrors-kg-triggers-2026-08-10"]
  missing:  []
```

verified independently through a separate MCP session, not from the receipt.

## What the port changes, and why

| Python | here | why it matters |
|---|---|---|
| exceptions | the `E` channel | the compiler lists what a call can fail with |
| module-global `settings`, `kg`, `store` | services in the `R` channel | a test supplies a layer instead of monkey-patching |
| `time.monotonic()` | `Clock` | the window-expiry test advances `TestClock` instead of sleeping 60s |
| `threading.Lock` on the rate limiter | `Ref.modify` | atomic under the fiber scheduler; the lock disappears |
| `uuid4()`, `datetime.utcnow()` | the `Ids` service | ids are deterministic in tests |
| `finally: await driver.close()` | `Effect.acquireRelease` | SIGINT closes the driver through the scope that opened it |
| `try/except` around each dependency | `Breaker.guard` | one degradation policy, written once |
| `Effect.all(tasks)` fan-out | `Scheduler.run(graph)` | dependencies, write-set leases, subtree-scoped failure |

Behaviour was *intended* to be unchanged — same routes, same snapshot
magnitudes, same fail-closed rules. An audit on 2026-08-10 found that intent
was not achieved on ten points. They are listed below rather than quietly
fixed, because two of them are behaviour decisions someone has to make.

## Divergences found and closed (audited 2026-08-10)

Measured against `app/` and its 249-test suite, not against this README. Every
row was verified live against the SYMPOSIUM KG after the fix.

| # | divergence | Python contract | now |
|---|---|---|---|
| 1 | `/ready` omitted `status`/`kg_live`/`degraded` | `ops/check-web-back-live.sh:296-302` asserts five keys | all 7 keys, 503 + same body when the wiki plane is required but absent |
| 2 | auth read `Authorization` only | `app/routers/kg_proxy.py:74`, `feedback.py:85` read `x-api-key` | both accepted, `x-api-key` first |
| 3 | DELETE retained the record as spam **with the contact address** | `feedback.py:153` "Permanently erase … including an optional contact address" | erases; 404 on a non-32-hex id |
| 4 | rate limit keyed on subject text — varying it bypassed the limiter | `netutil.client_key` keys on client IP | client IP, via the trust-proxy rule |
| 5 | `trustProxy` parsed, never read | `CF-Connecting-IP`, else RIGHTMOST `X-Forwarded-For` | implemented; leftmost XFF is never trusted |
| 6 | `neo4jFallbackUris` was dead — `driver()` does not throw on a dead host | Python fails over | `verifyConnectivity()` with a 5s deadline enforced inside the promise |
| 7 | three cache settings parsed, no cache | 120s/300s TTL | bounded TTL + single-flight (`ports/Cache.ts`) |
| 8 | validation answered 400 | FastAPI answers 422 (12 Python tests assert it) | 422 everywhere |
| 9 | out-of-range pagination was clamped | `Query(20, ge=1, le=100)` rejects | rejects |
| 10 | tests shared one handler, order-dependent | — | fresh composition root per test |

Two of these needed a decision rather than a fix, and the safe reading was
taken: **#3 erases** (the Python's documented intent; retention would have been
a silent privacy change) and **#8 is 422** (pinned by twelve Python tests).

### How 422 is done, and why it looks odd

`HttpApiDecodeError` is a built-in fixed at 400, and a response-rewriting
middleware does not work — verified: `setStatus` produced 422 and the client
still received 400, because the framework re-serialises after the middleware
returns. So each endpoint declares the **wire shape** (field names and base
types, which is what OpenAPI needs) and the **constraints** run inside the
handler via `domain/Validation.ts`, failing with a 422 error type. The
constraints are not duplicated: the strict schemas in `Contracts.ts` are built
by piping filters onto the same base.

### One trap worth writing down

`Layer.provide(OperatorPlaneNoStore)` on `HttpApiBuilder.serve` type-checks,
starts cleanly, and **silently drops every prefixed route** — `/health`
answered 200 while `/api/stats` 404'd. It has to be provided to
`HttpApiBuilder.api` instead. All 117 tests stayed green through this, because
they build their own composition root. That is why `scripts/smoke.sh` exists
and runs in CI: it starts the real compiled server and checks every route.

## The agent paradigm is a graph, not a loop

The first cut of `agent/Loop.ts` was `(state: A, index: number) => A` with
`Object.is` convergence — and **nothing imported it**. Dead code cannot be
wrong, which is why it was never right. An `index: number` is a confession
that the work is one-dimensional; this system's work is not:

- APT/SP decomposes an anchor into a **DAG of spans**, not a list
- a dispatch is a **hyperedge** — one parent, N children, one fan-in
- two nodes may run together only if their **write-sets are disjoint**
  (the lesson OMD paid for)
- the seven commanders use **measurement-driven conditional dispatch**
  (canon `7cmd-…-2026-05-30`): the service graph is resolved at runtime, so an
  edge is *computed*, not imported
- convergence over a search is **frontier empty / K dry rounds**, never state
  equality — and dedup must be against everything *seen*, not everything
  *kept*, or rejected nodes reappear each round and the loop never terminates

`src/agent/` now models all of that, and it is reachable:

| endpoint | exercises |
|---|---|
| `GET /api/agent/walk` | `Traversal` — bounded BFS over the live KG |
| `GET /api/agent/plan` | `WorkGraph` — the same neighbourhood as a DAG: topo order, ready-set, frontier, cycle/dangling check |
| `GET /api/agent/explore` | `Loop` + `Scheduler` — rounds to convergence with real I/O per node |
| `POST /api/agent/record` | `KgWritePort` — the write path (see below). Gated on the **write** key |

Live run against the SYMPOSIUM KG, 2026-08-10:

```
/api/agent/walk    → 5 visited, stoppedBecause: Exhausted, unexplored: 0
/api/agent/plan    → 5 nodes, cyclic: false, dangling: 0, readySet: [seed]
/api/agent/explore → rounds 3, admitted 9, stoppedBecause: Dry, failed 0
/api/agent/explore?seed=<nonexistent> → rounds 2, admitted 1, Dry
```

Every terminal condition is named. There is no path out of the loop that means
"it just ended".

One honest gap: `Commanders.ts` is the *mechanism* for measurement-driven
dispatch, covered by tests, but it has **no HTTP surface and no real roster**.
The actual commanders (occam, 나생문, 롱기누스, …) live in `PI/bhgman_tool` in
Python. Wiring a fabricated roster to an endpoint would be theatre; the roster
belongs to whoever owns those engines.

## The write path — closing the loop

Reading the KG is half a feedback loop. `verdict → root cause → lesson → next
action` needs the last arrow to land somewhere durable, and until now the agent
could only read.

What makes this more than "run a Cypher": **the schema mirrors the database's
own triggers.** This KG runs 17 APOC triggers that reject a bad write at commit
time with a `TransactionHookFailed` blob — I hit three of them by hand while
building this. Each is now a type:

| trigger | mirrored as |
|---|---|
| `t_researchfinding_citation_required` | `CitationEvidence`, a required union — a URL, or a stated reason there isn't one |
| `t_lesson_lakatos_mechanism_required_v27` | required enum of the canonical five |
| `t_lesson_name_not_null` | `minLength(1)` + no-whitespace on `NodeName` |
| `t_schema_freeze_label_v2` | `SchemaGuard`, preflight |
| `t_schema_freeze_reltype_v1` | `SchemaGuard`, preflight |

So a violation is a 400 naming the offending field, at the request boundary,
instead of a stack trace after the transaction did its work:

```
POST /api/agent/record   {"relType": "FINDING_OF"}
→ 400 refused 1/1 statement(s): a -[:FINDING_OF]-> b —
       relationship types not in SchemaRegistry: FINDING_OF
```

(`FINDING_OF` does not exist; `HAS_FINDING` is the real one. That is the exact
mistake I made by hand earlier, now unmakeable.)

Four house rules, each of them a scar:

1. **`dryRun` defaults to true.** Touching canon is opt-in.
2. **Preflight before the transaction** against the live registry
   (3,453 labels / 4,938 relationship types, loaded once at startup).
   Fail-closed: if the registry is unreadable, nothing is admissible.
3. **Read back, always.** *A skipped readback creates a duplicate, not a
   missing node.* Anything planned but absent afterwards lands in `missing`
   and `committed` goes false.
4. **Never MERGE an endpoint into existence.** Hubs, link endpoints and
   dispatch children are all `MATCH`ed. Merging them is how orphans appear.

Deliberately **additive only** — no supersede, archive, delete, or re-label.
Retiring canon is Occam's job and needs a human verdict.

Live commit, 2026-08-10:

```
POST /api/agent/record  (dryRun:false, write key)
→ committed: true
  readback: ["rf-ts-agent-write-path-mirrors-kg-triggers-2026-08-10"]
  missing:  []
```

verified independently through a separate MCP session, not from the receipt.

## What the port changes, and why

| Python | here | why it matters |
|---|---|---|
| exceptions | the `E` channel | the compiler lists what a call can fail with |
| module-global `settings`, `kg`, `store` | services in the `R` channel | a test supplies a layer instead of monkey-patching |
| `time.monotonic()` | `Clock` | the window-expiry test advances `TestClock` instead of sleeping 60s |
| `threading.Lock` on the rate limiter | `Ref.modify` | atomic under the fiber scheduler; the lock disappears |
| `uuid4()`, `datetime.utcnow()` | the `Ids` service | ids are deterministic in tests |
| `finally: await driver.close()` | `Effect.acquireRelease` | SIGINT closes the driver through the scope that opened it |
| `try/except` around each dependency | `Breaker.guard` | one degradation policy, written once |
| `Effect.all(tasks)` fan-out | `Scheduler.run(graph)` | dependencies, write-set leases, subtree-scoped failure |

Behaviour was *intended* to be unchanged — same routes, same snapshot
magnitudes, same fail-closed rules. An audit on 2026-08-10 found that intent
was not achieved on ten points. They are listed below rather than quietly
fixed, because two of them are behaviour decisions someone has to make.

## Known divergences from the Python service (audited 2026-08-10)

Measured against `app/` and its 249-test suite, not against this README.

| # | divergence | evidence | severity |
|---|---|---|---|
| 1 | `/ready` omits `status`, `kg_live`, `degraded` | `ops/check-web-back-live.sh:298,301` asserts `kg_live is True` and `degraded is False`; `app/routers/meta.py:66-73` emits them | **blocks deployment** |
| 2 | Cypher proxy auth header is `Authorization`; Python uses `X-API-Key` | `app/routers/kg_proxy.py:49,74` | **breaks clients** |
| 3 | `DELETE /internal/feedback/:id` retains the record as spam; Python *erases* it and the contact address | `app/routers/feedback.py:151-164` | **privacy regression** |
| 4 | Feedback rate limit keys on subject text, not client IP | `src/server/Handlers.ts:223` | **security** |
| 5 | `trustProxy` is parsed and never used — no forwarded-IP handling | `src/Config.ts:184`, no reader | **security** |
| 6 | `neo4jFallbackUris` is dead: `neo4j.driver()` does not throw on an unreachable host, so `Effect.firstSuccessOf` always takes `uris[0]` | verified empirically | availability |
| 7 | No cache layer, though three TTL settings are parsed | `src/Config.ts:152-154`; no `ports/Cache.ts` | load regression |
| 9 | Out-of-range pagination is clamped; Python rejects it | `Schema.clamp` in `src/api/Api.ts` | contract |
| 10 | Tests share one handler with no per-test reset | `test/http.test.ts:98` | test hygiene |

Items 3 and 8 are decisions, not bugs — someone has to choose. The rest are
defects.

## Verified

Typecheck clean, 127/127 tests green, and driven live against the SYMPOSIUM KG on
2026-08-10:

- snapshot mode — `/health` `/ready` `/` `/api/stats` `/api/domains`
  `/api/skills`, the seven `/api/research/*` surfaces, `/docs`
- live Bolt — `/api/stats` returned 118,217 nodes and
  `/api/research/summary` returned `source: "live"` with 15,584 findings
- `/api/research/{lessons,papers,consensus,recent,neighbors}` returned real
  rows; `neighbors` correctly reported `degree 7, returned 3, truncated true`
- feedback accept / honeypot / 429-with-retry-hint, operator inbox
  401→200→triage→404, KG proxy 503-when-disabled, and a write rejected inside
  a READ transaction by the server itself (502 `KgQueryFailed`)
- the four `/api/agent/*` endpoints against real nodes, including a real
  KG commit with readback (see above)

## Two defects found while porting

**1. JSON integers reached Cypher as floats.** `{"limit": 100}` was sent as
`100.0` and Neo4j rejected it with `LIMIT: '100.0' is not a valid value`. Python
never hit this because `int` maps to a Cypher Integer on its own.
Fixed in `coerceParams` (`src/ports/KgPort.ts`), with a regression test.
**This bug is unique to the TS port** — the Python proxy is unaffected.

**2. `/api/research/findings` is silently empty against the live KG — in both
implementations.** `_FINDINGS_CYPHER` calls `toString(n.created_at)`, and at
least one `ResearchFinding` stores `created_at` as a *list*:

```
Invalid input for function 'toString()': Expected a String, Float, Integer,
Boolean, Temporal or Duration, got: StringArray[2026-05-14T16:00:00…, …]
```

`KG._run` catches `Neo4jError` and returns `None`, so `get_findings` returns
`[]`. The endpoint answers 200 with an empty list and nothing is logged. This
port reproduces the same `[]` (deliberately — same behaviour), but now emits a
`WARN` naming the cause. **Not fixed here**: the fix is either a data cleanup or
a Cypher change, and both belong to the Python service that owns the endpoint.

## Tooling

`effect-mcp` (by `tim-smart`, the #1 contributor to Effect-TS/effect) is
registered in `CD/.mcp.json` as `effect-docs`. Two tools —
`effect_docs_search` and `get_effect_doc` — so an agent editing this tree can
read current Effect documentation instead of recalling it. Note the repo was
last pushed 2026-02, so treat it as a docs index, not a version oracle.

## Not ported

Roughly 45% of `app/` is out of scope for this pass, and the service is honest
about it — `/ready` reports the wiki plane as read-only rather than pretending.

- **the community wiki** (`app/routers/wiki.py` 1060 lines, `app/wiki/*` ~1900)
  — sessions, revisions, diffs, moderation, quarantine, PostgreSQL store
- **the MCP registry** (`app/routers/mcp_registry.py`, `mcp_store`, `mcp_vault`,
  `mcp_manifest`) and `/.well-known/mcp-servers.json`
- **live drivers** for Mongo (feedback durability), Redis (distributed rate
  limiting) and PostgreSQL — the ports and their fail-closed semantics exist;
  only the in-memory implementations are wired
- Turnstile token *verification* (the required-token check is here; the HTTP
  call to Cloudflare is not), Prometheus `/metrics`, and the CLIs
  (`mhb-mcp`, `mhb-wiki`, `mhb-wiki-mcp`)

Each has a port interface already, so adding a layer is additive rather than a
refactor.
