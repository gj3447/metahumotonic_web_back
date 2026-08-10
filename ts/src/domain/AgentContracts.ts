/**
 * Wire contracts for the agent surface.
 *
 * These exist so the graph machinery is *reachable*, not merely present. The
 * first version of `agent/Loop.ts` had no endpoint and nothing imported it —
 * dead code cannot be wrong, which is exactly why it was never right.
 */
import { Schema } from "effect"

export class WalkEdge extends Schema.Class<WalkEdge>("WalkEdge")({
  type: Schema.String,
  direction: Schema.Literal("out", "in"),
  from: Schema.String
}) {}

export class WalkNode extends Schema.Class<WalkNode>("WalkNode")({
  name: Schema.String,
  depth: Schema.Number,
  degree: Schema.Number,
  via: Schema.NullOr(WalkEdge),
  truncated: Schema.Boolean
}) {}

export class WalkResponse extends Schema.Class<WalkResponse>("WalkResponse")({
  seed: Schema.String,
  visited: Schema.Array(WalkNode),
  /** Discovered but not expanded because a bound was hit — never silent. */
  unexplored: Schema.Array(Schema.String),
  stoppedBecause: Schema.Literal("Exhausted", "MaxNodes", "MaxDepth")
}) {}

/**
 * The walk, re-read as a work graph.
 *
 * This is the endpoint that makes the difference between the two designs
 * visible: the same neighbourhood, presented as a DAG with a topological
 * order and a ready-set rather than as a flat list.
 */
export class PlanResponse extends Schema.Class<PlanResponse>("PlanResponse")({
  seed: Schema.String,
  nodeCount: Schema.Number,
  /** Kahn order, or the cycle members when the graph cannot be ordered. */
  topologicalOrder: Schema.Array(Schema.String),
  cyclic: Schema.Boolean,
  cycleMembers: Schema.Array(Schema.String),
  /** What could start immediately, honouring dependencies and write-set leases. */
  readySet: Schema.Array(Schema.String),
  frontier: Schema.Array(Schema.String),
  /** Dependency edges pointing outside the graph. */
  dangling: Schema.Number
}) {}

export class NodeOutcome extends Schema.Class<NodeOutcome>("NodeOutcome")({
  id: Schema.String,
  outcome: Schema.Literal("Completed", "Failed", "Blocked"),
  /** For `Blocked`: the ancestor whose failure cascaded here. */
  detail: Schema.optionalWith(Schema.String, { default: () => "" })
}) {}

export class ExploreResponse extends Schema.Class<ExploreResponse>("ExploreResponse")({
  seed: Schema.String,
  rounds: Schema.Number,
  admitted: Schema.Number,
  /** Named terminal condition. There is no unnamed way for the loop to end. */
  stoppedBecause: Schema.String,
  completed: Schema.Number,
  failed: Schema.Number,
  blocked: Schema.Number,
  outcomes: Schema.Array(NodeOutcome)
}) {}
