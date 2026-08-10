/**
 * The DAG scheduler — what `fanOut(tasks, concurrency)` should have been.
 *
 * `Effect.all` over a flat array is a *star* graph: one parent, N independent
 * children, no ordering, no shared resources, all-or-nothing failure. Real
 * dispatch is none of those things. This executor:
 *
 *   - starts a node only when its dependencies are done,
 *   - never runs two nodes whose write-sets intersect,
 *   - drops a failed node's **descendants** and keeps the rest of the graph
 *     running (a bad branch is not a bad run),
 *   - bounds concurrency, and
 *   - reports every node's fate, including *why* a node never ran.
 *
 * The scheduling decisions themselves live in `WorkGraph` as pure functions.
 * This file only supplies fibers and a clock.
 */
import { Effect, Fiber, Ref } from "effect"
import * as G from "./WorkGraph.js"

export type Outcome<A, E> =
  | { readonly _tag: "Completed"; readonly value: A }
  | { readonly _tag: "Failed"; readonly error: E }
  | { readonly _tag: "Blocked"; readonly by: G.NodeId }

export interface RunReport<A, E> {
  readonly outcomes: ReadonlyMap<G.NodeId, Outcome<A, E>>
  readonly completed: ReadonlyArray<G.NodeId>
  readonly failed: ReadonlyArray<G.NodeId>
  readonly blocked: ReadonlyArray<G.NodeId>
  /** Peak simultaneous fibers. Lets a test assert the bound was respected. */
  readonly peakConcurrency: number
}

/** A cyclic graph is refused before anything runs, rather than deadlocking. */
export class CyclicGraph {
  readonly _tag = "CyclicGraph"
  constructor(readonly involved: ReadonlyArray<G.NodeId>) {}
}

/** A dependency edge pointing at a node that is not in the graph. */
export class DanglingDependency {
  readonly _tag = "DanglingDependency"
  constructor(readonly edges: ReadonlyArray<{ readonly from: G.NodeId; readonly to: G.NodeId }>) {}
}

export interface SchedulerOptions {
  readonly concurrency: number
}

/**
 * Execute the graph.
 *
 * `execute` receives the node and the outcomes of everything upstream of it —
 * that is how a child sees its parent's result without threading a mutable
 * blackboard through the run.
 *
 * The returned Effect never fails on a *node* failure; node failures are data
 * in the report. It fails only when the graph itself is unrunnable.
 */
export const run = <T, A, E, R>(
  graph: G.WorkGraph<T>,
  execute: (
    node: G.WorkNode<T>,
    upstream: ReadonlyMap<G.NodeId, A>
  ) => Effect.Effect<A, E, R>,
  options: SchedulerOptions
): Effect.Effect<RunReport<A, E>, CyclicGraph | DanglingDependency, R> =>
  Effect.gen(function* () {
    const dangling = G.danglingEdges(graph)
    if (dangling.length > 0) return yield* Effect.fail(new DanglingDependency(dangling))

    const sorted = G.topoSort(graph)
    if (sorted._tag === "Cyclic") return yield* Effect.fail(new CyclicGraph(sorted.involved))

    const stateRef = yield* Ref.make(G.initialRunState())
    const outcomes = yield* Ref.make(new Map<G.NodeId, Outcome<A, E>>())
    const values = yield* Ref.make(new Map<G.NodeId, A>())
    const peak = yield* Ref.make(0)

    /** Ancestors' successful values, for the node about to run. */
    const upstreamOf = (node: G.WorkNode<T>) =>
      Ref.get(values).pipe(
        Effect.map((all) => {
          const seen = new Map<G.NodeId, A>()
          const stack = [...node.dependsOn]
          while (stack.length > 0) {
            const id = stack.pop()!
            if (seen.has(id)) continue
            const v = all.get(id)
            if (v !== undefined) seen.set(id, v)
            stack.push(...(graph.nodes.get(id)?.dependsOn ?? []))
          }
          return seen as ReadonlyMap<G.NodeId, A>
        })
      )

    const markFailed = (id: G.NodeId, error: E) =>
      Effect.gen(function* () {
        const state = yield* Ref.get(stateRef)
        const cascade = G.newlyBlocked(graph, state, id)

        yield* Ref.update(outcomes, (m) => {
          const next = new Map(m)
          next.set(id, { _tag: "Failed", error })
          for (const blockedId of cascade) {
            if (!next.has(blockedId)) next.set(blockedId, { _tag: "Blocked", by: id })
          }
          return next
        })

        yield* Ref.update(stateRef, (s) => ({
          done: s.done,
          running: new Set(Array.from(s.running).filter((r) => r !== id)),
          blocked: new Set([...s.blocked, id, ...cascade])
        }))
      })

    const markCompleted = (id: G.NodeId, value: A) =>
      Effect.gen(function* () {
        yield* Ref.update(values, (m) => new Map(m).set(id, value))
        yield* Ref.update(outcomes, (m) => new Map(m).set(id, { _tag: "Completed", value }))
        yield* Ref.update(stateRef, (s) => ({
          done: new Set([...s.done, id]),
          running: new Set(Array.from(s.running).filter((r) => r !== id)),
          blocked: s.blocked
        }))
      })

    const start = (id: G.NodeId) =>
      Effect.gen(function* () {
        const node = graph.nodes.get(id)!
        const upstream = yield* upstreamOf(node)
        return yield* Effect.fork(
          Effect.either(execute(node, upstream)).pipe(
            Effect.flatMap((result) =>
              result._tag === "Right"
                ? markCompleted(id, result.right)
                : markFailed(id, result.left)
            ),
            Effect.withSpan("agent.node", { attributes: { nodeId: id } })
          )
        )
      })

    // --- the loop: plan from pure state, run, wait for the first completion --
    let live: Array<Fiber.RuntimeFiber<void, never>> = []

    while (true) {
      const state = yield* Ref.get(stateRef)
      const slots = options.concurrency - state.running.size

      if (slots > 0) {
        const ready = G.readySet(graph, state).slice(0, slots)
        if (ready.length > 0) {
          yield* Ref.update(stateRef, (s) => ({
            ...s,
            running: new Set([...s.running, ...ready])
          }))
          for (const id of ready) live.push(yield* start(id))
          const running = (yield* Ref.get(stateRef)).running.size
          yield* Ref.update(peak, (p) => Math.max(p, running))
          continue
        }
      }

      const current = yield* Ref.get(stateRef)
      if (current.running.size === 0) break

      // Something is in flight but nothing new can start: wait for progress.
      yield* Fiber.join(live[0] ?? (yield* Effect.fork(Effect.void)))
      live = live.slice(1)
    }

    yield* Fiber.joinAll(live)

    const finalOutcomes = yield* Ref.get(outcomes)
    const pick = (tag: Outcome<A, E>["_tag"]) =>
      Array.from(finalOutcomes.entries())
        .filter(([, o]) => o._tag === tag)
        .map(([id]) => id)
        .sort()

    return {
      outcomes: finalOutcomes,
      completed: pick("Completed"),
      failed: pick("Failed"),
      blocked: pick("Blocked"),
      peakConcurrency: yield* Ref.get(peak)
    } satisfies RunReport<A, E>
  }).pipe(Effect.withSpan("agent.scheduler"))
