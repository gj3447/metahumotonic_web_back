/**
 * Property tests — PROMPT O ("법칙이 오라클").
 *
 * AGENT_PARADIGM_SPEC_v1 §4-1 sets the bar these have to clear:
 *
 *   "Oracle 이 자산이다. 테스트 *개수*가 아니라 판정 기준의 *질*이다.
 *    `assert impl(x) == impl(x)` 류는 100개여도 0이다."
 *
 * So every block below states a LAW — something that could be false
 * independently of how the code happens to be written today: an invariant, a
 * round-trip, a metamorphic relation, or agreement with a separately-written
 * model. Where a model is used it is deliberately a DIFFERENT algorithm from
 * the implementation (naive fixpoint vs DFS, for instance), because a model
 * that mirrors the code is the vacuous case in disguise.
 *
 * Generated 2026-08-10 by a fan-out of 8 module agents, judged by two
 * adversarial reviewers (vacuity lens, mutation lens), then mutation-tested
 * against deliberately broken implementations before being committed.
 */

import fc from "fast-check"
import * as Cache from "../src/ports/Cache.js"
import * as G from "../src/agent/WorkGraph.js"
import * as Traversal from "../src/agent/Traversal.js"
import neo4j from "neo4j-driver"
import {
  ConsensusRecord,
  FeedbackRequest,
  FindingRecord,
  FindingsParamsStrict,
  GraphNeighbor,
  LessonRecord,
  ListParamsStrict,
  NeighborsParamsStrict,
  NodeNeighbors
} from "../src/domain/Contracts.js"
import { RateLimited, Unavailable } from "../src/domain/Errors.js"
import {
  LinkNodes,
  WriteBatch,
  type CitationEvidence,
  type Provenance,
  type WriteIntent
} from "../src/domain/WriteIntent.js"
import { ClientIpFixed, ClientIpTag, resolve } from "../src/ports/ClientIp.js"
import {
  KgPortTag,
  coerceParams,
  decodeNeighbors,
  mergeRecent,
  notFoundNeighbors,
  type KgPort
} from "../src/ports/KgPort.js"
import { plan } from "../src/ports/KgWritePort.js"
import {
  denyAll,
  enforce,
  makeInProcess,
  type Decision,
  type LimiterPolicy
} from "../src/ports/RateLimiter.js"
import { HttpServerRequest } from "@effect/platform"
import { Effect, Either, Exit, Layer, Ref, Schema, TestClock, TestContext } from "effect"
import { describe, expect, it } from "vitest"

describe("WorkGraph — property laws", () => {
  // -------------------------------------------------------------------------
  // generators + a hand-written model (the oracle)
  // -------------------------------------------------------------------------

  const RESOURCES = ["r0", "r1", "r2", "r3"]
  const nid = (i: number): string => `n${i}`
  const range = (k: number): Array<number> => Array.from({ length: k }, (_, i) => i)

  interface Spec {
    readonly n: number
    readonly deps: ReadonlyArray<ReadonlyArray<number>>
    readonly writes: ReadonlyArray<ReadonlyArray<string>>
  }

  const build = (spec: Spec): G.WorkGraph<number> =>
    G.fromNodes(
      range(spec.n).map((i) => ({
        id: nid(i),
        payload: i,
        dependsOn: (spec.deps[i] ?? []).map(nid),
        writeSet: spec.writes[i] ?? []
      }))
    )

  const lowerIdx = (i: number): fc.Arbitrary<Array<number>> =>
    i === 0 ? fc.constant<Array<number>>([]) : fc.subarray(range(i))

  /** DAG by construction: node `i` may only depend on nodes `j < i`. */
  const dagArb = (minN = 1, maxN = 8, chain = false): fc.Arbitrary<Spec> =>
    fc.integer({ min: minN, max: maxN }).chain((n) =>
      fc
        .tuple(
          fc.tuple(...range(n).map(lowerIdx)),
          fc.tuple(...range(n).map(() => fc.subarray(RESOURCES)))
        )
        .map(([deps, writes]): Spec => ({
          n,
          // `chain` forces the spine i-1 -> i so every j > i is reachable from i,
          // which is what lets the cyclic generator close a guaranteed cycle.
          deps: chain
            ? deps.map((d, i) => (i === 0 ? d : Array.from(new Set<number>([...d, i - 1]))))
            : deps,
          writes
        }))
    )

  /**
   * Reverse reachability by naive fixpoint — deliberately a different algorithm
   * from the DFS in `descendants`, so it can serve as an oracle.
   */
  const modelDescendants = (spec: Spec, from: number): Set<number> => {
    const out = new Set<number>()
    let changed = true
    while (changed) {
      changed = false
      for (const j of range(spec.n)) {
        if (out.has(j)) continue
        if ((spec.deps[j] ?? []).some((d) => d === from || out.has(d))) {
          out.add(j)
          changed = true
        }
      }
    }
    return out
  }

  type Status = "none" | "done" | "running" | "blocked"

  const withStatuses = (spec: Spec): fc.Arbitrary<{ spec: Spec; st: Array<Status> }> =>
    fc
      .tuple(
        ...range(spec.n).map(() =>
          fc.constantFrom<Status>("none", "none", "done", "done", "running", "blocked")
        )
      )
      .map((st) => ({ spec, st: st as Array<Status> }))

  const toRunState = (st: ReadonlyArray<Status>): G.RunState => ({
    done: new Set(st.flatMap((s, i) => (s === "done" ? [nid(i)] : []))),
    running: new Set(st.flatMap((s, i) => (s === "running" ? [nid(i)] : []))),
    blocked: new Set(st.flatMap((s, i) => (s === "blocked" ? [nid(i)] : [])))
  })

  const dagWithState = dagArb().chain(withStatuses)

  // -------------------------------------------------------------------------
  // L1 — topoSort of an acyclic graph is a dependency-respecting permutation
  // -------------------------------------------------------------------------

  it("L1: topoSort of a DAG is a permutation placing every node after its dependencies", () => {
    fc.assert(
      fc.property(dagArb(), (spec) => {
        const g = build(spec)
        const r = G.topoSort(g)
        expect(r._tag).toBe("Ordered")
        if (r._tag !== "Ordered") return

        // permutation of exactly the node ids, no repeats, no invention
        expect([...r.order].sort()).toEqual(range(spec.n).map(nid).sort())

        const pos = new Map(r.order.map((id, i) => [id, i] as const))
        for (const i of range(spec.n)) {
          for (const d of spec.deps[i] ?? []) {
            expect(pos.get(nid(d))!).toBeLessThan(pos.get(nid(i))!)
          }
        }
      })
    )
  })

  // -------------------------------------------------------------------------
  // L2 — topoSort is exactly a cycle detector
  // -------------------------------------------------------------------------

  it("L2: closing any back-edge yields Cyclic, and every involved node is held by another involved node", () => {
    const cyclicArb = dagArb(2, 8, true).chain((spec) =>
      fc.integer({ min: 0, max: spec.n - 2 }).chain((i) =>
        fc.integer({ min: i + 1, max: spec.n - 1 }).map((j) => ({
          spec: {
            ...spec,
            deps: spec.deps.map((d, k) => (k === i ? Array.from(new Set<number>([...d, j])) : d))
          },
          i,
          j
        }))
      )
    )

    fc.assert(
      fc.property(cyclicArb, ({ spec, i, j }) => {
        const g = build(spec)
        const r = G.topoSort(g)
        expect(r._tag).toBe("Cyclic")
        if (r._tag !== "Cyclic") return

        const involved = new Set(r.involved)
        // both endpoints of the injected back-edge sit on the cycle
        expect(involved.has(nid(i))).toBe(true)
        expect(involved.has(nid(j))).toBe(true)
        // the leftover set is closed: nothing in it could have been scheduled,
        // i.e. each member still waits on an in-graph dependency inside the set
        for (const id of involved) {
          const node = g.nodes.get(id)!
          expect(node.dependsOn.some((d) => involved.has(d))).toBe(true)
        }
        // and it never swallows a node that was validly ordered
        expect(involved.size).toBeLessThanOrEqual(spec.n)
      })
    )
  })

  // -------------------------------------------------------------------------
  // L3 — dangling edges are reported exactly and ignored by ordering
  // -------------------------------------------------------------------------

  it("L3: edges to absent nodes are reported exactly and change no topological order", () => {
    const danglingArb = dagArb().chain((spec) =>
      fc
        .tuple(
          fc.integer({ min: 0, max: spec.n - 1 }),
          fc.uniqueArray(fc.integer({ min: 0, max: 4 }), { minLength: 1, maxLength: 3 })
        )
        .map(([i, ghosts]) => ({ spec, i, ghosts: ghosts.map((g) => `ghost${g}`) }))
    )

    fc.assert(
      fc.property(danglingArb, ({ spec, i, ghosts }) => {
        const clean = build(spec)
        const target = clean.nodes.get(nid(i))!
        const dirty = G.addNode(clean, { ...target, dependsOn: [...target.dependsOn, ...ghosts] })

        expect(G.danglingEdges(clean)).toEqual([])
        expect(G.danglingEdges(dirty).map((e) => `${e.from}->${e.to}`).sort()).toEqual(
          ghosts.map((g) => `${nid(i)}->${g}`).sort()
        )

        const a = G.topoSort(clean)
        const b = G.topoSort(dirty)
        expect(b._tag).toBe("Ordered")
        if (a._tag === "Ordered" && b._tag === "Ordered") expect(b.order).toEqual(a.order)
      })
    )
  })

  // -------------------------------------------------------------------------
  // L4 — descendants agrees with the model, is irreflexive and transitively closed
  // -------------------------------------------------------------------------

  it("L4: descendants equals reverse reachability, excludes the node, and is transitively closed", () => {
    fc.assert(
      fc.property(dagArb(), (spec) => {
        const g = build(spec)
        for (const i of range(spec.n)) {
          const got = G.descendants(g, nid(i))
          const want = modelDescendants(spec, i)
          expect([...got].sort()).toEqual([...want].map(nid).sort())
          expect(got.has(nid(i))).toBe(false)
          for (const d of got) {
            for (const dd of G.descendants(g, d)) expect(got.has(dd)).toBe(true)
          }
        }
      })
    )
  })

  // -------------------------------------------------------------------------
  // L5 — readySet safety
  // -------------------------------------------------------------------------

  it("L5: readySet only yields unresolved nodes whose deps are done, with leases free and pairwise disjoint", () => {
    fc.assert(
      fc.property(dagWithState, ({ spec, st }) => {
        const g = build(spec)
        const state = toRunState(st)
        const ready = G.readySet(g, state)

        expect([...ready].sort()).toEqual([...ready])
        expect(new Set(ready).size).toBe(ready.length)

        const held = new Set<string>()
        for (const id of state.running) {
          for (const r of g.nodes.get(id)!.writeSet) held.add(r)
        }

        const claimed = new Set<string>()
        for (const id of ready) {
          expect(state.done.has(id)).toBe(false)
          expect(state.running.has(id)).toBe(false)
          expect(state.blocked.has(id)).toBe(false)

          const node = g.nodes.get(id)!
          for (const d of node.dependsOn) expect(state.done.has(d)).toBe(true)
          for (const r of node.writeSet) {
            expect(held.has(r)).toBe(false) // gate 2: no running lease
            expect(claimed.has(r)).toBe(false) // gate 2': batch-internal disjointness
            claimed.add(r)
          }
        }
      })
    )
  })

  // -------------------------------------------------------------------------
  // L6 — an empty write-set is never withheld by a lease
  // -------------------------------------------------------------------------

  it("L6: a read-only node whose dependencies are done is always ready, whatever is running", () => {
    fc.assert(
      fc.property(dagWithState, ({ spec, st }) => {
        const g = build(spec)
        const state = toRunState(st)
        const ready = new Set(G.readySet(g, state))

        for (const i of range(spec.n)) {
          const node = g.nodes.get(nid(i))!
          const unresolved =
            !state.done.has(node.id) && !state.running.has(node.id) && !state.blocked.has(node.id)
          const depsDone = node.dependsOn.every((d) => state.done.has(d))
          if (node.writeSet.length === 0 && unresolved && depsDone) {
            expect(ready.has(node.id)).toBe(true)
          }
        }
      })
    )
  })

  // -------------------------------------------------------------------------
  // L7 — liveness: a failure-free DAG always drains, and the frontier only shrinks
  // -------------------------------------------------------------------------

  it("L7: scheduling by readySet drains any DAG without deadlock, frontier monotonically shrinking", () => {
    fc.assert(
      fc.property(dagArb(), (spec) => {
        const g = build(spec)
        const done = new Set<string>()
        const running = new Set<string>()
        const blocked = new Set<string>()
        let prevFrontier = G.frontier(g, { done, running, blocked })
        let steps = 0

        while (done.size < spec.n) {
          steps += 1
          expect(steps).toBeLessThanOrEqual(4 * spec.n + 4)

          const state: G.RunState = { done, running, blocked }
          for (const id of G.readySet(g, state)) running.add(id)
          // no deadlock: something is either startable or already in flight
          expect(running.size).toBeGreaterThan(0)

          const finish = [...running][0]!
          running.delete(finish)
          done.add(finish)

          const next = G.frontier(g, { done, running, blocked })
          expect(next.length).toBe(prevFrontier.length - 1)
          expect(next.every((id) => prevFrontier.includes(id))).toBe(true)
          prevFrontier = next
        }

        expect(G.frontier(g, { done, running, blocked })).toEqual([])
        expect(G.isSettled(g, { done, running, blocked })).toBe(true)
      })
    )
  })

  // -------------------------------------------------------------------------
  // L8 — newlyBlocked is exactly the unresolved descendants, and idempotent
  // -------------------------------------------------------------------------

  it("L8: newlyBlocked is the unresolved descendants of the failure, never the node itself, and idempotent", () => {
    fc.assert(
      fc.property(
        dagWithState.chain(({ spec, st }) =>
          fc.integer({ min: 0, max: spec.n - 1 }).map((f) => ({ spec, st, f }))
        ),
        ({ spec, st, f }) => {
          const g = build(spec)
          const state = toRunState(st)
          const failed = nid(f)
          const nb = G.newlyBlocked(g, state, failed)
          const desc = G.descendants(g, failed)

          for (const id of nb) {
            expect(desc.has(id)).toBe(true)
            expect(state.done.has(id)).toBe(false)
            expect(state.blocked.has(id)).toBe(false)
          }
          expect(nb.has(failed)).toBe(false)

          // exact: every unresolved model-descendant must be blocked, none missed
          const want = [...modelDescendants(spec, f)]
            .map(nid)
            .filter((id) => !state.done.has(id) && !state.blocked.has(id))
          expect([...nb].sort()).toEqual(want.sort())

          const after: G.RunState = { ...state, blocked: new Set([...state.blocked, ...nb]) }
          expect([...G.newlyBlocked(g, after, failed)]).toEqual([])
        }
      )
    )
  })

  // -------------------------------------------------------------------------
  // L9 — expand wires the hyperedge, preserves acyclicity, and is edge-idempotent
  // -------------------------------------------------------------------------

  it("L9: expand makes every child a descendant of its parent, preserves acyclicity, and adds the edge once", () => {
    const expandArb = dagArb().chain((spec) =>
      fc
        .tuple(fc.integer({ min: 0, max: spec.n - 1 }), fc.integer({ min: 1, max: 3 }))
        .chain(([pi, k]) =>
          fc
            .tuple(...range(k).map(() => fc.subarray(range(spec.n))))
            .map((childDeps) => ({ spec, pi, childDeps: childDeps as Array<Array<number>> }))
        )
    )

    fc.assert(
      fc.property(expandArb, ({ spec, pi, childDeps }) => {
        const g = build(spec)
        const parent = nid(pi)
        const children = childDeps.map((ds, k) => ({
          id: `c${k}`,
          payload: -1 - k,
          dependsOn: ds.map(nid),
          writeSet: [] as Array<string>
        }))

        const { graph: g2, hyperedge } = G.expand(g, parent, children, "dispatch")

        expect(hyperedge.parent).toBe(parent)
        expect(hyperedge.children).toEqual(children.map((c) => c.id))
        expect(hyperedge.id).toBe(`dispatch:${parent}:${children.length}`)

        // pre-existing nodes untouched
        for (const i of range(spec.n)) expect(g2.nodes.get(nid(i))).toBe(g.nodes.get(nid(i)))

        const desc = G.descendants(g2, parent)
        for (const c of children) {
          const wired = g2.nodes.get(c.id)!
          expect(wired.dependsOn.filter((d) => d === parent).length).toBe(1)
          expect(desc.has(c.id)).toBe(true)
        }

        const r = G.topoSort(g2)
        expect(r._tag).toBe("Ordered")
        if (r._tag === "Ordered") {
          const pos = new Map(r.order.map((id, i) => [id, i] as const))
          for (const c of children) expect(pos.get(parent)!).toBeLessThan(pos.get(c.id)!)
        }

        // re-expanding with the already-wired children must not duplicate the edge
        const rewired = children.map((c) => g2.nodes.get(c.id)!)
        const { graph: g3 } = G.expand(g2, parent, rewired, "dispatch")
        for (const c of children) {
          expect(g3.nodes.get(c.id)!.dependsOn).toEqual(g2.nodes.get(c.id)!.dependsOn)
        }
      })
    )
  })
})

describe("TtlCache — properties", () => {
  const runTest = <A, E>(e: Effect.Effect<A, E, never>) =>
    Effect.runPromise(Effect.provide(e, TestContext.TestContext) as Effect.Effect<A, E, never>)

  type Command =
    | { readonly tag: "get"; readonly key: string }
    | { readonly tag: "invalidate"; readonly key: string }
    | { readonly tag: "clear" }
    | { readonly tag: "advance"; readonly ms: number }

  const KEYS = ["a", "b", "c", "d"] as const

  const commandArb: fc.Arbitrary<Command> = fc.oneof(
    { weight: 8, arbitrary: fc.constantFrom(...KEYS).map((key): Command => ({ tag: "get", key })) },
    {
      weight: 4,
      arbitrary: fc.integer({ min: 1, max: 4000 }).map((ms): Command => ({ tag: "advance", ms }))
    },
    {
      weight: 2,
      arbitrary: fc.constantFrom(...KEYS).map((key): Command => ({ tag: "invalidate", key }))
    },
    { weight: 1, arbitrary: fc.constant<Command>({ tag: "clear" }) }
  )

  /**
   * The oracle for L7: a hand-written reference cache in plain TypeScript — no
   * Effect, no Ref, no Deferred, no Clock. It encodes what a bounded TTL cache
   * *means* (fresh iff written-at + ttl is still in the future; evict the
   * least-recently-written entry once over the cap) rather than how the port
   * happens to be built.
   */
  const referenceCache = (
    commands: ReadonlyArray<Command>,
    ttlSeconds: number,
    maxEntries: number
  ) => {
    const ttlMillis = Math.max(0, ttlSeconds) * 1000
    const entries = new Map<string, { value: number; expiresAt: number; seq: number }>()
    const reads: Array<number> = []
    const sizes: Array<number> = []
    let seq = 0
    let now = 0
    let calls = 0

    for (const c of commands) {
      if (c.tag === "get") {
        const hit = entries.get(c.key)
        if (hit !== undefined && hit.expiresAt > now) {
          reads.push(hit.value)
        } else {
          calls += 1
          entries.set(c.key, { value: calls, expiresAt: now + ttlMillis, seq })
          seq += 1
          if (entries.size > maxEntries) {
            const ordered = [...entries.entries()].sort((x, y) => x[1].seq - y[1].seq)
            const excess = entries.size - maxEntries
            for (let i = 0; i < excess; i += 1) entries.delete(ordered[i]![0])
          }
          reads.push(calls)
        }
      } else if (c.tag === "invalidate") {
        entries.delete(c.key)
      } else if (c.tag === "clear") {
        entries.clear()
      } else {
        now += c.ms
      }
      sizes.push(entries.size)
    }
    return { reads, sizes, calls }
  }

  it("L1: size never exceeds maxEntries, nor the number of distinct keys seen", async () => {
    // The cache key embeds attacker-controlled query params, so unbounded growth
    // is a memory-exhaustion vector. This must hold for EVERY prefix of the
    // request stream, not just at the end.
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: 0, max: 8 }),
        fc.integer({ min: 1, max: 3600 }),
        fc.array(fc.string({ minLength: 0, maxLength: 4 }), { minLength: 1, maxLength: 60 }),
        async (maxEntries, ttlSeconds, keys) => {
          const observed = await runTest(
            Effect.gen(function* () {
              const cache = yield* Cache.make<number>({ ttlSeconds, maxEntries })
              const sizes: Array<number> = []
              for (let i = 0; i < keys.length; i += 1) {
                yield* cache.get(keys[i]!, Effect.succeed(i))
                sizes.push(yield* cache.size)
              }
              return sizes
            })
          )
          const seen = new Set<string>()
          keys.forEach((k, i) => {
            seen.add(k)
            expect(observed[i]).toBeLessThanOrEqual(maxEntries)
            expect(observed[i]).toBeLessThanOrEqual(seen.size)
          })
        }
      ),
      { numRuns: 60 }
    )
  }, 30_000)

  it("L2: within the TTL every read returns the first value and the producer ran exactly once", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: 1, max: 600 }),
        fc.array(fc.integer({ min: 0, max: 1000 }), { minLength: 1, maxLength: 6 }),
        async (ttlSeconds, weights) => {
          // Scale the generated weights into clock steps whose SUM is strictly
          // inside the TTL window, so the precondition is true by construction.
          const budget = ttlSeconds * 1000 - 1
          const steps = weights.map((w) => Math.floor((w * budget) / (1000 * weights.length)))
          const r = await runTest(
            Effect.gen(function* () {
              const calls = yield* Ref.make(0)
              const cache = yield* Cache.make<number>({ ttlSeconds, maxEntries: 8 })
              const produce = Ref.updateAndGet(calls, (n) => n + 1)
              const first = yield* cache.get("k", produce)
              const later: Array<number> = []
              for (const step of steps) {
                if (step > 0) yield* TestClock.adjust(step)
                later.push(yield* cache.get("k", produce))
              }
              return { first, later, calls: yield* Ref.get(calls) }
            })
          )
          expect(steps.reduce((a, b) => a + b, 0)).toBeLessThan(ttlSeconds * 1000)
          expect(r.calls).toBe(1)
          expect(r.later.every((v) => v === r.first)).toBe(true)
        }
      ),
      { numRuns: 60 }
    )
  }, 30_000)

  it("L3: an entry is fresh strictly before ttl elapses and stale at or after it", async () => {
    await fc.assert(
      fc.asyncProperty(
        // Mix near-boundary elapsed times with a broad range, so the exact
        // freshness cutoff is exercised rather than sampled by luck.
        fc.integer({ min: 1, max: 100 }).chain((ttlSeconds) => {
          const ttlMillis = ttlSeconds * 1000
          return fc.tuple(
            fc.constant(ttlSeconds),
            fc.oneof(
              fc.integer({ min: ttlMillis - 5, max: ttlMillis + 5 }),
              fc.integer({ min: 0, max: 2 * ttlMillis + 1000 })
            )
          )
        }),
        async ([ttlSeconds, elapsed]) => {
          const r = await runTest(
            Effect.gen(function* () {
              const calls = yield* Ref.make(0)
              const cache = yield* Cache.make<number>({ ttlSeconds, maxEntries: 4 })
              const produce = Ref.updateAndGet(calls, (n) => n + 1)
              const before = yield* cache.get("k", produce)
              if (elapsed > 0) yield* TestClock.adjust(elapsed)
              const after = yield* cache.get("k", produce)
              return { before, after, calls: yield* Ref.get(calls) }
            })
          )
          const expired = elapsed >= ttlSeconds * 1000
          expect(r.before).toBe(1)
          expect(r.calls).toBe(expired ? 2 : 1)
          expect(r.after).toBe(expired ? 2 : 1)
        }
      ),
      { numRuns: 80 }
    )
  }, 30_000)

  it("L4: N concurrent cold gets run the producer once per distinct key, never once per caller", async () => {
    // Without single-flight the cache makes a thundering herd WORSE: every miss
    // arrives at once the moment an entry expires.
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: 1, max: 3 }),
        fc.array(fc.nat({ max: 2 }), { minLength: 2, maxLength: 14 }),
        fc.nat({ max: 3 }),
        async (keyCount, rawSlots, yields) => {
          const slots = rawSlots.map((n) => n % keyCount)
          const distinct = new Set(slots)
          const r = await runTest(
            Effect.gen(function* () {
              const calls = yield* Ref.make(0)
              const cache = yield* Cache.make<number>({ ttlSeconds: 600, maxEntries: 8 })
              const slow = Effect.gen(function* () {
                const n = yield* Ref.updateAndGet(calls, (x) => x + 1)
                // A varying number of suspension points, so the property is not
                // an artifact of one particular interleaving.
                for (let i = 0; i <= yields; i += 1) yield* Effect.yieldNow()
                return n
              })
              const results = yield* Effect.all(
                slots.map((s) =>
                  cache.get(`key-${s}`, slow).pipe(Effect.map((v) => [s, v] as const))
                ),
                { concurrency: "unbounded" }
              )
              return { results, calls: yield* Ref.get(calls), size: yield* cache.size }
            })
          )
          expect(r.calls).toBe(distinct.size)
          expect(r.size).toBe(distinct.size)

          const perKey = new Map<number, Set<number>>()
          for (const [s, v] of r.results) {
            const bucket = perKey.get(s) ?? new Set<number>()
            bucket.add(v)
            perKey.set(s, bucket)
          }
          // every caller on a key observes the same value...
          for (const bucket of perKey.values()) expect(bucket.size).toBe(1)
          // ...and no two distinct keys were served by the same producer run
          expect(new Set(r.results.map(([, v]) => v)).size).toBe(distinct.size)
        }
      ),
      { numRuns: 40 }
    )
  }, 40_000)

  it("L5: a non-positive ttl is a pass-through — every get produces, nothing is retained", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: -100, max: 0 }),
        fc.integer({ min: 1, max: 10 }),
        fc.integer({ min: 1, max: 8 }),
        async (ttlSeconds, n, maxEntries) => {
          const r = await runTest(
            Effect.gen(function* () {
              const calls = yield* Ref.make(0)
              const cache = yield* Cache.make<number>({ ttlSeconds, maxEntries })
              const produce = Ref.updateAndGet(calls, (x) => x + 1)
              const values: Array<number> = []
              for (let i = 0; i < n; i += 1) values.push(yield* cache.get("k", produce))
              return { values, calls: yield* Ref.get(calls), size: yield* cache.size }
            })
          )
          expect(r.calls).toBe(n)
          expect(r.values).toEqual(Array.from({ length: n }, (_, i) => i + 1))
          // Not merely "always a miss" — a disabled cache must retain nothing.
          expect(r.size).toBe(0)
        }
      ),
      { numRuns: 60 }
    )
  }, 30_000)

  it("L6: failures are never cached and never poison the key", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(fc.constantFrom("fail" as const, "die" as const), { minLength: 0, maxLength: 6 }),
        fc.integer({ min: 1, max: 600 }),
        async (failures, ttlSeconds) => {
          const r = await runTest(
            Effect.gen(function* () {
              const calls = yield* Ref.make(0)
              const cache = yield* Cache.make<number>({ ttlSeconds, maxEntries: 4 })
              const boom = (kind: "fail" | "die"): Effect.Effect<number, string> =>
                Effect.gen(function* () {
                  yield* Ref.update(calls, (n) => n + 1)
                  return kind === "fail"
                    ? yield* Effect.fail("boom")
                    : yield* Effect.die(new Error("boom"))
                })
              const outcomes: Array<boolean> = []
              const sizesDuring: Array<number> = []
              for (const kind of failures) {
                const exit = yield* Effect.exit(cache.get("k", boom(kind)))
                outcomes.push(Exit.isSuccess(exit))
                sizesDuring.push(yield* cache.size)
              }
              const ok = yield* cache.get("k", Ref.updateAndGet(calls, (n) => n + 1))
              const again = yield* cache.get("k", Ref.updateAndGet(calls, (n) => n + 1))
              return {
                outcomes,
                sizesDuring,
                ok,
                again,
                calls: yield* Ref.get(calls),
                size: yield* cache.size
              }
            })
          )
          expect(r.outcomes.every((s) => s === false)).toBe(true)
          // A failure stores nothing...
          expect(r.sizesDuring.every((s) => s === 0)).toBe(true)
          // ...and is re-attempted every time rather than served from cache.
          expect(r.calls).toBe(failures.length + 1)
          // The key is still usable afterwards — no leaked in-flight slot, so
          // the eventual success completes and is cached normally.
          expect(r.ok).toBe(failures.length + 1)
          expect(r.again).toBe(r.ok)
          expect(r.size).toBe(1)
        }
      ),
      { numRuns: 60 }
    )
  }, 30_000)

  it("L7: agrees with a hand-written reference cache on random command sequences", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: 1, max: 4 }),
        fc.integer({ min: 1, max: 10 }),
        fc.array(commandArb, { minLength: 1, maxLength: 30 }),
        async (maxEntries, ttlSeconds, commands) => {
          const expected = referenceCache(commands, ttlSeconds, maxEntries)
          const actual = await runTest(
            Effect.gen(function* () {
              const callsRef = yield* Ref.make(0)
              const cache = yield* Cache.make<number>({ ttlSeconds, maxEntries })
              const produce = Ref.updateAndGet(callsRef, (n) => n + 1)
              const reads: Array<number> = []
              const sizes: Array<number> = []
              for (const c of commands) {
                if (c.tag === "get") reads.push(yield* cache.get(c.key, produce))
                else if (c.tag === "invalidate") yield* cache.invalidate(c.key)
                else if (c.tag === "clear") yield* cache.clear
                else yield* TestClock.adjust(c.ms)
                sizes.push(yield* cache.size)
              }
              return { reads, sizes, calls: yield* Ref.get(callsRef) }
            })
          )
          expect(actual.reads).toEqual(expected.reads)
          expect(actual.sizes).toEqual(expected.sizes)
          expect(actual.calls).toBe(expected.calls)
        }
      ),
      { numRuns: 50 }
    )
  }, 40_000)
})

describe("RateLimiter — laws", () => {
  const runTest = <A, E>(e: Effect.Effect<A, E, never>) =>
    Effect.runPromise(Effect.provide(e, TestContext.TestContext) as Effect.Effect<A, E, never>)

  const policyArb: fc.Arbitrary<LimiterPolicy> = fc
    .record({
      maxEvents: fc.integer({ min: 1, max: 5 }),
      windowSeconds: fc.integer({ min: 1, max: 90 })
    })
    .map(({ maxEvents, windowSeconds }) => ({ maxEvents, windowSeconds, failClosed: false }))

  const keyArb = fc.constantFrom("a", "b", "c")

  // Delays are named relative to the window so the generator lands *on* the
  // interesting boundaries (exactly the window, one tick past it) instead of
  // hoping a uniform integer hits them.
  type DelayKind = "zero" | "tick" | "part" | "exact" | "past"
  const delayArb = fc.constantFrom("zero", "tick", "part", "exact", "past")
  const delayMillis = (kind: DelayKind, windowMillis: number): number =>
    kind === "zero"
      ? 0
      : kind === "tick"
        ? 1
        : kind === "part"
          ? Math.floor(windowMillis / 3)
          : kind === "exact"
            ? windowMillis
            : windowMillis + 1

  const stepArb = fc.record({ key: keyArb, delay: delayArb })
  const distinct = (keys: ReadonlyArray<string>) => Array.from(new Set(keys))

  // -- L1 --------------------------------------------------------------------
  it("grants a key exactly maxEvents allows per window, whatever the interleaving", async () => {
    await fc.assert(
      fc.asyncProperty(policyArb, fc.array(keyArb, { maxLength: 24 }), async (policy, keys) => {
        const decisions = await runTest(
          Effect.gen(function* () {
            const limiter = yield* makeInProcess(policy)
            const out: Array<Decision> = []
            for (const k of keys) out.push(yield* limiter.check(k))
            return out
          })
        )

        for (const key of distinct(keys)) {
          const mine = decisions.filter((_, i) => keys[i] === key)
          expect(mine.filter((d) => d.allowed).length).toBe(
            Math.min(mine.length, policy.maxEvents)
          )
          // allows form a prefix — nothing revives a key while the clock stands still
          const firstDeny = mine.findIndex((d) => !d.allowed)
          if (firstDeny >= 0) {
            expect(mine.slice(firstDeny).some((d) => d.allowed)).toBe(false)
          }
        }
      }),
      { numRuns: 60 }
    )
  })

  // -- L2 --------------------------------------------------------------------
  it("keys are independent: replaying one key alone on the same timeline is indistinguishable", async () => {
    await fc.assert(
      fc.asyncProperty(policyArb, fc.array(stepArb, { maxLength: 14 }), async (policy, schedule) => {
        const w = policy.windowSeconds * 1000

        const full = await runTest(
          Effect.gen(function* () {
            const limiter = yield* makeInProcess(policy)
            const out: Array<{ key: string; d: Decision }> = []
            for (const s of schedule) {
              const ms = delayMillis(s.delay, w)
              if (ms > 0) yield* TestClock.adjust(ms)
              out.push({ key: s.key, d: yield* limiter.check(s.key) })
            }
            return out
          })
        )

        for (const target of distinct(schedule.map((s) => s.key))) {
          // same clock movements, but only the target key's checks are issued
          const solo = await runTest(
            Effect.gen(function* () {
              const limiter = yield* makeInProcess(policy)
              const out: Array<Decision> = []
              for (const s of schedule) {
                const ms = delayMillis(s.delay, w)
                if (ms > 0) yield* TestClock.adjust(ms)
                if (s.key === target) out.push(yield* limiter.check(target))
              }
              return out
            })
          )
          expect(solo).toEqual(full.filter((x) => x.key === target).map((x) => x.d))
        }
      }),
      { numRuns: 40 }
    )
  })

  // -- L3 --------------------------------------------------------------------
  it("crossing the window restores the whole budget: R rounds give exactly R*maxEvents allows", async () => {
    await fc.assert(
      fc.asyncProperty(
        policyArb,
        fc.integer({ min: 1, max: 4 }),
        fc.integer({ min: 0, max: 3 }),
        async (policy, rounds, surplus) => {
          const w = policy.windowSeconds * 1000
          const allowed = await runTest(
            Effect.gen(function* () {
              const limiter = yield* makeInProcess(policy)
              let count = 0
              for (let r = 0; r < rounds; r++) {
                for (let i = 0; i < policy.maxEvents + surplus; i++) {
                  const d = yield* limiter.check("k")
                  if (d.allowed) count++
                }
                yield* TestClock.adjust(w + 1)
              }
              return count
            })
          )
          expect(allowed).toBe(rounds * policy.maxEvents)
        }
      ),
      { numRuns: 60 }
    )
  })

  // -- L4 --------------------------------------------------------------------
  it("never renews early: after exhaustion any wait of at most windowSeconds is still denied", async () => {
    await fc.assert(
      fc.asyncProperty(policyArb, fc.integer({ min: 0, max: 1000 }), async (policy, permille) => {
        const w = policy.windowSeconds * 1000
        const wait = Math.floor((w * permille) / 1000) // 0 <= wait <= windowMillis
        const r = await runTest(
          Effect.gen(function* () {
            const limiter = yield* makeInProcess(policy)
            const fill: Array<Decision> = []
            for (let i = 0; i < policy.maxEvents; i++) fill.push(yield* limiter.check("k"))
            if (wait > 0) yield* TestClock.adjust(wait)
            return { fill, after: yield* limiter.check("k") }
          })
        )
        expect(r.fill.every((d) => d.allowed)).toBe(true)
        expect(r.after.allowed).toBe(false)
      }),
      { numRuns: 60 }
    )
  })

  // -- L5 --------------------------------------------------------------------
  it("retryAfterSeconds is 0 iff allowed, else a whole number in [1, windowSeconds]", async () => {
    await fc.assert(
      fc.asyncProperty(policyArb, fc.array(stepArb, { maxLength: 20 }), async (policy, schedule) => {
        const w = policy.windowSeconds * 1000
        const decisions = await runTest(
          Effect.gen(function* () {
            const limiter = yield* makeInProcess(policy)
            const out: Array<Decision> = []
            for (const s of schedule) {
              const ms = delayMillis(s.delay, w)
              if (ms > 0) yield* TestClock.adjust(ms)
              out.push(yield* limiter.check(s.key))
            }
            return out
          })
        )
        for (const d of decisions) {
          if (d.allowed) {
            expect(d.retryAfterSeconds).toBe(0)
          } else {
            expect(Number.isInteger(d.retryAfterSeconds)).toBe(true)
            expect(d.retryAfterSeconds).toBeGreaterThanOrEqual(1)
            expect(d.retryAfterSeconds).toBeLessThanOrEqual(policy.windowSeconds)
          }
        }
      }),
      { numRuns: 60 }
    )
  })

  // -- L6 --------------------------------------------------------------------
  it("retryAfterSeconds is a tight honest hint: still denied a second earlier, allowed just after", async () => {
    await fc.assert(
      fc.asyncProperty(
        policyArb,
        fc.integer({ min: 0, max: 1000 }),
        fc.array(fc.integer({ min: 0, max: 1000 }), { minLength: 5, maxLength: 5 }),
        async (policy, startPermille, gapPermille) => {
          const w = policy.windowSeconds * 1000
          // gaps stay under w/(maxEvents+1) each, so the whole fill provably
          // fits inside one window and the next check is provably a denial
          const maxGap = Math.floor(w / (policy.maxEvents + 1))
          const start = Math.floor((w * startPermille) / 1000)
          const gaps = gapPermille
            .slice(0, policy.maxEvents)
            .map((p) => Math.floor((maxGap * p) / 1000))

          const r = await runTest(
            Effect.gen(function* () {
              const limiter = yield* makeInProcess(policy)
              if (start > 0) yield* TestClock.adjust(start)
              const fill: Array<Decision> = []
              for (const g of gaps) {
                if (g > 0) yield* TestClock.adjust(g)
                fill.push(yield* limiter.check("k"))
              }
              const denied = yield* limiter.check("k")

              let early: Decision | null = null
              if (denied.retryAfterSeconds >= 2) {
                yield* TestClock.adjust((denied.retryAfterSeconds - 1) * 1000)
                early = yield* limiter.check("k")
              }
              // total elapsed since the denial is now retryAfterSeconds*1000 + 1
              yield* TestClock.adjust(1001)
              return { fill, denied, early, after: yield* limiter.check("k") }
            })
          )

          expect(r.fill.every((d) => d.allowed)).toBe(true)
          expect(r.denied.allowed).toBe(false)
          if (r.early !== null) expect(r.early.allowed).toBe(false) // not an over-estimate
          expect(r.after.allowed).toBe(true) // not an under-estimate
        }
      ),
      { numRuns: 60 }
    )
  })

  // -- L7 --------------------------------------------------------------------
  it("reset puts every key back to a full budget at the very same instant", async () => {
    await fc.assert(
      fc.asyncProperty(policyArb, fc.array(keyArb, { maxLength: 12 }), async (policy, warmup) => {
        const probes = ["a", "b", "c"]
        const after = await runTest(
          Effect.gen(function* () {
            const limiter = yield* makeInProcess(policy)
            for (const k of warmup) yield* limiter.check(k)
            yield* limiter.reset
            const out: Record<string, Array<Decision>> = {}
            for (const k of probes) {
              const seq: Array<Decision> = []
              for (let i = 0; i < policy.maxEvents + 1; i++) seq.push(yield* limiter.check(k))
              out[k] = seq
            }
            return out
          })
        )
        for (const k of probes) {
          const seq = after[k]!
          expect(seq.slice(0, policy.maxEvents).every((d) => d.allowed)).toBe(true)
          expect(seq[policy.maxEvents]!.allowed).toBe(false)
        }
      }),
      { numRuns: 50 }
    )
  })

  // -- L8 --------------------------------------------------------------------
  it("denyAll never allows — check and enforce always fail Unavailable, before and after reset", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.string(),
        fc.array(keyArb, { minLength: 1, maxLength: 8 }),
        async (reason, keys) => {
          const limiter = denyAll(reason)
          const r = await runTest(
            Effect.gen(function* () {
              const checks: Array<Either.Either<Decision, Unavailable>> = []
              const enforced: Array<Either.Either<void, RateLimited | Unavailable>> = []
              for (const k of keys) {
                checks.push(yield* Effect.either(limiter.check(k)))
                enforced.push(yield* Effect.either(enforce(limiter, k)))
              }
              const ready = yield* limiter.ready
              yield* limiter.reset
              const afterReset = yield* Effect.either(limiter.check(keys[0]!))
              return { checks, enforced, ready, afterReset }
            })
          )

          expect(r.ready).toBe(false)
          for (const c of [...r.checks, r.afterReset]) {
            expect(Either.isLeft(c)).toBe(true)
            if (Either.isLeft(c)) {
              expect(c.left._tag).toBe("Unavailable")
              expect(c.left.reason).toBe(reason)
            }
          }
          for (const e of r.enforced) {
            expect(Either.isLeft(e)).toBe(true)
            if (Either.isLeft(e)) expect(e.left._tag).toBe("Unavailable")
          }
        }
      ),
      { numRuns: 50 }
    )
  })

  // -- L9 --------------------------------------------------------------------
  it("enforce mirrors the budget: min(n, maxEvents) successes per key, rest RateLimited", async () => {
    await fc.assert(
      fc.asyncProperty(policyArb, fc.array(keyArb, { maxLength: 18 }), async (policy, keys) => {
        const results = await runTest(
          Effect.gen(function* () {
            const limiter = yield* makeInProcess(policy)
            const out: Array<Either.Either<void, RateLimited | Unavailable>> = []
            for (const k of keys) out.push(yield* Effect.either(enforce(limiter, k)))
            return out
          })
        )

        for (const key of distinct(keys)) {
          const mine = results.filter((_, i) => keys[i] === key)
          expect(mine.filter(Either.isRight).length).toBe(Math.min(mine.length, policy.maxEvents))
        }
        for (const e of results) {
          if (Either.isLeft(e)) {
            expect(e.left._tag).toBe("RateLimited")
            const hint = (e.left as RateLimited).retryAfterSeconds
            expect(hint).toBeGreaterThanOrEqual(1)
            expect(hint).toBeLessThanOrEqual(policy.windowSeconds)
          }
        }
      }),
      { numRuns: 60 }
    )
  })
})

describe("domain/Contracts — wire contract laws", () => {
  const decFeedback = Schema.decodeUnknownEither(FeedbackRequest)
  const decList = Schema.decodeUnknownEither(ListParamsStrict)
  const decFindings = Schema.decodeUnknownEither(FindingsParamsStrict)
  const decNeighbors = Schema.decodeUnknownEither(NeighborsParamsStrict)

  const right = <A, E>(e: Either.Either<A, E>): A => {
    if (Either.isLeft(e)) throw new Error("expected Right, got Left")
    return e.right
  }
  const isOk = <A, E>(e: Either.Either<A, E>): boolean => Either.isRight(e)

  // -- generators -------------------------------------------------------------

  /** Printable, C0-free characters. No "/" or "@" so path/email laws stay sharp. */
  const VISIBLE = "abzABZ019.,;:!?'()[]{}<>*&^%$#~|+=-_é漢"
  const visibleChar = fc.constantFrom(...VISIBLE.split(""))
  const innerChar = fc.oneof(visibleChar, fc.constant(" "))

  /** Non-empty, already-trimmed, C0-free; length >= 2 so an interior index exists. */
  const cleanCore = fc
    .tuple(visibleChar, fc.array(innerChar, { maxLength: 12 }), visibleChar)
    .map(([a, mid, b]) => a + mid.join("") + b)

  const TAB = String.fromCharCode(9)
  const LF = String.fromCharCode(10)
  const VT = String.fromCharCode(11)
  const FF = String.fromCharCode(12)
  const CR = String.fromCharCode(13)

  /** Whitespace that String.prototype.trim strips. Note VT and FF are C0 too. */
  const trimmable = fc
    .array(fc.constantFrom(" ", TAB, LF, VT, FF, CR), { maxLength: 4 })
    .map((cs) => cs.join(""))

  const anyC0 = fc.integer({ min: 0, max: 31 }).map((c) => String.fromCharCode(c))
  const nonWhitespaceC0 = anyC0.filter((c) => c !== TAB && c !== LF && c !== CR)
  const whitespaceC0 = fc.constantFrom(TAB, LF, CR)

  /** Insert `c` strictly inside `s` (never at index 0 or s.length). */
  const insertInside = (s: string, c: string, i: number): string => {
    const at = 1 + (i % (s.length - 1))
    return s.slice(0, at) + c + s.slice(at)
  }

  const atFreeChunk = fc
    .array(visibleChar, { minLength: 1, maxLength: 8 })
    .map((cs) => cs.join(""))

  const pathSeg = fc
    .tuple(
      fc.constantFrom(..."abzABZ019".split("")),
      fc.array(fc.constantFrom(..."abz019-_./".split("")), { maxLength: 10 })
    )
    .map(([a, rest]) => a + rest.join(""))

  // ------------------------------------------------------------------ subject
  it("subject: a C0 character that survives trimming always flips accept to reject", () => {
    fc.assert(
      fc.property(cleanCore, cleanCore, anyC0, fc.nat(), (subject, body, ctrl, i) => {
        // the un-poisoned request is the control: it must be accepted
        expect(isOk(decFeedback({ subject, body }))).toBe(true)
        const poisoned = insertInside(subject, ctrl, i)
        expect(poisoned.trim()).toBe(poisoned) // the char really did survive trim
        expect(isOk(decFeedback({ subject: poisoned, body }))).toBe(false)
      })
    )
  })

  it("subject: decoding is exactly `trim`, and the result is always non-empty", () => {
    fc.assert(
      fc.property(trimmable, cleanCore, trimmable, cleanCore, (lead, core, trail, body) => {
        const raw = lead + core + trail
        const out = right(decFeedback({ subject: raw, body }))
        expect(out.subject).toBe(raw.trim())
        expect(out.subject).toBe(core)
        expect(out.subject.trim()).toBe(out.subject)
        expect(out.subject.length).toBeGreaterThan(0)
        // drop the visible text and only whitespace is left => nothing to accept
        expect(isOk(decFeedback({ subject: lead + trail, body }))).toBe(false)
      })
    )
  })

  // --------------------------------------------------------------------- body
  it("body: interior LF/CR/TAB are preserved; every other C0 char is rejected", () => {
    fc.assert(
      fc.property(
        cleanCore,
        cleanCore,
        nonWhitespaceC0,
        whitespaceC0,
        fc.nat(),
        (subject, body, bad, good, i) => {
          expect(isOk(decFeedback({ subject, body }))).toBe(true)

          expect(isOk(decFeedback({ subject, body: insertInside(body, bad, i) }))).toBe(false)

          const allowed = insertInside(body, good, i)
          const out = right(decFeedback({ subject, body: allowed }))
          expect(out.body).toBe(allowed) // preserved verbatim, not stripped
        }
      )
    )
  })

  // -------------------------------------------------------------------- email
  it("email: a non-empty decoded email is accepted iff contact_consent is true", () => {
    fc.assert(
      fc.property(
        atFreeChunk,
        atFreeChunk,
        cleanCore,
        cleanCore,
        trimmable,
        (local, domain, subject, body, blank) => {
          const email = `${local}@${domain}`

          expect(isOk(decFeedback({ subject, body, email }))).toBe(false)
          expect(isOk(decFeedback({ subject, body, email, contact_consent: false }))).toBe(false)

          const out = right(decFeedback({ subject, body, email, contact_consent: true }))
          expect(out.email).toBe(email)
          expect(out.contact_consent).toBe(true)

          // consent is keyed on the DECODED email, so whitespace-only needs none
          const noEmail = right(decFeedback({ subject, body, email: blank }))
          expect(noEmail.email).toBe("")
          expect(noEmail.contact_consent).toBe(false)
        }
      )
    )
  })

  // -------------------------------------------------------------- source_path
  it("source_path: every accepted request carries a single-slash absolute path", () => {
    fc.assert(
      fc.property(
        cleanCore,
        cleanCore,
        pathSeg,
        trimmable,
        trimmable,
        (subject, body, seg, lead, trail) => {
          const candidates: ReadonlyArray<Record<string, unknown>> = [
            { subject, body },
            { subject, body, source_path: lead + "/" + seg + trail },
            { subject, body, source_path: seg },
            { subject, body, source_path: "//" + seg },
            { subject, body, source_path: lead + trail }
          ]
          for (const c of candidates) {
            const r = decFeedback(c)
            if (Either.isRight(r)) {
              expect(r.right.source_path.startsWith("/")).toBe(true)
              expect(r.right.source_path.startsWith("//")).toBe(false)
            }
          }

          // and the shapes above are decided, not merely permitted
          expect(right(decFeedback(candidates[0]!)).source_path).toBe("/")
          expect(right(decFeedback(candidates[1]!)).source_path).toBe("/" + seg)
          expect(isOk(decFeedback(candidates[2]!))).toBe(false)
          expect(isOk(decFeedback(candidates[3]!))).toBe(false)
        }
      )
    )
  })

  // --------------------------------------------------------------- pagination
  it("pagination: an in-band limit/offset is accepted and returned unchanged (never clamped)", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 100 }),
        fc.integer({ min: 0, max: 10_000 }),
        fc.integer({ min: 1, max: 200 }),
        (limit, offset, nbLimit) => {
          const l = right(decList({ limit: String(limit), offset: String(offset) }))
          expect(l.limit).toBe(limit)
          expect(l.offset).toBe(offset)

          const f = right(decFindings({ limit: String(limit), offset: String(offset) }))
          expect(f.limit).toBe(limit)
          expect(f.offset).toBe(offset)

          const n = right(decNeighbors({ name: "x", limit: String(nbLimit) }))
          expect(n.limit).toBe(nbLimit)

          // a fallback must itself be in band: omitting a param must behave
          // exactly like sending that same value explicitly
          const omitted = right(decList({}))
          const explicit = right(
            decList({ limit: String(omitted.limit), offset: String(omitted.offset) })
          )
          expect(explicit).toEqual(omitted)
        }
      )
    )
  })

  it("pagination: acceptance matches the declared band exactly — the schemas are an oracle", () => {
    fc.assert(
      fc.property(fc.integer({ min: -200, max: 500 }), (n) => {
        const s = String(n)
        const inList = isOk(decList({ limit: s }))
        const inFindings = isOk(decFindings({ limit: s }))
        const inNeighbors = isOk(decNeighbors({ name: "x", limit: s }))

        expect(inList).toBe(n >= 1 && n <= 100)
        expect(inFindings).toBe(inList) // same declared bound
        expect(inNeighbors).toBe(n >= 1 && n <= 200)
        if (inList) expect(inNeighbors).toBe(true) // 1..100 is a subset of 1..200

        expect(isOk(decList({ offset: s }))).toBe(n >= 0 && n <= 10_000)

        // `name` is required for neighbours, whatever the limit is
        expect(isOk(decNeighbors({ limit: s }))).toBe(false)
      })
    )
  })

  it("pagination: a non-integer or non-numeric limit is rejected even inside the band", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 99 }),
        fc.integer({ min: 1, max: 9 }),
        fc.string({ minLength: 1, maxLength: 6 }).filter((s) => Number.isNaN(Number(s))),
        (whole, frac, junk) => {
          expect(isOk(decList({ limit: `${whole}.${frac}` }))).toBe(false)
          expect(isOk(decList({ limit: junk }))).toBe(false)
          expect(isOk(decNeighbors({ name: "x", limit: `${whole}.${frac}` }))).toBe(false)
          // the band is over the parsed number, so a numeric-typed param is not a string
          expect(isOk(decList({ limit: whole }))).toBe(false)
        }
      )
    )
  })
})

describe("KgPort pure exports", () => {
  // --- generators ----------------------------------------------------------

  /** JSON-shaped values. `-0` and `NaN` are excluded so `Object.is` is a
   *  usable notion of "left untouched" for the non-integral arm. */
  const jsonLeaf = fc.oneof(
    fc.integer(),
    fc.double({ noNaN: true, noDefaultInfinity: true }).filter((d) => !Number.isInteger(d)),
    fc.string(),
    fc.boolean(),
    fc.constant(null)
  )

  const jsonValue: fc.Arbitrary<unknown> = fc.letrec<{ v: unknown }>((tie) => ({
    v: fc.oneof(
      { maxDepth: 4, depthSize: "small" },
      jsonLeaf,
      fc.array(tie("v"), { maxLength: 4 }),
      fc.dictionary(fc.constantFrom("a", "b", "c", "d"), tie("v"), { maxKeys: 4 })
    )
  })).v

  const pad = (n: number) => String(n).padStart(2, "0")

  /** Fixed-width ASCII stamps: every non-digit character sits at the same
   *  offset in all of them, so `localeCompare`, `<` and chronology agree. */
  const isoStamp = fc
    .tuple(
      fc.integer({ min: 2020, max: 2030 }),
      fc.integer({ min: 1, max: 12 }),
      fc.integer({ min: 1, max: 28 }),
      fc.integer({ min: 0, max: 23 }),
      fc.integer({ min: 0, max: 59 })
    )
    .map(([y, mo, d, h, mi]) => `${y}-${pad(mo)}-${pad(d)}T${pad(h)}:${pad(mi)}:00Z`)

  const stamp = fc.oneof(
    { arbitrary: isoStamp, weight: 4 },
    { arbitrary: fc.constant(""), weight: 1 }
  )

  const findingsArb = fc
    .array(
      fc.record({ finding: fc.string(), axis: fc.string(), subAxis: fc.string(), createdAt: stamp }),
      { maxLength: 8 }
    )
    .map((rows) =>
      rows.map(
        (r, i) =>
          new FindingRecord({
            name: `finding-${i}`,
            finding: r.finding,
            axis: r.axis,
            subAxis: r.subAxis,
            createdAt: r.createdAt
          })
      )
    )

  const lessonsArb = fc
    .array(
      fc.record({
        problem: fc.string(),
        solution: fc.string(),
        wrongAssumption: fc.string(),
        truth: fc.string(),
        createdAt: stamp
      }),
      { maxLength: 8 }
    )
    .map((rows) =>
      rows.map(
        (r, i) =>
          new LessonRecord({
            name: `lesson-${i}`,
            problem: r.problem,
            solution: r.solution,
            wrongAssumption: r.wrongAssumption,
            truth: r.truth,
            createdAt: r.createdAt
          })
      )
    )

  const consensusArb = fc
    .array(fc.record({ summary: fc.string(), createdAt: stamp }), { maxLength: 8 })
    .map((rows) =>
      rows.map(
        (r, i) =>
          new ConsensusRecord({ name: `consensus-${i}`, summary: r.summary, createdAt: r.createdAt })
      )
    )

  const neighborRow = fc.record({
    direction: fc.constantFrom("in", "out", "IN", "", "sideways"),
    type: fc.string(),
    name: fc.string(),
    labels: fc.array(fc.string(), { maxLength: 3 })
  })

  /** newest-first with blanks last, as a predicate rather than a comparator. */
  const strictlyNewer = (a: string, b: string): boolean => a !== "" && (b === "" || a > b)

  // --- coerceParams --------------------------------------------------------

  it("promotes every integral number to a Neo4j Integer at any depth, leaves every other leaf identical, and preserves the container shape", () => {
    const walk = (orig: unknown, out: unknown, path: string): void => {
      if (typeof orig === "number") {
        if (Number.isInteger(orig)) {
          expect(neo4j.isInt(out), `${path}: ${orig} must become a Neo4j Integer`).toBe(true)
          expect((out as { toNumber: () => number }).toNumber(), `${path}: value`).toBe(orig)
        } else {
          expect(Object.is(out, orig), `${path}: non-integral ${orig} must be untouched`).toBe(true)
        }
        return
      }
      if (orig === null || typeof orig === "string" || typeof orig === "boolean") {
        expect(out, `${path}: scalar must be untouched`).toBe(orig)
        return
      }
      if (Array.isArray(orig)) {
        expect(Array.isArray(out), `${path}: array must stay an array`).toBe(true)
        const arr = out as ReadonlyArray<unknown>
        expect(arr.length, `${path}: array length`).toBe(orig.length)
        orig.forEach((v, i) => walk(v, arr[i], `${path}[${i}]`))
        return
      }
      expect(neo4j.isInt(out), `${path}: object must not become an Integer`).toBe(false)
      expect(Array.isArray(out), `${path}: object must not become an array`).toBe(false)
      expect(Object.keys(out as object).sort(), `${path}: key set`).toEqual(
        Object.keys(orig as object).sort()
      )
      for (const k of Object.keys(orig as object)) {
        walk((orig as Record<string, unknown>)[k], (out as Record<string, unknown>)[k], `${path}.${k}`)
      }
    }

    fc.assert(
      fc.property(jsonValue, (value) => {
        walk(value, coerceParams(value), "$")
      })
    )
  })

  it("never mutates the value it was handed", () => {
    fc.assert(
      fc.property(jsonValue, (value) => {
        const before = JSON.stringify(value)
        coerceParams(value)
        expect(JSON.stringify(value)).toBe(before)
      })
    )
  })

  // --- mergeRecent ---------------------------------------------------------

  it("emits exactly min(limit, total inputs) items", () => {
    fc.assert(
      fc.property(
        findingsArb,
        lessonsArb,
        consensusArb,
        fc.nat({ max: 30 }),
        (findings, lessons, consensus, limit) => {
          const total = findings.length + lessons.length + consensus.length
          expect(mergeRecent({ findings, lessons, consensus, limit }).length).toBe(
            Math.min(limit, total)
          )
        }
      )
    )
  })

  it("orders output newest-first, with blank timestamps last", () => {
    fc.assert(
      fc.property(
        findingsArb,
        lessonsArb,
        consensusArb,
        fc.nat({ max: 30 }),
        (findings, lessons, consensus, limit) => {
          const out = mergeRecent({ findings, lessons, consensus, limit })
          for (let i = 0; i + 1 < out.length; i++) {
            const a = out[i]!.createdAt
            const b = out[i + 1]!.createdAt
            if (a === "") {
              expect(b, "a blank timestamp may only be followed by blanks").toBe("")
            } else {
              expect(b === "" || a >= b, `not descending at ${i}: ${a} then ${b}`).toBe(true)
            }
          }
        }
      )
    )
  })

  it("returns each output item from exactly one input item — no duplication, no invention, no cross-wired fields", () => {
    fc.assert(
      fc.property(
        findingsArb,
        lessonsArb,
        consensusArb,
        fc.nat({ max: 30 }),
        (findings, lessons, consensus, limit) => {
          const out = mergeRecent({ findings, lessons, consensus, limit })

          const stamps = new Map<string, string>()
          const summaries = new Map<string, string>()
          for (const f of findings) {
            stamps.set(`finding:${f.name}`, f.createdAt)
            summaries.set(`finding:${f.name}`, f.finding)
          }
          for (const l of lessons) stamps.set(`lesson:${l.name}`, l.createdAt)
          for (const c of consensus) {
            stamps.set(`consensus:${c.name}`, c.createdAt)
            summaries.set(`consensus:${c.name}`, c.summary)
          }

          const keys = out.map((i) => `${i.type}:${i.name}`)
          expect(new Set(keys).size, "no item may appear twice").toBe(keys.length)

          for (const item of out) {
            const key = `${item.type}:${item.name}`
            expect(stamps.has(key), `invented item ${key}`).toBe(true)
            expect(item.createdAt, `${key}: timestamp must stay with its own item`).toBe(
              stamps.get(key)
            )
            if (summaries.has(key)) {
              expect(item.summary, `${key}: summary must stay with its own item`).toBe(
                summaries.get(key)
              )
            }
            if (item.type === "finding") {
              expect(item.title, `${key}: a finding is never left untitled`).not.toBe("")
            }
          }
        }
      )
    )
  })

  it("drops only the oldest — no excluded input is newer than an included one", () => {
    fc.assert(
      fc.property(
        findingsArb,
        lessonsArb,
        consensusArb,
        fc.nat({ max: 30 }),
        (findings, lessons, consensus, limit) => {
          const out = mergeRecent({ findings, lessons, consensus, limit })
          const kept = new Set(out.map((i) => `${i.type}:${i.name}`))

          const all: Array<{ key: string; createdAt: string }> = [
            ...findings.map((f) => ({ key: `finding:${f.name}`, createdAt: f.createdAt })),
            ...lessons.map((l) => ({ key: `lesson:${l.name}`, createdAt: l.createdAt })),
            ...consensus.map((c) => ({ key: `consensus:${c.name}`, createdAt: c.createdAt }))
          ]

          for (const dropped of all.filter((a) => !kept.has(a.key))) {
            for (const item of out) {
              expect(
                strictlyNewer(dropped.createdAt, item.createdAt),
                `dropped ${dropped.key} (${dropped.createdAt}) is newer than kept ${item.name} (${item.createdAt})`
              ).toBe(false)
            }
          }
        }
      )
    )
  })

  // --- decodeNeighbors / notFoundNeighbors ---------------------------------

  it("marks truncated exactly when the reported degree exceeds the returned neighbour count", () => {
    fc.assert(
      fc.property(
        fc.string(),
        fc.nat({ max: 12 }),
        fc.array(neighborRow, { maxLength: 12 }),
        (name, degree, list) => {
          const out = decodeNeighbors(name, [{ degree, neighbors: list }])
          expect(out.name).toBe(name)
          expect(out.found).toBe(true)
          expect(out.degree).toBe(degree)
          expect(out.neighbors.length, "every returned neighbour is kept, none added").toBe(
            list.length
          )
          expect(out.truncated).toBe(degree > list.length)
          out.neighbors.forEach((n, i) => {
            const src = list[i]!
            expect(n.name).toBe(src.name)
            expect(n.type).toBe(src.type)
            expect(n.labels).toEqual(src.labels)
            expect(n.direction, "direction is 'in' only when the row said 'in'").toBe(
              src.direction === "in" ? "in" : "out"
            )
          })
        }
      )
    )
  })

  it("decodes a degree identically whether it arrives as a number, a Neo4j Integer, or a numeric string", () => {
    fc.assert(
      fc.property(
        fc.string(),
        fc.nat({ max: 500 }),
        fc.array(neighborRow, { maxLength: 6 }),
        (name, degree, list) => {
          const asNumber = decodeNeighbors(name, [{ degree, neighbors: list }])
          expect(decodeNeighbors(name, [{ degree: neo4j.int(degree), neighbors: list }])).toStrictEqual(asNumber)
          expect(decodeNeighbors(name, [{ degree: String(degree), neighbors: list }])).toStrictEqual(asNumber)
        }
      )
    )
  })

  it("reports an empty row list as not-found with degree 0 and nothing truncated", () => {
    fc.assert(
      fc.property(fc.string(), (name) => {
        const out = decodeNeighbors(name, [])
        expect(out.found).toBe(false)
        expect(out.degree).toBe(0)
        expect(out.neighbors).toEqual([])
        expect(out.truncated).toBe(false)
        expect(out.name).toBe(name)
        expect(out).toStrictEqual(notFoundNeighbors(name))
      })
    )
  })
})

describe("ClientIp.resolve — property laws", () => {
  // --- generators -----------------------------------------------------------
  const WS = fc.constantFrom("", " ", "  ", "\t", "\n", " \t ")
  const padded = (inner: fc.Arbitrary<string>) =>
    fc.tuple(WS, inner, WS).map(([a, v, b]) => `${a}${v}${b}`)

  /** undefined / empty / whitespace-only — every value that must count as "absent". */
  const blank = fc.constantFrom(undefined, "", " ", "   ", "\t", "\n", " \t\n ")

  // Disjoint address pools: a value from one pool can never equal a value from
  // another, so "the result is X" also proves "the result is not Y".
  const remoteTok = fc.integer({ min: 1, max: 254 }).map((n) => `10.0.0.${n}`)
  const cfTok = fc.integer({ min: 1, max: 254 }).map((n) => `172.16.0.${n}`)
  const proxyTok = fc.integer({ min: 1, max: 254 }).map((n) => `192.168.7.${n}`)
  const attackerTok = fc.integer({ min: 1, max: 254 }).map((n) => `203.0.113.${n}`)

  /** Anything a hostile client could put in a header. */
  const adversarialHeader = fc.oneof(
    fc.constant(undefined),
    fc.string(),
    fc.constantFrom(
      "",
      " ",
      "\t",
      "127.0.0.1",
      "::1",
      "0.0.0.0",
      "203.0.113.9, 198.51.100.4",
      ",,,",
      "a,b,c",
      "10.0.0.1;10.0.0.2",
      "<script>alert(1)</script>",
      "10.0.0.1\n10.0.0.2",
      "unknown"
    ),
    fc.array(fc.string(), { maxLength: 4 }).map((xs) => xs.join(","))
  )

  // --- L1 -------------------------------------------------------------------
  it("L1: with trustProxy=false the headers are inert — no header value can change the answer", () => {
    fc.assert(
      fc.property(adversarialHeader, adversarialHeader, fc.option(fc.string(), { nil: undefined }), (cf, xff, remote) => {
        const withHeaders = resolve({
          trustProxy: false,
          cfConnectingIp: cf,
          xForwardedFor: xff,
          remoteAddress: remote
        })
        const withoutHeaders = resolve({
          trustProxy: false,
          cfConnectingIp: undefined,
          xForwardedFor: undefined,
          remoteAddress: remote
        })
        expect(withHeaders).toBe(withoutHeaders)
      })
    )
  })

  // --- L2 -------------------------------------------------------------------
  it("L2: with trustProxy=false the answer is the transport peer, or 'unknown' when there is none", () => {
    fc.assert(
      fc.property(adversarialHeader, adversarialHeader, fc.oneof(padded(remoteTok), blank), (cf, xff, remote) => {
        const out = resolve({
          trustProxy: false,
          cfConnectingIp: cf,
          xForwardedFor: xff,
          remoteAddress: remote
        })
        if (remote === undefined || remote.trim() === "") {
          expect(out).toBe("unknown")
        } else {
          expect(out).toBe(remote.trim())
        }
      })
    )
  })

  // --- L3 -------------------------------------------------------------------
  it("L3: with trustProxy=true a non-blank CF-Connecting-IP outranks XFF and the peer", () => {
    fc.assert(
      fc.property(
        padded(cfTok),
        adversarialHeader,
        fc.oneof(padded(remoteTok), blank),
        (cf, xff, remote) => {
          const out = resolve({
            trustProxy: true,
            cfConnectingIp: cf,
            xForwardedFor: xff,
            remoteAddress: remote
          })
          expect(out).toBe(cf.trim())
        }
      )
    )
  })

  // --- L4 -------------------------------------------------------------------
  it("L4: with trustProxy=true and no CF header, the RIGHTMOST XFF entry wins", () => {
    fc.assert(
      fc.property(
        fc.array(padded(attackerTok), { minLength: 1, maxLength: 5 }),
        padded(proxyTok),
        blank,
        fc.oneof(padded(remoteTok), blank),
        (leading, last, cf, remote) => {
          const xff = [...leading, last].join(",")
          const out = resolve({
            trustProxy: true,
            cfConnectingIp: cf,
            xForwardedFor: xff,
            remoteAddress: remote
          })
          expect(out).toBe(last.trim())
          expect(out).not.toBe(leading[0]!.trim())
        }
      )
    )
  })

  // --- L5 -------------------------------------------------------------------
  it("L5: prepending arbitrary attacker-controlled XFF entries cannot change the answer", () => {
    fc.assert(
      fc.property(fc.string(), fc.string(), blank, fc.oneof(padded(remoteTok), blank), (prefix, base, cf, remote) => {
        const plain = resolve({
          trustProxy: true,
          cfConnectingIp: cf,
          xForwardedFor: base,
          remoteAddress: remote
        })
        const spoofed = resolve({
          trustProxy: true,
          cfConnectingIp: cf,
          xForwardedFor: `${prefix},${base}`,
          remoteAddress: remote
        })
        expect(spoofed).toBe(plain)
      })
    )
  })

  // --- L6 -------------------------------------------------------------------
  it("L6: no non-final XFF entry is ever returned, whatever trustProxy is", () => {
    fc.assert(
      fc.property(
        fc.boolean(),
        fc.array(padded(attackerTok), { minLength: 1, maxLength: 5 }),
        padded(proxyTok),
        fc.oneof(padded(cfTok), blank),
        fc.oneof(padded(remoteTok), blank),
        (trustProxy, leading, last, cf, remote) => {
          const xff = [...leading, last].join(",")
          const out = resolve({ trustProxy, cfConnectingIp: cf, xForwardedFor: xff, remoteAddress: remote })
          for (const spoof of leading) expect(out).not.toBe(spoof.trim())
        }
      )
    )
  })

  // --- L7 -------------------------------------------------------------------
  it("L7: the result is total — never empty, never untrimmed", () => {
    fc.assert(
      fc.property(fc.boolean(), adversarialHeader, adversarialHeader, adversarialHeader, (tp, cf, xff, remote) => {
        const out = resolve({ trustProxy: tp, cfConnectingIp: cf, xForwardedFor: xff, remoteAddress: remote })
        expect(typeof out).toBe("string")
        expect(out).not.toBe("")
        expect(out).toBe(out.trim())
      })
    )
  })

  // --- L8 -------------------------------------------------------------------
  it("L8: ClientIpFixed yields its key verbatim and identically on every read", async () => {
    await fc.assert(
      fc.asyncProperty(fc.string(), async (key) => {
        const read = Effect.gen(function* () {
          const svc = yield* ClientIpTag
          return yield* svc.key
        }).pipe(
          Effect.provide(ClientIpFixed(key)),
          Effect.provideService(
            HttpServerRequest.HttpServerRequest,
            {} as HttpServerRequest.HttpServerRequest
          )
        )
        const a = await Effect.runPromise(read)
        const b = await Effect.runPromise(read)
        expect(a).toBe(key)
        expect(b).toBe(key)
      })
    )
  })
})

describe("KgWritePort.plan / WriteBatch — laws", () => {
  // -------------------------------------------------------------------------
  // generators
  // -------------------------------------------------------------------------
  const charsOf = (s: string) => fc.constantFrom(...s.split(""))

  const nodeName: fc.Arbitrary<string> = fc
    .array(charsOf("abcdefghijklmnopqrstuvwxyz0123456789-_"), { minLength: 1, maxLength: 30 })
    .map((cs) => cs.join(""))

  const relTypeArb: fc.Arbitrary<string> = fc
    .tuple(
      charsOf("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
      fc.array(charsOf("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"), { maxLength: 18 })
    )
    .map(([h, t]) => h + t.join(""))

  const citationArb: fc.Arbitrary<CitationEvidence> = fc.oneof(
    fc.webUrl().map((u) => ({ _tag: "Url" as const, citation_url: u })),
    fc
      .string({ minLength: 12, maxLength: 120 })
      .map((r) => ({ _tag: "NoExternalSource" as const, no_external_citation_reason: r }))
  )

  const text = (max = 80) => fc.string({ minLength: 1, maxLength: max })

  const provArb: fc.Arbitrary<Provenance> = fc.record({
    actor: text(20),
    runId: text(20),
    sourcePath: fc.string({ maxLength: 40 })
  })

  const nowArb: fc.Arbitrary<string> = fc.date({ noInvalidDate: true }).map((d) => d.toISOString())

  const findingArb = (hub: fc.Arbitrary<string | undefined>): fc.Arbitrary<WriteIntent> =>
    fc
      .record({
        name: nodeName,
        description: text(),
        evidence: text(),
        citation: citationArb,
        status: text(40),
        hub
      })
      .map((r) => ({ _tag: "RecordFinding", ...r }) as WriteIntent)

  const lessonArb: fc.Arbitrary<WriteIntent> = fc
    .record({
      name: nodeName,
      wrongAssumption: text(),
      truth: text(),
      problem: fc.string({ maxLength: 40 }),
      solution: fc.string({ maxLength: 40 }),
      lakatos_mechanism: fc.constantFrom(
        "monster-barring",
        "exception-barring",
        "concept-stretching",
        "lemma-incorporation",
        "proof-analysis"
      ),
      citation: citationArb
    })
    .map((r) => ({ _tag: "RecordLesson", ...r }) as WriteIntent)

  const dispatchArb: fc.Arbitrary<WriteIntent> = fc
    .record({
      name: nodeName,
      parent: nodeName,
      children: fc.array(nodeName, { minLength: 1, maxLength: 6 }),
      kind: text(40),
      stoppedBecause: fc.string({ maxLength: 40 }),
      citation: citationArb
    })
    .map((r) => ({ _tag: "RecordDispatch", ...r }) as WriteIntent)

  const linkArb = (rel: fc.Arbitrary<string>): fc.Arbitrary<WriteIntent> =>
    fc
      .record({ from: nodeName, to: nodeName, relType: rel })
      .map((r) => ({ _tag: "LinkNodes", ...r }) as WriteIntent)

  const nodeProducingArb: fc.Arbitrary<WriteIntent> = fc.oneof(
    findingArb(fc.option(nodeName, { nil: undefined })),
    lessonArb,
    dispatchArb
  )

  const anyIntentArb: fc.Arbitrary<WriteIntent> = fc.oneof(nodeProducingArb, linkArb(relTypeArb))

  /** Two intents of the same structural shape but wholly independent data. */
  const sameShapePairArb: fc.Arbitrary<readonly [WriteIntent, WriteIntent]> = fc.oneof(
    fc.tuple(findingArb(fc.constant(undefined)), findingArb(fc.constant(undefined))),
    fc.tuple(findingArb(nodeName), findingArb(nodeName)),
    fc.tuple(lessonArb, lessonArb),
    fc.tuple(dispatchArb, dispatchArb),
    relTypeArb.chain((rt) => fc.tuple(linkArb(fc.constant(rt)), linkArb(fc.constant(rt))))
  )

  // -------------------------------------------------------------------------
  // a tiny hand-written Cypher reader — the oracle side of L1/L2/L5
  // -------------------------------------------------------------------------
  /** Labels a node pattern introduces: `(v:Label ...)`. */
  const labelsIn = (cypher: string): ReadonlyArray<string> =>
    [...cypher.matchAll(/\(\s*\w*\s*:\s*`?([A-Za-z_][A-Za-z0-9_]*)`?/g)].map((m) => m[1]!).sort()

  /** Relationship types a pattern introduces: `[v:REL]`. */
  const relsIn = (cypher: string): ReadonlyArray<string> =>
    [...cypher.matchAll(/\[\s*\w*\s*:\s*`?([A-Za-z_][A-Za-z0-9_]*)`?/g)].map((m) => m[1]!).sort()

  const uniqSorted = (xs: ReadonlyArray<string>) => Array.from(new Set(xs)).sort()

  /** The clause keyword governing the text at `idx`. */
  const clauseAt = (cypher: string, idx: number): string => {
    const kws = [
      ...cypher.slice(0, idx).matchAll(/\b(MATCH|MERGE|CREATE|SET|RETURN|UNWIND|WITH)\b/g)
    ]
    return kws.length === 0 ? "" : kws[kws.length - 1]![1]!
  }

  const occurrences = (hay: string, needle: string): ReadonlyArray<number> => {
    const out: number[] = []
    let i = hay.indexOf(needle)
    while (i !== -1) {
      out.push(i)
      i = hay.indexOf(needle, i + 1)
    }
    return out
  }

  /** MERGE clauses carrying a property map — i.e. those able to bring a node into being. */
  const nodeCreatingMerges = (cypher: string): ReadonlyArray<string> =>
    [...cypher.matchAll(/MERGE\s*\(\s*\w*\s*(?::\s*`?\w+`?\s*)?\{([^}]*)\}\s*\)/g)].map((m) => m[1]!)

  const propsOf = (params: Record<string, unknown>): Record<string, unknown> =>
    (params["props"] ?? {}) as Record<string, unknown>

  // -------------------------------------------------------------------------

  it("L1: declared labels and relTypes are exactly those the Cypher text introduces", () => {
    fc.assert(
      fc.property(anyIntentArb, provArb, nowArb, (intent, prov, now) => {
        const s = plan(intent, prov, now)
        expect(labelsIn(s.cypher)).toEqual(uniqSorted(s.labels))
        expect(relsIn(s.cypher)).toEqual(uniqSorted(s.relTypes))
        expect(s.labels.length).toBe(new Set(s.labels).size)
        expect(s.relTypes.length).toBe(new Set(s.relTypes).size)
      })
    )
  })

  it("L2: no endpoint is ever MERGEd into existence; the only created node is the target", () => {
    const endpointRefs = ["$hub", "$parent", "$from", "$to", "$children", "childName"]
    fc.assert(
      fc.property(anyIntentArb, provArb, nowArb, (intent, prov, now) => {
        const c = plan(intent, prov, now).cypher
        // additive only
        expect(/\b(CREATE|DELETE|DETACH|REMOVE)\b/.test(c)).toBe(false)
        // every endpoint reference sits under MATCH/UNWIND, never MERGE
        for (const ref of endpointRefs) {
          for (const i of occurrences(c, ref)) expect(clauseAt(c, i)).not.toBe("MERGE")
        }
        // at most one property-bearing MERGE, and it keys on $name
        const creates = nodeCreatingMerges(c)
        expect(creates.length).toBeLessThanOrEqual(1)
        for (const propMap of creates) expect(propMap.replace(/\s+/g, "")).toBe("name:$name")
      })
    )
  })

  it("L3: every node-producing statement stamps actor / run_id / written_at", () => {
    fc.assert(
      fc.property(nodeProducingArb, provArb, nowArb, (intent, prov, now) => {
        const s = plan(intent, prov, now)
        const props = propsOf(s.params)
        expect(props["actor"]).toBe(prov.actor)
        expect(props["run_id"]).toBe(prov.runId)
        expect(props["written_at"]).toBe(now)
        expect(String(props["provenance"])).toContain(prov.runId)
        expect(String(props["provenance"])).toContain(prov.actor)
        expect(s.cypher).toMatch(/\+=\s*\$props/)
      })
    )
  })

  it("L3b: LinkNodes stamps the edge instead, and writes no node properties", () => {
    fc.assert(
      fc.property(linkArb(relTypeArb), provArb, nowArb, (intent, prov, now) => {
        const s = plan(intent, prov, now)
        expect(s.params["props"]).toBeUndefined()
        expect(s.params["runId"]).toBe(prov.runId)
        expect(s.params["actor"]).toBe(prov.actor)
        expect(s.cypher).toContain("r.run_id = $runId")
        expect(s.cypher).toContain("r.actor = $actor")
      })
    )
  })

  it("L4: exactly one citation property is set, and it matches the union tag", () => {
    fc.assert(
      fc.property(nodeProducingArb, provArb, nowArb, (intent, prov, now) => {
        const props = propsOf(plan(intent, prov, now).params)
        const hasUrl = "citation_url" in props
        const hasReason = "no_external_citation_reason" in props
        expect(hasUrl !== hasReason).toBe(true) // exclusive or
        const citation = (intent as { citation: CitationEvidence }).citation
        expect(hasUrl).toBe(citation._tag === "Url")
        if (citation._tag === "Url") expect(props["citation_url"]).toBe(citation.citation_url)
        else expect(props["no_external_citation_reason"]).toBe(citation.no_external_citation_reason)
      })
    )
  })

  it("L5: expectNodes = [target] for node-producing intents, [] for LinkNodes", () => {
    fc.assert(
      fc.property(anyIntentArb, provArb, nowArb, (intent, prov, now) => {
        const s = plan(intent, prov, now)
        if (intent._tag === "LinkNodes") {
          expect(s.expectNodes).toEqual([])
          expect(s.labels).toEqual([])
          expect(s.target).toBe(`${intent.from} -[:${intent.relType}]-> ${intent.to}`)
        } else {
          expect(s.expectNodes).toEqual([intent.name])
          expect(s.target).toBe(intent.name)
          expect(s.params["name"]).toBe(intent.name)
          expect(s.labels.length).toBe(1)
        }
        // readback only ever asks for nodes this statement can actually create
        expect(s.expectNodes.length).toBe(nodeCreatingMerges(s.cypher).length)
      })
    )
  })

  it("L6: the Cypher text is a function of shape alone — data flows only through params", () => {
    fc.assert(
      fc.property(sameShapePairArb, provArb, provArb, nowArb, nowArb, ([a, b], pa, pb, na, nb) => {
        expect(plan(a, pa, na).cypher).toBe(plan(b, pb, nb).cypher)
      })
    )
  })

  it("L7: relType is the sole interpolated token, backtick-quoted, and unquotable input is refused", () => {
    fc.assert(
      fc.property(linkArb(relTypeArb), provArb, nowArb, (intent, prov, now) => {
        const rel = (intent as { relType: string }).relType
        const s = plan(intent, prov, now)
        expect(rel).not.toContain("`")
        expect(s.cypher).toContain("`" + rel + "`")
        expect(s.relTypes).toEqual([rel])
        expect(Either.isRight(Schema.decodeUnknownEither(LinkNodes)(intent))).toBe(true)
      })
    )
    // anything outside SCREAMING_SNAKE_CASE — backticks, spaces, lowercase — never reaches plan()
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 24 }).filter((s) => !/^[A-Z][A-Z0-9_]*$/.test(s)),
        nodeName,
        nodeName,
        (relType, from, to) => {
          const r = Schema.decodeUnknownEither(LinkNodes)({ _tag: "LinkNodes", from, to, relType })
          expect(Either.isLeft(r)).toBe(true)
        }
      )
    )
  })

  it("L8: WriteBatch accepts 1..50 intents, rejects 0 and >50; dryRun defaults to true", () => {
    const decode = Schema.decodeUnknownEither(WriteBatch)
    const encode = (i: WriteIntent): Record<string, unknown> => {
      const o: Record<string, unknown> = { ...i }
      for (const k of Object.keys(o)) if (o[k] === undefined) delete o[k]
      return o
    }
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 60 }),
        anyIntentArb,
        provArb,
        fc.option(fc.boolean(), { nil: undefined }),
        (n, intent, prov, dryRun) => {
          const body: Record<string, unknown> = {
            provenance: prov,
            intents: Array.from({ length: n }, () => encode(intent))
          }
          if (dryRun !== undefined) body["dryRun"] = dryRun

          const r = decode(body)
          expect(Either.isRight(r)).toBe(n >= 1 && n <= 50)
          if (Either.isRight(r)) {
            expect(r.right.dryRun).toBe(dryRun === undefined ? true : dryRun)
            expect(r.right.intents.length).toBe(n)
          }
        }
      )
    )
  })
})

describe("Traversal.walk — properties", () => {
  // --- the world the walk is driven through -------------------------------
  // A randomly generated adjacency map. `""` and `ghost0` are deliberate:
  // an empty neighbour name must never become a node, and a name with no
  // adjacency entry must terminate a branch rather than throw.
  interface Edge {
    readonly name: string
    readonly type: string
    readonly direction: "out" | "in"
  }
  type Adjacency = Record<string, ReadonlyArray<Edge>>

  const NODES = ["n0", "n1", "n2", "n3", "n4", "n5", "n6"] as const
  const TARGETS = [...NODES, "ghost0", ""] as const
  const TYPES = ["REL", "LINK", "MENTIONS"] as const

  const edgeArb: fc.Arbitrary<Edge> = fc.record({
    name: fc.constantFrom(...TARGETS),
    type: fc.constantFrom(...TYPES),
    direction: fc.constantFrom<"out" | "in">("out", "in")
  })

  /** One page per node, deduplicated by target so a page holds distinct names. */
  const adjacencyArb: fc.Arbitrary<Adjacency> = fc
    .array(fc.array(edgeArb, { maxLength: 5 }), {
      minLength: NODES.length,
      maxLength: NODES.length
    })
    .map((pages) =>
      Object.fromEntries(
        NODES.map((n, i) => {
          const seen = new Set<string>()
          const page = (pages[i] ?? []).filter((e) =>
            seen.has(e.name) ? false : (seen.add(e.name), true)
          )
          return [n, page]
        })
      )
    )

  const seedArb = fc.constantFrom(...NODES, "ghost0")

  const fakeKg = (adjacency: Adjacency): Layer.Layer<KgPortTag> =>
    Layer.succeed(KgPortTag, {
      ...({} as KgPort),
      neighbors: ({ name, limit }) => {
        const all = adjacency[name] ?? []
        const page = all.slice(0, limit)
        return Effect.succeed(
          new NodeNeighbors({
            name,
            found: name in adjacency,
            degree: all.length,
            neighbors: page.map(
              (e) =>
                new GraphNeighbor({
                  direction: e.direction,
                  type: e.type,
                  name: e.name,
                  labels: []
                })
            ),
            truncated: all.length > page.length
          })
        )
      }
    } as KgPort)

  const walk = (
    adjacency: Adjacency,
    seed: string,
    opts: Partial<Traversal.WalkOptions> = {}
  ): Promise<Traversal.WalkResult> =>
    Effect.runPromise(Effect.provide(Traversal.walk(seed, opts), fakeKg(adjacency)))

  /** Bounds wide enough that only the graph, never the budget, stops the walk. */
  const generous = { maxNodes: 1000, branching: 100 }

  const names = (r: Traversal.WalkResult) => r.visited.map((v) => v.name)

  // ------------------------------------------------------------------------

  it("the seed opens the walk exactly once and no node is visited twice", async () => {
    await fc.assert(
      fc.asyncProperty(
        adjacencyArb,
        seedArb,
        fc.integer({ min: 0, max: 4 }),
        fc.integer({ min: 1, max: 12 }),
        fc.integer({ min: 1, max: 6 }),
        async (adjacency, seed, maxDepth, maxNodes, branching) => {
          const r = await walk(adjacency, seed, { maxDepth, maxNodes, branching })
          expect(r.seed).toBe(seed)
          expect(r.visited[0]?.name).toBe(seed)
          expect(r.visited[0]?.depth).toBe(0)
          expect(r.visited[0]?.via).toBeUndefined()
          const ns = names(r)
          expect(new Set(ns).size).toBe(ns.length)
          expect(ns.filter((n) => n === "")).toEqual([])
        }
      )
    )
  })

  it("via edges form a tree rooted at the seed, each parent visited earlier and one hop shallower", async () => {
    await fc.assert(
      fc.asyncProperty(
        adjacencyArb,
        seedArb,
        fc.integer({ min: 0, max: 4 }),
        fc.integer({ min: 1, max: 12 }),
        fc.integer({ min: 1, max: 6 }),
        async (adjacency, seed, maxDepth, maxNodes, branching) => {
          const r = await walk(adjacency, seed, { maxDepth, maxNodes, branching })
          const index = new Map(r.visited.map((v, i) => [v.name, i]))
          r.visited.forEach((v, i) => {
            if (i === 0) return
            expect(v.via).toBeDefined()
            const parent = index.get(v.via!.from)
            expect(parent).toBeDefined()
            expect(parent!).toBeLessThan(i)
            expect(v.depth).toBe(r.visited[parent!]!.depth + 1)
            // breadth-first: the layer index never goes backwards
            expect(v.depth).toBeGreaterThanOrEqual(r.visited[i - 1]!.depth)
          })
        }
      )
    )
  })

  it("every declared bound actually bounds", async () => {
    await fc.assert(
      fc.asyncProperty(
        adjacencyArb,
        seedArb,
        fc.integer({ min: 0, max: 4 }),
        fc.integer({ min: 1, max: 12 }),
        fc.integer({ min: 1, max: 6 }),
        async (adjacency, seed, maxDepth, maxNodes, branching) => {
          const r = await walk(adjacency, seed, { maxDepth, maxNodes, branching })
          expect(r.visited.length).toBeLessThanOrEqual(maxNodes)
          for (const v of r.visited) expect(v.depth).toBeLessThanOrEqual(maxDepth)

          const fanOut = new Map<string, number>()
          for (const v of r.visited) {
            if (v.via === undefined) continue
            fanOut.set(v.via.from, (fanOut.get(v.via.from) ?? 0) + 1)
          }
          for (const count of fanOut.values()) expect(count).toBeLessThanOrEqual(branching)

          // unexplored is a report, so it is deduplicated and ordered
          expect(new Set(r.unexplored).size).toBe(r.unexplored.length)
          expect([...r.unexplored]).toEqual([...r.unexplored].sort())
        }
      )
    )
  })

  it("the stop reason accounts for the leftovers: Exhausted means nothing was left behind", async () => {
    await fc.assert(
      fc.asyncProperty(
        adjacencyArb,
        seedArb,
        fc.integer({ min: 0, max: 4 }),
        fc.integer({ min: 1, max: 12 }),
        fc.integer({ min: 1, max: 6 }),
        async (adjacency, seed, maxDepth, maxNodes, branching) => {
          const r = await walk(adjacency, seed, { maxDepth, maxNodes, branching })
          if (r.stoppedBecause === "Exhausted") {
            expect(r.unexplored).toEqual([])
            for (const v of r.visited) expect(v.depth).toBeLessThan(maxDepth)
          }
          // nothing is ever abandoned for a reason other than the node budget
          if (r.unexplored.length > 0) expect(r.stoppedBecause).toBe("MaxNodes")
        }
      )
    )
  })

  it("Exhausted means the visited set is closed under the adjacency it was allowed to see", async () => {
    await fc.assert(
      fc.asyncProperty(
        adjacencyArb,
        seedArb,
        fc.integer({ min: 0, max: 4 }),
        fc.integer({ min: 1, max: 12 }),
        fc.integer({ min: 1, max: 6 }),
        async (adjacency, seed, maxDepth, maxNodes, branching) => {
          const r = await walk(adjacency, seed, { maxDepth, maxNodes, branching })
          fc.pre(r.stoppedBecause === "Exhausted")
          const inside = new Set(names(r))
          for (const v of r.visited) {
            for (const e of (adjacency[v.name] ?? []).slice(0, branching)) {
              if (e.name === "") continue
              expect(inside.has(e.name)).toBe(true)
            }
          }
        }
      )
    )
  })

  it("agrees node-for-node with a hand-written breadth-first model", async () => {
    // The model knows only the documented rules: breadth-first, dedupe against
    // everything seen, drop blank names, prefer outgoing edges, stop expanding
    // at maxDepth. It shares no code with the implementation.
    const model = (
      adjacency: Adjacency,
      seed: string,
      maxDepth: number
    ): ReadonlyArray<{ name: string; depth: number }> => {
      const seen = new Set([seed])
      const order = [{ name: seed, depth: 0 }]
      const queue = [{ name: seed, depth: 0 }]
      while (queue.length > 0) {
        const cur = queue.shift()!
        if (cur.depth >= maxDepth) continue
        const page = (adjacency[cur.name] ?? []).filter((e) => e.name !== "")
        const ranked = [
          ...page.filter((e) => e.direction === "out"),
          ...page.filter((e) => e.direction === "in")
        ]
        for (const e of ranked) {
          if (seen.has(e.name)) continue
          seen.add(e.name)
          const next = { name: e.name, depth: cur.depth + 1 }
          order.push(next)
          queue.push(next)
        }
      }
      return order
    }

    await fc.assert(
      fc.asyncProperty(
        adjacencyArb,
        seedArb,
        fc.integer({ min: 0, max: 4 }),
        async (adjacency, seed, maxDepth) => {
          const r = await walk(adjacency, seed, { ...generous, maxDepth })
          const expected = model(adjacency, seed, maxDepth)
          expect(r.visited.map((v) => ({ name: v.name, depth: v.depth }))).toEqual([...expected])
          expect(r.stoppedBecause).toBe(
            expected.some((v) => v.depth >= maxDepth) ? "MaxDepth" : "Exhausted"
          )
        }
      )
    )
  })

  it("a tighter node budget only truncates: the walk is a prefix of the unbudgeted one", async () => {
    await fc.assert(
      fc.asyncProperty(
        adjacencyArb,
        seedArb,
        fc.integer({ min: 0, max: 4 }),
        fc.integer({ min: 1, max: 8 }),
        async (adjacency, seed, maxDepth, maxNodes) => {
          const tight = await walk(adjacency, seed, { ...generous, maxDepth, maxNodes })
          const full = await walk(adjacency, seed, { ...generous, maxDepth })
          expect(tight.visited.length).toBe(Math.min(maxNodes, full.visited.length))
          expect(names(tight)).toEqual(names(full).slice(0, tight.visited.length))
        }
      )
    )
  })

  it("a refused edge is never traversed, whether refused by type or by score", async () => {
    await fc.assert(
      fc.asyncProperty(
        adjacencyArb,
        seedArb,
        fc.integer({ min: 1, max: 4 }),
        fc.constantFrom(...TYPES),
        async (adjacency, seed, maxDepth, banned) => {
          const full = await walk(adjacency, seed, { ...generous, maxDepth })
          const filtered = await walk(adjacency, seed, {
            ...generous,
            maxDepth,
            excludeTypes: [banned]
          })
          for (const v of filtered.visited) expect(v.via?.type).not.toBe(banned)
          // removing edges can only shrink the neighbourhood, never reroute it
          const reachable = new Set(names(full))
          for (const n of names(filtered)) expect(reachable.has(n)).toBe(true)

          const refused = await walk(adjacency, seed, {
            ...generous,
            maxDepth,
            score: () => -1
          })
          expect(names(refused)).toEqual([seed])
          expect(refused.stoppedBecause).toBe("Exhausted")
          expect(refused.unexplored).toEqual([])
        }
      )
    )
  })
})
