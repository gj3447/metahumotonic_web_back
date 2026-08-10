/**
 * The agent surface, wired.
 *
 * This file is the point of the rewrite. The previous `agent/Loop.ts` was
 * imported by nothing — a paradigm on display rather than a paradigm running.
 * Here every piece of the graph stack is on a request path:
 *
 *   /api/agent/walk     → Traversal        (KG as the move set)
 *   /api/agent/plan     → WorkGraph        (the walk re-read as a DAG)
 *   /api/agent/explore  → Loop + Scheduler (rounds to convergence)
 */
import { HttpApiBuilder } from "@effect/platform"
import { Effect, Layer } from "effect"
import * as Loop from "../agent/Loop.js"
import * as Traversal from "../agent/Traversal.js"
import * as G from "../agent/WorkGraph.js"
import { Api } from "../api/Api.js"
import {
  ExploreResponse,
  NodeOutcome,
  PlanResponse,
  WalkEdge,
  WalkNode,
  WalkResponse
} from "../domain/AgentContracts.js"
import { KgPortTag } from "../ports/KgPort.js"

const toWalkNode = (v: Traversal.Visited): WalkNode =>
  new WalkNode({
    name: v.name,
    depth: v.depth,
    degree: v.degree,
    via: v.via === undefined ? null : new WalkEdge(v.via),
    truncated: v.truncated
  })

/** The walk, lifted into a work graph — shared by `plan` and `explore`. */
const graphOfWalk = (result: Traversal.WalkResult): G.WorkGraph<Traversal.Visited> =>
  G.fromNodes(Traversal.toWorkNodes(result))

export const AgentLive = HttpApiBuilder.group(Api, "agent", (handlers) =>
  handlers
    .handle("walk", ({ urlParams }) =>
      Effect.gen(function* () {
        const result = yield* Traversal.walk(urlParams.seed, {
          maxNodes: urlParams.maxNodes,
          maxDepth: urlParams.maxDepth,
          branching: urlParams.branching
        })
        return new WalkResponse({
          seed: result.seed,
          visited: result.visited.map(toWalkNode),
          unexplored: result.unexplored,
          stoppedBecause: result.stoppedBecause
        })
      })
    )

    /**
     * The same neighbourhood, presented as a DAG.
     *
     * This is the endpoint that shows what the flat-array design could not
     * express: an execution order, a ready-set that respects write-set
     * leases, and an explicit answer to "is this graph even runnable?".
     */
    .handle("plan", ({ urlParams }) =>
      Effect.gen(function* () {
        const result = yield* Traversal.walk(urlParams.seed, {
          maxNodes: urlParams.maxNodes,
          maxDepth: urlParams.maxDepth,
          branching: urlParams.branching
        })
        const graph = graphOfWalk(result)
        const sorted = G.topoSort(graph)
        const state = G.initialRunState()

        return new PlanResponse({
          seed: result.seed,
          nodeCount: graph.nodes.size,
          topologicalOrder: sorted._tag === "Ordered" ? sorted.order : [],
          cyclic: sorted._tag === "Cyclic",
          cycleMembers: sorted._tag === "Cyclic" ? sorted.involved : [],
          readySet: G.readySet(graph, state),
          frontier: G.frontier(graph, state),
          dangling: G.danglingEdges(graph).length
        })
      })
    )

    /**
     * Run the neighbourhood to convergence.
     *
     * Each node's work is "fetch this node's own neighbours" — real I/O, so
     * the scheduler's dependency ordering, concurrency bound and failure
     * cascade are all genuinely exercised rather than simulated. `discover`
     * then feeds newly-seen names back in as fresh nodes, which is what makes
     * this a loop instead of a single scheduler pass.
     */
    .handle("explore", ({ urlParams }) =>
      Effect.gen(function* () {
        const kg = yield* KgPortTag

        const seed = G.fromNodes([
          G.leaf(urlParams.seed, { name: urlParams.seed, depth: 0 } as Traversal.Visited)
        ])

        const result = yield* Loop.run<Traversal.Visited, ReadonlyArray<string>, never, never>(
          seed,
          {
            name: "agent.explore",
            budget: {
              maxNodes: urlParams.maxNodes,
              maxRounds: urlParams.maxRounds,
              concurrency: urlParams.concurrency
            },
            execute: (node) =>
              kg
                .neighbors({ name: node.id, limit: 8 })
                .pipe(
                  Effect.map((page) =>
                    page.neighbors.map((n) => n.name).filter((n) => n !== "")
                  )
                ),
            // Everything the round uncovered becomes candidate work. The loop
            // dedups against every id ever admitted, so a hub reached twice
            // does not reopen it.
            discover: ({ outcomes }) =>
              Effect.succeed(
                Array.from(outcomes.entries()).flatMap(([from, outcome]) =>
                  outcome._tag === "Completed"
                    ? outcome.value.map((name) => ({
                        id: name,
                        payload: { name, depth: 0 } as Traversal.Visited,
                        dependsOn: [from],
                        writeSet: []
                      }))
                    : []
                )
              )
          }
        )

        const outcomes = Array.from(result.outcomes.entries()).map(
          ([id, o]) =>
            new NodeOutcome({
              id,
              outcome: o._tag,
              detail: o._tag === "Blocked" ? o.by : ""
            })
        )

        const count = (tag: string) => outcomes.filter((o) => o.outcome === tag).length

        return new ExploreResponse({
          seed: urlParams.seed,
          rounds: result.rounds,
          admitted: result.admitted,
          stoppedBecause: result.stoppedBecause._tag,
          completed: count("Completed"),
          failed: count("Failed"),
          blocked: count("Blocked"),
          outcomes: outcomes.slice(0, 100)
        })
      })
    )
)

export const AgentHandlersLive = Layer.mergeAll(AgentLive)
