/**
 * The agent loop — over a graph, not over an opaque state.
 *
 * The first version of this file took `(state: A, index: number) => A` and
 * stopped when `Object.is(prev, next)`. Both are one-dimensional: an integer
 * index says the work is a sequence, and state equality is not what
 * convergence means when the work is a search.
 *
 * What replaces them:
 *
 *   - the unit of work is a `WorkGraph`, executed by the DAG `Scheduler`;
 *   - a round may **grow** the graph (discovery), which is why the loop
 *     exists at all — a static DAG needs no loop, just one scheduler pass;
 *   - convergence is **frontier empty** or **K consecutive dry rounds**, not
 *     state equality;
 *   - deduplication is against everything *seen*, never against everything
 *     *kept* — deduping against the kept set makes rejected nodes reappear
 *     every round and the loop never terminates.
 *
 * The budget survives from the old version, because bounding the work was the
 * one thing it got right.
 */
import { Duration, Effect, Schedule } from "effect"
import * as Scheduler from "./Scheduler.js"
import * as G from "./WorkGraph.js"

export interface LoopBudget {
  /** Hard ceiling on rounds. */
  readonly maxRounds: number
  /** Hard ceiling on total nodes admitted across all rounds. */
  readonly maxNodes: number
  /** Rounds that discover nothing new before the loop calls it converged. */
  readonly dryRounds: number
  /** Wall-clock ceiling for one round. */
  readonly roundTimeout: Duration.DurationInput
  /** Wall-clock ceiling for the whole loop. */
  readonly totalTimeout: Duration.DurationInput
  /** Retry attempts per round before the round is abandoned. */
  readonly retries: number
  readonly concurrency: number
}

export const defaultBudget: LoopBudget = {
  maxRounds: 8,
  maxNodes: 200,
  dryRounds: 2,
  roundTimeout: Duration.seconds(30),
  totalTimeout: Duration.minutes(3),
  retries: 2,
  concurrency: 4
}

export type StopReason =
  | { readonly _tag: "FrontierEmpty" }
  | { readonly _tag: "Dry"; readonly rounds: number }
  | { readonly _tag: "RoundBudget" }
  | { readonly _tag: "NodeBudget" }
  | { readonly _tag: "TimedOut" }
  | { readonly _tag: "GraphUnrunnable"; readonly reason: string }

export interface LoopResult<T, A, E> {
  readonly graph: G.WorkGraph<T>
  readonly outcomes: ReadonlyMap<G.NodeId, Scheduler.Outcome<A, E>>
  readonly rounds: number
  readonly admitted: number
  readonly stoppedBecause: StopReason
}

/**
 * What a round is allowed to do.
 *
 * `execute` runs one node. `discover` may propose new nodes from the round's
 * outcomes — this is where an agent turns a result into more work (expand a
 * neighbourhood, follow a citation, dispatch a sub-question). Returning `[]`
 * makes the round dry.
 */
export interface LoopSpec<T, A, E, R> {
  readonly execute: (
    node: G.WorkNode<T>,
    upstream: ReadonlyMap<G.NodeId, A>
  ) => Effect.Effect<A, E, R>
  readonly discover?: (round: {
    readonly graph: G.WorkGraph<T>
    readonly outcomes: ReadonlyMap<G.NodeId, Scheduler.Outcome<A, E>>
    readonly index: number
  }) => Effect.Effect<ReadonlyArray<G.WorkNode<T>>, never, R>
  readonly budget?: Partial<LoopBudget>
  readonly name?: string
}

/**
 * Run the graph to convergence.
 *
 * Node failures are data in `outcomes`; the loop itself fails only when the
 * graph is structurally unrunnable, and even that is reported as a
 * `StopReason` rather than thrown.
 */
export const run = <T, A, E, R>(
  initial: G.WorkGraph<T>,
  spec: LoopSpec<T, A, E, R>
): Effect.Effect<LoopResult<T, A, E>, never, R> => {
  const budget: LoopBudget = { ...defaultBudget, ...spec.budget }
  const name = spec.name ?? "agent.loop"

  const retryPolicy = Schedule.exponential(Duration.millis(100), 2).pipe(
    Schedule.jittered,
    Schedule.compose(Schedule.recurs(budget.retries))
  )

  const body = Effect.gen(function* () {
    let graph = initial
    // Dedup key set: every id ever admitted, including ones that later failed.
    const seen = new Set<G.NodeId>(graph.nodes.keys())
    let outcomes = new Map<G.NodeId, Scheduler.Outcome<A, E>>()
    let rounds = 0
    let consecutiveDry = 0
    let stoppedBecause: StopReason = { _tag: "FrontierEmpty" }

    while (rounds < budget.maxRounds) {
      const pass = yield* Scheduler.run(graph, spec.execute, {
        concurrency: budget.concurrency
      }).pipe(
        Effect.timeoutOption(budget.roundTimeout),
        Effect.retry(retryPolicy),
        Effect.either
      )

      if (pass._tag === "Left") {
        stoppedBecause = { _tag: "GraphUnrunnable", reason: pass.left._tag }
        break
      }
      if (pass.right._tag === "None") {
        stoppedBecause = { _tag: "TimedOut" }
        break
      }

      const report = pass.right.value
      outcomes = new Map([...outcomes, ...report.outcomes])
      rounds += 1

      if (spec.discover === undefined) {
        stoppedBecause = { _tag: "FrontierEmpty" }
        break
      }

      const proposed = yield* spec.discover({ graph, outcomes: report.outcomes, index: rounds - 1 })
      // Dedup against `seen`, not against the graph's live nodes.
      const fresh = proposed.filter((n) => !seen.has(n.id))

      if (fresh.length === 0) {
        consecutiveDry += 1
        if (consecutiveDry >= budget.dryRounds) {
          stoppedBecause = { _tag: "Dry", rounds: consecutiveDry }
          break
        }
        continue
      }

      consecutiveDry = 0
      const room = budget.maxNodes - seen.size
      const admitted = fresh.slice(0, Math.max(0, room))
      for (const node of admitted) {
        seen.add(node.id)
        graph = G.addNode(graph, node)
      }

      if (admitted.length < fresh.length) {
        stoppedBecause = { _tag: "NodeBudget" }
        break
      }
    }

    if (rounds >= budget.maxRounds && stoppedBecause._tag === "FrontierEmpty") {
      stoppedBecause = { _tag: "RoundBudget" }
    }

    return {
      graph,
      outcomes,
      rounds,
      admitted: seen.size,
      stoppedBecause
    } satisfies LoopResult<T, A, E>
  })

  return body.pipe(
    Effect.timeoutTo({
      duration: budget.totalTimeout,
      onSuccess: (r: LoopResult<T, A, E>) => r,
      onTimeout: (): LoopResult<T, A, E> => ({
        graph: initial,
        outcomes: new Map(),
        rounds: 0,
        admitted: initial.nodes.size,
        stoppedBecause: { _tag: "TimedOut" }
      })
    }),
    Effect.withSpan(name, {
      attributes: { maxRounds: budget.maxRounds, maxNodes: budget.maxNodes }
    })
  )
}
