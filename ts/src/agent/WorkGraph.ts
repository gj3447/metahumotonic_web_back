/**
 * The work graph — pure.
 *
 * The previous `Loop.ts` modelled agent work as `(state, index) => state`.
 * An `index: number` is a confession: it says the work is one-dimensional.
 * It is not. In this system:
 *
 *   - APT/SP decomposes an anchor into a **DAG of spans**, not a list;
 *   - a dispatch is a **hyperedge** — one parent, N children, one fan-in;
 *   - two nodes may only run together if their **write-sets are disjoint**
 *     (the lesson OMD paid for);
 *   - a failure must drop its **descendants**, not the whole run.
 *
 * None of that is expressible over an opaque state, so it lives here as an
 * explicit graph. Everything in this file is a pure function of plain data:
 * no Effect, no clock, no I/O. The scheduler that runs it is separate, which
 * is what makes the scheduling *rules* testable without running anything.
 */

export type NodeId = string

/** A unit of agent work. `T` is the payload the executor understands. */
export interface WorkNode<T> {
  readonly id: NodeId
  readonly payload: T
  /** Nodes that must complete before this one may start. */
  readonly dependsOn: ReadonlyArray<NodeId>
  /**
   * Resources this node will mutate. Two nodes whose write-sets intersect are
   * never run concurrently, even when the DAG would allow it.
   *
   * Empty means read-only: always safe to parallelise.
   */
  readonly writeSet: ReadonlyArray<string>
}

export interface WorkGraph<T> {
  readonly nodes: ReadonlyMap<NodeId, WorkNode<T>>
}

/**
 * A dispatch: one parent fanning out to N children, collected once.
 *
 * This is a hyperedge, not N ordinary edges — the fan-out and the fan-in are
 * a single fact, and recording them separately loses the fact that they
 * belong together. Reifying it is what makes provenance answerable later
 * ("which dispatch produced this finding?").
 */
export interface Hyperedge {
  readonly id: string
  readonly parent: NodeId
  readonly children: ReadonlyArray<NodeId>
  readonly kind: string
}

// ---------------------------------------------------------------------------
// construction
// ---------------------------------------------------------------------------

export const empty = <T>(): WorkGraph<T> => ({ nodes: new Map() })

export const fromNodes = <T>(nodes: Iterable<WorkNode<T>>): WorkGraph<T> => ({
  nodes: new Map(Array.from(nodes, (n) => [n.id, n] as const))
})

export const addNode = <T>(graph: WorkGraph<T>, node: WorkNode<T>): WorkGraph<T> => {
  const nodes = new Map(graph.nodes)
  nodes.set(node.id, node)
  return { nodes }
}

/** A node with no dependencies and no write-set — the common leaf case. */
export const leaf = <T>(id: NodeId, payload: T): WorkNode<T> => ({
  id,
  payload,
  dependsOn: [],
  writeSet: []
})

/**
 * Expand `parent` into `children`, wiring each child to depend on the parent
 * and returning the hyperedge that records the fan-out as one event.
 */
export const expand = <T>(
  graph: WorkGraph<T>,
  parent: NodeId,
  children: ReadonlyArray<WorkNode<T>>,
  kind = "dispatch"
): { readonly graph: WorkGraph<T>; readonly hyperedge: Hyperedge } => {
  const nodes = new Map(graph.nodes)
  for (const child of children) {
    nodes.set(child.id, {
      ...child,
      dependsOn: child.dependsOn.includes(parent) ? child.dependsOn : [...child.dependsOn, parent]
    })
  }
  return {
    graph: { nodes },
    hyperedge: {
      id: `${kind}:${parent}:${children.length}`,
      parent,
      children: children.map((c) => c.id),
      kind
    }
  }
}

// ---------------------------------------------------------------------------
// structural queries
// ---------------------------------------------------------------------------

/** Dependency edges that point at nodes which are not in the graph. */
export const danglingEdges = <T>(
  graph: WorkGraph<T>
): ReadonlyArray<{ readonly from: NodeId; readonly to: NodeId }> => {
  const out: Array<{ from: NodeId; to: NodeId }> = []
  for (const node of graph.nodes.values()) {
    for (const dep of node.dependsOn) {
      if (!graph.nodes.has(dep)) out.push({ from: node.id, to: dep })
    }
  }
  return out
}

/**
 * Kahn's algorithm. Returns the topological order, or the set of nodes
 * involved in a cycle.
 *
 * A cycle is a *design* error — an agent graph that cannot be ordered will
 * deadlock — so it is reported as data rather than thrown, and the scheduler
 * refuses to start rather than discovering it halfway through.
 */
export const topoSort = <T>(
  graph: WorkGraph<T>
): { readonly _tag: "Ordered"; readonly order: ReadonlyArray<NodeId> } | {
  readonly _tag: "Cyclic"
  readonly involved: ReadonlyArray<NodeId>
} => {
  const indegree = new Map<NodeId, number>()
  const dependents = new Map<NodeId, Array<NodeId>>()

  for (const node of graph.nodes.values()) {
    const deps = node.dependsOn.filter((d) => graph.nodes.has(d))
    indegree.set(node.id, deps.length)
    for (const dep of deps) {
      const list = dependents.get(dep) ?? []
      list.push(node.id)
      dependents.set(dep, list)
    }
  }

  const queue = Array.from(indegree.entries())
    .filter(([, n]) => n === 0)
    .map(([id]) => id)
    .sort()
  const order: Array<NodeId> = []

  while (queue.length > 0) {
    const id = queue.shift()!
    order.push(id)
    for (const next of dependents.get(id) ?? []) {
      const remaining = (indegree.get(next) ?? 0) - 1
      indegree.set(next, remaining)
      if (remaining === 0) queue.push(next)
    }
    queue.sort()
  }

  if (order.length !== graph.nodes.size) {
    const involved = Array.from(graph.nodes.keys())
      .filter((id) => !order.includes(id))
      .sort()
    return { _tag: "Cyclic", involved }
  }
  return { _tag: "Ordered", order }
}

/** Everything reachable *downstream* of `id` — what a failure takes with it. */
export const descendants = <T>(graph: WorkGraph<T>, id: NodeId): ReadonlySet<NodeId> => {
  const dependents = new Map<NodeId, Array<NodeId>>()
  for (const node of graph.nodes.values()) {
    for (const dep of node.dependsOn) {
      const list = dependents.get(dep) ?? []
      list.push(node.id)
      dependents.set(dep, list)
    }
  }

  const out = new Set<NodeId>()
  const stack = [...(dependents.get(id) ?? [])]
  while (stack.length > 0) {
    const next = stack.pop()!
    if (out.has(next)) continue
    out.add(next)
    stack.push(...(dependents.get(next) ?? []))
  }
  return out
}

export interface RunState {
  readonly done: ReadonlySet<NodeId>
  readonly running: ReadonlySet<NodeId>
  /** Failed, or skipped because an ancestor failed. */
  readonly blocked: ReadonlySet<NodeId>
}

export const initialRunState = (): RunState => ({
  done: new Set(),
  running: new Set(),
  blocked: new Set()
})

/** Write-sets currently held by running nodes. */
const heldLeases = <T>(graph: WorkGraph<T>, running: ReadonlySet<NodeId>): ReadonlySet<string> => {
  const held = new Set<string>()
  for (const id of running) {
    for (const resource of graph.nodes.get(id)?.writeSet ?? []) held.add(resource)
  }
  return held
}

/**
 * The nodes that may start *right now*.
 *
 * Two independent gates, and both matter:
 *
 *   1. every dependency is `done`  — the DAG gate;
 *   2. the write-set does not intersect a lease held by a running node — the
 *      concurrency gate.
 *
 * A node whose dependency ended up `blocked` can never become ready; it is
 * reported by `newlyBlocked` instead, so the run terminates rather than
 * spinning on an unsatisfiable frontier.
 */
export const readySet = <T>(graph: WorkGraph<T>, state: RunState): ReadonlyArray<NodeId> => {
  const held = new Set(heldLeases(graph, state.running))
  const ready: Array<NodeId> = []

  for (const node of graph.nodes.values()) {
    if (state.done.has(node.id) || state.running.has(node.id) || state.blocked.has(node.id)) {
      continue
    }
    if (!node.dependsOn.every((d) => state.done.has(d))) continue
    if (node.writeSet.some((r) => held.has(r))) continue

    // Claim the lease for this planning pass so two conflicting nodes are not
    // both reported ready in the same batch.
    for (const r of node.writeSet) held.add(r)
    ready.push(node.id)
  }

  return ready.sort()
}

/** Nodes that can never run because an ancestor failed. */
export const newlyBlocked = <T>(
  graph: WorkGraph<T>,
  state: RunState,
  failed: NodeId
): ReadonlySet<NodeId> => {
  const out = new Set<NodeId>()
  for (const id of descendants(graph, failed)) {
    if (!state.done.has(id) && !state.blocked.has(id)) out.add(id)
  }
  return out
}

/** Nothing running and nothing ready ⇒ the run is over. */
export const isSettled = <T>(graph: WorkGraph<T>, state: RunState): boolean =>
  state.running.size === 0 && readySet(graph, state).length === 0

/**
 * The open frontier: nodes not yet resolved.
 *
 * Convergence for a graph walk is "this set is empty", which is what replaces
 * the old `Object.is(prev, next)` state comparison.
 */
export const frontier = <T>(graph: WorkGraph<T>, state: RunState): ReadonlyArray<NodeId> =>
  Array.from(graph.nodes.keys())
    .filter((id) => !state.done.has(id) && !state.blocked.has(id))
    .sort()
