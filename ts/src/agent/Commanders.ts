/**
 * Measurement-driven conditional dispatch.
 *
 * SYMPOSIUM canon `7cmd-measurement-driven-conditional-dispatch-2026-05-30`
 * retracted the idea that the seven commanders hold *fixed* USES relations.
 * What replaced it: each commander measures its own metric, and when that
 * metric crosses a threshold it dispatches another commander. The service
 * graph is therefore **resolved at runtime, not fixed at compile time** —
 * the canonical examples being
 *
 *   occam.confidence < 0.7        → dispatch 나생문 (needs verification)
 *   eureka.bindingDensity < τ     → dispatch 롱기누스 (needs binding)
 *
 * That is a graph whose edges are computed, which is exactly the structure the
 * old `Loop.ts` had no way to express: it could call things in sequence, but
 * it could not let a *measurement* decide what the next edge is.
 *
 * This module is deliberately about the mechanism, not about the seven
 * specific commanders — a `Commander` here is any capability that can measure
 * itself. The roster is data.
 */
import { Effect } from "effect"

export interface Measurement {
  readonly metric: string
  readonly value: number
  /** What the value was compared against. */
  readonly threshold: number
  /** Which side of the threshold triggers a dispatch. */
  readonly direction: "below" | "above"
}

export const crossed = (m: Measurement): boolean =>
  m.direction === "below" ? m.value < m.threshold : m.value > m.threshold

/**
 * A capability that can measure its own state and, when that measurement
 * crosses its threshold, name who should be called next.
 */
export interface Commander<Ctx, E, R> {
  readonly name: string
  /** The commander's own work. */
  readonly act: (ctx: Ctx) => Effect.Effect<Ctx, E, R>
  /** Self-measurement, taken *after* acting. */
  readonly measure: (ctx: Ctx) => Effect.Effect<Measurement, E, R>
  /**
   * Who to hand to when the threshold is crossed. A name, not a reference —
   * resolution happens against the roster at runtime, so the dependency is a
   * computed edge rather than an import.
   */
  readonly escalateTo: string
}

export type Roster<Ctx, E, R> = ReadonlyMap<string, Commander<Ctx, E, R>>

export const roster = <Ctx, E, R>(
  commanders: Iterable<Commander<Ctx, E, R>>
): Roster<Ctx, E, R> => new Map(Array.from(commanders, (c) => [c.name, c] as const))

/** One resolved edge: who ran, what they measured, who it sent them to. */
export interface DispatchStep {
  readonly commander: string
  readonly measurement: Measurement
  readonly escalated: boolean
  readonly next: string | null
  /** Set when the escalation target was refused rather than followed. */
  readonly refusedBecause?: "UnknownCommander" | "AlreadyVisited" | "BudgetExhausted"
}

export interface DispatchTrace<Ctx> {
  readonly context: Ctx
  readonly path: ReadonlyArray<DispatchStep>
  readonly stoppedBecause: "Converged" | "BudgetExhausted" | "Cycle" | "UnknownCommander"
}

export interface DispatchBudget {
  /** Hard ceiling on hand-offs. */
  readonly maxHops: number
  /**
   * Whether a commander may run twice in one chain.
   *
   * Default false: without it, `occam → naesengmoon → occam` is a live
   * lock, and "the metric will settle eventually" is not a termination proof.
   */
  readonly allowRevisit?: boolean
}

/**
 * Follow the computed edges from `start` until the metric stops crossing, the
 * hop budget runs out, or the chain tries to revisit a commander.
 *
 * Every stop is a named reason. There is no path out of this function that
 * means "it just ended".
 */
export const dispatchFrom = <Ctx, E, R>(
  registry: Roster<Ctx, E, R>,
  start: string,
  initial: Ctx,
  budget: DispatchBudget
): Effect.Effect<DispatchTrace<Ctx>, E, R> =>
  Effect.gen(function* () {
    const path: Array<DispatchStep> = []
    const visited = new Set<string>()
    let context = initial
    let currentName: string | null = start
    let stoppedBecause: DispatchTrace<Ctx>["stoppedBecause"] = "Converged"

    while (currentName !== null) {
      if (path.length >= budget.maxHops) {
        stoppedBecause = "BudgetExhausted"
        break
      }

      const commander: Commander<Ctx, E, R> | undefined = registry.get(currentName)
      if (commander === undefined) {
        stoppedBecause = "UnknownCommander"
        break
      }

      visited.add(commander.name)
      context = yield* commander.act(context).pipe(
        Effect.withSpan("agent.commander.act", { attributes: { commander: commander.name } })
      )
      const measurement = yield* commander.measure(context)

      if (!crossed(measurement)) {
        path.push({ commander: commander.name, measurement, escalated: false, next: null })
        stoppedBecause = "Converged"
        break
      }

      const target = commander.escalateTo
      if (!registry.has(target)) {
        path.push({
          commander: commander.name,
          measurement,
          escalated: false,
          next: null,
          refusedBecause: "UnknownCommander"
        })
        stoppedBecause = "UnknownCommander"
        break
      }
      if (!(budget.allowRevisit ?? false) && visited.has(target)) {
        path.push({
          commander: commander.name,
          measurement,
          escalated: false,
          next: null,
          refusedBecause: "AlreadyVisited"
        })
        stoppedBecause = "Cycle"
        break
      }

      path.push({ commander: commander.name, measurement, escalated: true, next: target })
      currentName = target
    }

    return { context, path, stoppedBecause } satisfies DispatchTrace<Ctx>
  }).pipe(Effect.withSpan("agent.dispatchFrom", { attributes: { start } }))
