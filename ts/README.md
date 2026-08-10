# metahumotonic-web-back — Effect-TS port

A functional TypeScript implementation of this service, built on
[Effect](https://effect.website) and `@effect/platform`'s `HttpApi`.

**It sits beside the Python service. It does not replace it.** `app/` is
untouched and still the deployed implementation. Nothing here is wired into
`Dockerfile`, `deploy/`, `pyproject.toml`, or CI. Both trees can be developed
and run independently; the TS one binds the same routes so it can be put behind
the same ingress when — and only when — someone decides to.

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
npm test              # vitest — 84 tests, no infra required
npm run dev           # http://localhost:8000  (docs at /docs)
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

Behaviour is deliberately *unchanged*: same routes, same status codes, same
snapshot magnitudes, same fail-closed rules (an unset key disables a surface
with 503 rather than opening it).

## Verified

Typecheck clean, 84/84 tests green, and driven live against the SYMPOSIUM KG on
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
- the three `/api/agent/*` endpoints against real nodes (see above)

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
