/**
 * Graph-engineering tests.
 *
 * These are the properties the old `Loop.ts` could not even state, because a
 * sequence over an opaque state has no dependencies, no shared resources and
 * no subtree to isolate a failure to.
 */
import { Effect, Layer, Ref } from "effect"
import { describe, expect, it } from "vitest"
import * as Cmd from "../src/agent/Commanders.js"
import * as Loop from "../src/agent/Loop.js"
import * as Scheduler from "../src/agent/Scheduler.js"
import * as Traversal from "../src/agent/Traversal.js"
import * as G from "../src/agent/WorkGraph.js"
import { GraphNeighbor, NodeNeighbors } from "../src/domain/Contracts.js"
import { KgPortSnapshot, KgPortTag, type KgPort } from "../src/ports/KgPort.js"

const run = <A, E>(e: Effect.Effect<A, E, never>) => Effect.runPromise(e)

const node = (
  id: string,
  dependsOn: ReadonlyArray<string> = [],
  writeSet: ReadonlyArray<string> = []
): G.WorkNode<string> => ({ id, payload: id, dependsOn, writeSet })

// ---------------------------------------------------------------------------

describe("WorkGraph — topological order", () => {
  it("orders a diamond so both middles follow the root and precede the join", () => {
    const g = G.fromNodes([node("a"), node("b", ["a"]), node("c", ["a"]), node("d", ["b", "c"])])
    const r = G.topoSort(g)
    expect(r._tag).toBe("Ordered")
    if (r._tag !== "Ordered") return
    const pos = (id: string) => r.order.indexOf(id)
    expect(pos("a")).toBeLessThan(pos("b"))
    expect(pos("a")).toBeLessThan(pos("c"))
    expect(pos("d")).toBeGreaterThan(pos("b"))
    expect(pos("d")).toBeGreaterThan(pos("c"))
  })

  it("reports a cycle as data instead of deadlocking", () => {
    const g = G.fromNodes([node("x", ["z"]), node("y", ["x"]), node("z", ["y"])])
    const r = G.topoSort(g)
    expect(r._tag).toBe("Cyclic")
    if (r._tag !== "Cyclic") return
    expect(r.involved).toEqual(["x", "y", "z"])
  })

  it("finds dependency edges pointing outside the graph", () => {
    const g = G.fromNodes([node("a", ["ghost"])])
    expect(G.danglingEdges(g)).toEqual([{ from: "a", to: "ghost" }])
  })
})

describe("WorkGraph — readySet", () => {
  it("gates on dependencies", () => {
    const g = G.fromNodes([node("a"), node("b", ["a"])])
    const s = G.initialRunState()
    expect(G.readySet(g, s)).toEqual(["a"])
    expect(G.readySet(g, { ...s, done: new Set(["a"]) })).toEqual(["b"])
  })

  it("never returns two nodes whose write-sets intersect", () => {
    // The DAG allows both — only the lease forbids it. This is the rule that
    // a flat `Effect.all(tasks)` has no way to express.
    const g = G.fromNodes([node("a", [], ["kg:findings"]), node("b", [], ["kg:findings"])])
    expect(G.readySet(g, G.initialRunState())).toEqual(["a"])
  })

  it("allows disjoint write-sets to run together", () => {
    const g = G.fromNodes([node("a", [], ["kg:findings"]), node("b", [], ["kg:lessons"])])
    expect(G.readySet(g, G.initialRunState())).toEqual(["a", "b"])
  })

  it("withholds a node whose lease is held by something already running", () => {
    const g = G.fromNodes([node("a", [], ["shared"]), node("b", [], ["shared"])])
    const s = { ...G.initialRunState(), running: new Set(["a"]) }
    expect(G.readySet(g, s)).toEqual([])
  })

  it("read-only nodes are never blocked by a lease", () => {
    const g = G.fromNodes([node("w", [], ["shared"]), node("r1"), node("r2")])
    const s = { ...G.initialRunState(), running: new Set(["w"]) }
    expect(G.readySet(g, s)).toEqual(["r1", "r2"])
  })
})

describe("WorkGraph — failure reach", () => {
  it("descendants covers the transitive subtree only", () => {
    const g = G.fromNodes([
      node("root"),
      node("mid", ["root"]),
      node("leaf", ["mid"]),
      node("sibling")
    ])
    expect(Array.from(G.descendants(g, "root")).sort()).toEqual(["leaf", "mid"])
    expect(Array.from(G.descendants(g, "sibling"))).toEqual([])
  })

  it("newlyBlocked skips nodes that already completed", () => {
    const g = G.fromNodes([node("a"), node("b", ["a"]), node("c", ["b"])])
    const s = { ...G.initialRunState(), done: new Set(["b"]) }
    expect(Array.from(G.newlyBlocked(g, s, "a"))).toEqual(["c"])
  })
})

// ---------------------------------------------------------------------------

describe("Scheduler", () => {
  it("refuses a cyclic graph before running anything", async () => {
    const g = G.fromNodes([node("x", ["y"]), node("y", ["x"])])
    let ran = 0
    const e = await run(
      Effect.flip(
        Scheduler.run(
          g,
          () => {
            ran += 1
            return Effect.succeed(1)
          },
          { concurrency: 4 }
        )
      )
    )
    expect(e._tag).toBe("CyclicGraph")
    expect(ran).toBe(0)
  })

  it("refuses a dangling dependency", async () => {
    const e = await run(
      Effect.flip(
        Scheduler.run(G.fromNodes([node("a", ["ghost"])]), () => Effect.succeed(1), {
          concurrency: 2
        })
      )
    )
    expect(e._tag).toBe("DanglingDependency")
  })

  it("runs dependencies before dependents", async () => {
    const order: Array<string> = []
    const g = G.fromNodes([node("a"), node("b", ["a"]), node("c", ["b"])])
    await run(
      Scheduler.run(
        g,
        (n) =>
          Effect.sync(() => {
            order.push(n.id)
            return n.id
          }),
        { concurrency: 4 }
      ) as Effect.Effect<unknown, never, never>
    )
    expect(order).toEqual(["a", "b", "c"])
  })

  it("passes upstream results down to dependents", async () => {
    const g = G.fromNodes([node("a"), node("b", ["a"])])
    const report = await run(
      Scheduler.run(
        g,
        (n, upstream) => Effect.succeed(n.id === "a" ? "A" : `saw:${upstream.get("a")}`),
        { concurrency: 2 }
      ) as Effect.Effect<Scheduler.RunReport<string, never>, never, never>
    )
    const b = report.outcomes.get("b")
    expect(b?._tag).toBe("Completed")
    expect(b?._tag === "Completed" && b.value).toBe("saw:A")
  })

  it("respects the concurrency bound", async () => {
    const g = G.fromNodes(Array.from({ length: 10 }, (_, i) => node(`n${i}`)))
    const report = await run(
      Effect.gen(function* () {
        const live = yield* Ref.make(0)
        const peak = yield* Ref.make(0)
        return yield* Scheduler.run(
          g,
          () =>
            Effect.gen(function* () {
              const n = yield* Ref.updateAndGet(live, (x) => x + 1)
              yield* Ref.update(peak, (p) => Math.max(p, n))
              yield* Effect.yieldNow()
              yield* Ref.update(live, (x) => x - 1)
              return n
            }),
          { concurrency: 3 }
        )
      }) as Effect.Effect<Scheduler.RunReport<number, never>, never, never>
    )
    expect(report.completed).toHaveLength(10)
    expect(report.peakConcurrency).toBeLessThanOrEqual(3)
  })

  it("a failure blocks its subtree and leaves the rest of the graph running", async () => {
    // This is the behaviour `Effect.all` cannot give: one bad branch is not a
    // bad run.
    const g = G.fromNodes([
      node("bad"),
      node("child", ["bad"]),
      node("grandchild", ["child"]),
      node("independent")
    ])
    const report = await run(
      Scheduler.run(
        g,
        (n) => (n.id === "bad" ? Effect.fail("boom" as const) : Effect.succeed(n.id)),
        { concurrency: 4 }
      ) as Effect.Effect<Scheduler.RunReport<string, "boom">, never, never>
    )
    expect(report.failed).toEqual(["bad"])
    expect(report.blocked).toEqual(["child", "grandchild"])
    expect(report.completed).toEqual(["independent"])

    const blocked = report.outcomes.get("grandchild")
    expect(blocked?._tag === "Blocked" && blocked.by).toBe("bad")
  })
})

// ---------------------------------------------------------------------------

describe("Loop", () => {
  const exec = (n: G.WorkNode<string>) => Effect.succeed(n.id)

  it("stops at FrontierEmpty when there is nothing to discover", async () => {
    const r = await run(
      Loop.run(G.fromNodes([node("a"), node("b", ["a"])]), { execute: exec })
    )
    expect(r.stoppedBecause._tag).toBe("FrontierEmpty")
    expect(r.rounds).toBe(1)
  })

  it("converges after K dry rounds", async () => {
    const r = await run(
      Loop.run(G.fromNodes([node("a")]), {
        execute: exec,
        discover: () => Effect.succeed([]),
        budget: { dryRounds: 2, maxRounds: 10 }
      })
    )
    expect(r.stoppedBecause).toEqual({ _tag: "Dry", rounds: 2 })
    expect(r.rounds).toBe(2)
  })

  it("dedups against everything SEEN, not against the live graph", async () => {
    // The failure this guards: dedup against kept/succeeded nodes makes a
    // rejected node reappear every round and the loop never terminates.
    let proposals = 0
    const r = await run(
      Loop.run(G.fromNodes([node("a")]), {
        execute: exec,
        discover: () => {
          proposals += 1
          return Effect.succeed([node("a"), node("seen-once")]) // "a" already admitted
        },
        budget: { dryRounds: 2, maxRounds: 10 }
      })
    )
    // round 1 admits "seen-once"; round 2+ propose only duplicates → dry
    expect(r.admitted).toBe(2)
    expect(r.stoppedBecause._tag).toBe("Dry")
    expect(proposals).toBeGreaterThanOrEqual(2)
  })

  it("stops at the node budget rather than growing without bound", async () => {
    let i = 0
    const r = await run(
      Loop.run(G.fromNodes([node("seed")]), {
        execute: exec,
        discover: () =>
          Effect.succeed(Array.from({ length: 10 }, () => node(`gen-${i++}`))),
        budget: { maxNodes: 5, maxRounds: 10 }
      })
    )
    expect(r.stoppedBecause._tag).toBe("NodeBudget")
    expect(r.admitted).toBeLessThanOrEqual(5)
  })

  it("stops at the round budget", async () => {
    let i = 0
    const r = await run(
      Loop.run(G.fromNodes([node("seed")]), {
        execute: exec,
        discover: () => Effect.succeed([node(`r-${i++}`)]),
        budget: { maxRounds: 3, maxNodes: 100, dryRounds: 5 }
      })
    )
    expect(r.rounds).toBe(3)
    expect(r.stoppedBecause._tag).toBe("RoundBudget")
  })

  it("reports an unrunnable graph instead of hanging", async () => {
    const r = await run(
      Loop.run(G.fromNodes([node("x", ["y"]), node("y", ["x"])]), { execute: exec })
    )
    expect(r.stoppedBecause).toEqual({ _tag: "GraphUnrunnable", reason: "CyclicGraph" })
  })
})

// ---------------------------------------------------------------------------

describe("Commanders — measurement-driven conditional dispatch", () => {
  interface Ctx {
    readonly trail: ReadonlyArray<string>
  }

  const commander = (
    name: string,
    value: number,
    escalateTo: string,
    threshold = 0.7
  ): Cmd.Commander<Ctx, never, never> => ({
    name,
    act: (ctx) => Effect.succeed({ trail: [...ctx.trail, name] }),
    measure: () => Effect.succeed({ metric: `${name}.confidence`, value, threshold, direction: "below" }),
    escalateTo
  })

  it("crossed() reads the direction correctly", () => {
    expect(Cmd.crossed({ metric: "m", value: 0.5, threshold: 0.7, direction: "below" })).toBe(true)
    expect(Cmd.crossed({ metric: "m", value: 0.9, threshold: 0.7, direction: "below" })).toBe(false)
    expect(Cmd.crossed({ metric: "m", value: 0.9, threshold: 0.7, direction: "above" })).toBe(true)
  })

  it("converges when the metric does not cross — no escalation", async () => {
    // occam.confidence 0.95 >= 0.7 → 나생문 is NOT called.
    const registry = Cmd.roster([commander("occam", 0.95, "naesengmoon")])
    const t = await run(Cmd.dispatchFrom(registry, "occam", { trail: [] }, { maxHops: 5 }))
    expect(t.stoppedBecause).toBe("Converged")
    expect(t.context.trail).toEqual(["occam"])
    expect(t.path[0]?.escalated).toBe(false)
  })

  it("escalates when the metric crosses — the edge is computed, not fixed", async () => {
    // occam.confidence 0.4 < 0.7 → dispatch 나생문 (canonical example).
    const registry = Cmd.roster([
      commander("occam", 0.4, "naesengmoon"),
      commander("naesengmoon", 0.99, "longinus")
    ])
    const t = await run(Cmd.dispatchFrom(registry, "occam", { trail: [] }, { maxHops: 5 }))
    expect(t.context.trail).toEqual(["occam", "naesengmoon"])
    expect(t.path[0]?.next).toBe("naesengmoon")
    expect(t.stoppedBecause).toBe("Converged")
  })

  it("refuses to revisit a commander — 'the metric will settle' is not a proof", async () => {
    const registry = Cmd.roster([
      commander("occam", 0.1, "naesengmoon"),
      commander("naesengmoon", 0.1, "occam")
    ])
    const t = await run(Cmd.dispatchFrom(registry, "occam", { trail: [] }, { maxHops: 20 }))
    expect(t.stoppedBecause).toBe("Cycle")
    expect(t.path.at(-1)?.refusedBecause).toBe("AlreadyVisited")
  })

  it("honours the hop budget when revisits are permitted", async () => {
    const registry = Cmd.roster([
      commander("a", 0.1, "b"),
      commander("b", 0.1, "a")
    ])
    const t = await run(
      Cmd.dispatchFrom(registry, "a", { trail: [] }, { maxHops: 4, allowRevisit: true })
    )
    expect(t.stoppedBecause).toBe("BudgetExhausted")
    expect(t.path).toHaveLength(4)
  })

  it("names an unknown escalation target instead of silently stopping", async () => {
    const registry = Cmd.roster([commander("occam", 0.1, "does-not-exist")])
    const t = await run(Cmd.dispatchFrom(registry, "occam", { trail: [] }, { maxHops: 5 }))
    expect(t.stoppedBecause).toBe("UnknownCommander")
    expect(t.path[0]?.refusedBecause).toBe("UnknownCommander")
  })
})

// ---------------------------------------------------------------------------

describe("Traversal", () => {
  /** A hand-built adjacency map standing in for the KG. */
  const fakeKg = (adjacency: Record<string, ReadonlyArray<string>>): Layer.Layer<KgPortTag> =>
    Layer.succeed(KgPortTag, {
      ...({} as KgPort),
      neighbors: ({ name, limit }) =>
        Effect.succeed(
          new NodeNeighbors({
            name,
            found: name in adjacency,
            degree: (adjacency[name] ?? []).length,
            neighbors: (adjacency[name] ?? [])
              .slice(0, limit)
              .map((n) => new GraphNeighbor({ direction: "out", type: "REL", name: n, labels: [] })),
            truncated: (adjacency[name] ?? []).length > limit
          })
        )
    } as KgPort)

  const walk = (adjacency: Record<string, ReadonlyArray<string>>, seed: string, opts = {}) =>
    run(Effect.provide(Traversal.walk(seed, opts), fakeKg(adjacency)))

  it("visits breadth-first and records the arriving edge", async () => {
    const r = await walk({ a: ["b", "c"], b: ["d"], c: [], d: [] }, "a")
    expect(r.visited.map((v) => v.name)).toEqual(["a", "b", "c", "d"])
    expect(r.visited.find((v) => v.name === "d")?.via?.from).toBe("b")
    expect(r.stoppedBecause).toBe("Exhausted")
  })

  it("never revisits a node in a cyclic graph", async () => {
    const r = await walk({ a: ["b"], b: ["c"], c: ["a"] }, "a")
    expect(r.visited.map((v) => v.name).sort()).toEqual(["a", "b", "c"])
  })

  it("respects maxDepth", async () => {
    const r = await walk({ a: ["b"], b: ["c"], c: ["d"], d: [] }, "a", { maxDepth: 2 })
    expect(r.visited.map((v) => v.name)).toEqual(["a", "b", "c"])
    expect(r.stoppedBecause).toBe("MaxDepth")
  })

  it("reports what a tight node budget cost, instead of truncating silently", async () => {
    const r = await walk({ a: ["b", "c", "d", "e"], b: [], c: [], d: [], e: [] }, "a", {
      maxNodes: 3
    })
    expect(r.visited).toHaveLength(3)
    expect(r.unexplored.length).toBeGreaterThan(0)
    expect(r.stoppedBecause).toBe("MaxNodes")
  })

  it("turns a walk into a work graph whose edges follow discovery", async () => {
    const r = await walk({ a: ["b"], b: ["c"], c: [] }, "a")
    const g = G.fromNodes(Traversal.toWorkNodes(r))
    expect(G.danglingEdges(g)).toEqual([])
    const sorted = G.topoSort(g)
    expect(sorted._tag).toBe("Ordered")
    if (sorted._tag === "Ordered") expect(sorted.order).toEqual(["a", "b", "c"])
  })

  it("the snapshot KG yields a lone seed rather than an error", async () => {
    const r = await run(Effect.provide(Traversal.walk("anything"), KgPortSnapshot))
    expect(r.visited.map((v) => v.name)).toEqual(["anything"])
    expect(r.stoppedBecause).toBe("Exhausted")
  })
})
