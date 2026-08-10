/**
 * The KG as the agent's move set.
 *
 * `/api/research/neighbors` already carries the right idea in its docstring —
 * *"makes any node a doorway: walk from it to its real typed neighbours
 * instead of reading a flat card"* — but only humans could walk through it.
 * The agent module could not: it had no traversal at all.
 *
 * This is that doorway, for the agent. A walk is a graph search over the live
 * KG where the frontier is scored, bounded, and deduplicated — so "explore the
 * neighbourhood of X" is a first-class operation rather than something a
 * handler open-codes.
 */
import { Effect } from "effect"
import type { GraphNeighbor } from "../domain/Contracts.js"
import { KgPortTag } from "../ports/KgPort.js"

export interface Visited {
  readonly name: string
  /** Hops from the seed. */
  readonly depth: number
  /** Total degree of the node in the KG, not just the expanded edges. */
  readonly degree: number
  /** The edge we arrived by (absent for the seed). */
  readonly via?: { readonly type: string; readonly direction: "out" | "in"; readonly from: string }
  readonly truncated: boolean
}

export interface WalkOptions {
  readonly maxNodes: number
  readonly maxDepth: number
  /** Edges expanded per node. Bounds fan-out on hub nodes. */
  readonly branching: number
  /**
   * Rank the candidates leaving a node. Higher goes first. Return a negative
   * score to refuse the edge entirely.
   *
   * Default: a mild preference for outgoing edges, so a walk reads with the
   * grain of the graph rather than crawling back up into hubs.
   */
  readonly score?: (neighbor: GraphNeighbor, from: Visited) => number
  /** Edge types to never follow. */
  readonly excludeTypes?: ReadonlyArray<string>
}

export const defaultWalk: WalkOptions = {
  maxNodes: 40,
  maxDepth: 3,
  branching: 8
}

const defaultScore = (n: GraphNeighbor): number => (n.direction === "out" ? 1 : 0)

export interface WalkResult {
  readonly seed: string
  readonly visited: ReadonlyArray<Visited>
  /** Nodes discovered but not expanded because a bound was hit. */
  readonly unexplored: ReadonlyArray<string>
  readonly stoppedBecause: "Exhausted" | "MaxNodes" | "MaxDepth"
}

/**
 * Breadth-first walk from `seed`.
 *
 * Deduplication is against **everything seen**, not against everything kept.
 * Deduping against the kept set is the mistake that makes a loop revisit
 * rejected nodes forever and never converge.
 */
export const walk = (
  seed: string,
  options: Partial<WalkOptions> = {}
): Effect.Effect<WalkResult, never, KgPortTag> =>
  Effect.gen(function* () {
    const opts: WalkOptions = { ...defaultWalk, ...options }
    const score = opts.score ?? defaultScore
    const exclude = new Set(opts.excludeTypes ?? [])
    const kg = yield* KgPortTag

    const seen = new Set<string>([seed])
    const visited: Array<Visited> = []
    const unexplored: Array<string> = []
    let queue: Array<Visited> = []
    let stoppedBecause: WalkResult["stoppedBecause"] = "Exhausted"

    const head = yield* kg.neighbors({ name: seed, limit: opts.branching })
    const seedNode: Visited = {
      name: seed,
      depth: 0,
      degree: head.degree,
      truncated: head.truncated
    }
    visited.push(seedNode)
    queue.push(seedNode)

    // Neighbours are re-fetched per node; `KgPort` is the only I/O here, so a
    // test can drive the whole search with a hand-built adjacency map.
    while (queue.length > 0) {
      const current = queue.shift()!

      if (current.depth >= opts.maxDepth) {
        stoppedBecause = "MaxDepth"
        continue
      }

      const page =
        current.name === seed ? head : yield* kg.neighbors({ name: current.name, limit: opts.branching })

      const candidates = page.neighbors
        .filter((n) => n.name !== "" && !exclude.has(n.type) && !seen.has(n.name))
        .map((n) => ({ n, s: score(n, current) }))
        .filter((c) => c.s >= 0)
        .sort((a, b) => b.s - a.s)
        .slice(0, opts.branching)

      for (const { n } of candidates) {
        if (seen.has(n.name)) continue
        seen.add(n.name)

        if (visited.length >= opts.maxNodes) {
          unexplored.push(n.name)
          stoppedBecause = "MaxNodes"
          continue
        }

        const next: Visited = {
          name: n.name,
          depth: current.depth + 1,
          degree: 0,
          via: { type: n.type, direction: n.direction, from: current.name },
          truncated: false
        }
        visited.push(next)
        queue.push(next)
      }

      if (visited.length >= opts.maxNodes) {
        // Drain the rest of the queue into `unexplored` so the caller can see
        // exactly what a tighter budget cost them.
        for (const q of queue) unexplored.push(q.name)
        queue = []
        stoppedBecause = "MaxNodes"
      }
    }

    return {
      seed,
      visited,
      unexplored: Array.from(new Set(unexplored)).sort(),
      stoppedBecause
    } satisfies WalkResult
  }).pipe(Effect.withSpan("agent.walk", { attributes: { seed } }))

/**
 * Turn a walk into work.
 *
 * Each visited node becomes a graph node whose dependency is the node it was
 * reached from — so the resulting `WorkGraph` mirrors the shape of the walk
 * and the scheduler can process the neighbourhood in discovery order.
 */
export const toWorkNodes = (
  result: WalkResult
): ReadonlyArray<{
  readonly id: string
  readonly payload: Visited
  readonly dependsOn: ReadonlyArray<string>
  readonly writeSet: ReadonlyArray<string>
}> =>
  result.visited.map((v) => ({
    id: v.name,
    payload: v,
    dependsOn: v.via === undefined ? [] : [v.via.from],
    writeSet: []
  }))
