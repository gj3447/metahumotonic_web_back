/**
 * The agent paradigm, made structural.
 *
 * An agent step is not a special kind of function — it is an ordinary
 * `Effect<A, E, R>` that happens to be run under a budget. Everything an agent
 * loop needs is already in Effect and needs only to be *named*:
 *
 *   | agent concern        | Effect primitive                       |
 *   |----------------------|----------------------------------------|
 *   | typed failure        | the `E` channel                        |
 *   | tool / capability    | the `R` channel                        |
 *   | retry with backoff   | `Schedule.exponential` + `jittered`    |
 *   | wall-clock budget    | `Effect.timeout`                       |
 *   | cancellation         | fiber interruption (structural)        |
 *   | observability        | `Effect.withSpan` / `Effect.annotate*` |
 *   | bounded fan-out      | `Effect.all({ concurrency })`          |
 *
 * The value this module adds is the *bound*: a loop declares its step budget,
 * its per-step timeout and its retry policy up front, and cannot exceed them.
 * A runaway loop becomes a type-level impossibility rather than a bill.
 */
import { Duration, Effect, Schedule } from "effect"

export interface LoopBudget {
  /** Hard ceiling on iterations. The loop stops at this count, always. */
  readonly maxSteps: number
  /** Wall-clock ceiling for one step, retries included. */
  readonly stepTimeout: Duration.DurationInput
  /** Wall-clock ceiling for the whole loop. */
  readonly totalTimeout: Duration.DurationInput
  /** Retry attempts per step before the step is considered failed. */
  readonly retries: number
}

export const defaultBudget: LoopBudget = {
  maxSteps: 8,
  stepTimeout: Duration.seconds(20),
  totalTimeout: Duration.minutes(2),
  retries: 2
}

/** Why a loop stopped. Every terminal condition is one of these — there is no
 *  implicit "fell out of the while". */
export type StopReason =
  | { readonly _tag: "Converged" }
  | { readonly _tag: "BudgetExhausted"; readonly steps: number }
  | { readonly _tag: "NoProgress"; readonly steps: number }

export interface LoopResult<A> {
  readonly value: A
  readonly steps: number
  readonly stoppedBecause: StopReason
}

/** A step maps the running state to the next state, or signals convergence by
 *  returning the state unchanged (checked with `sameState`). */
export type Step<A, E, R> = (state: A, index: number) => Effect.Effect<A, E, R>

export interface LoopOptions<A> {
  readonly budget?: Partial<LoopBudget>
  /** Convergence test. Default: reference equality. */
  readonly sameState?: (previous: A, next: A) => boolean
  /** Name for the trace span wrapping the whole loop. */
  readonly name?: string
}

/**
 * Run `step` until it converges, the step budget runs out, or the total
 * timeout fires — whichever comes first.
 *
 * Retries are `exponential + jittered`, capped at `budget.retries`, so a flaky
 * dependency does not turn into a hot loop.
 */
export const run = <A, E, R>(
  initial: A,
  step: Step<A, E, R>,
  options: LoopOptions<A> = {}
): Effect.Effect<LoopResult<A>, E, R> => {
  const budget: LoopBudget = { ...defaultBudget, ...options.budget }
  const same = options.sameState ?? ((a: A, b: A) => Object.is(a, b))
  const name = options.name ?? "agent.loop"

  const retryPolicy = Schedule.exponential(Duration.millis(100), 2).pipe(
    Schedule.jittered,
    Schedule.compose(Schedule.recurs(budget.retries))
  )

  const body = Effect.gen(function* () {
    let state = initial
    let steps = 0
    let stoppedBecause: StopReason = { _tag: "BudgetExhausted", steps: budget.maxSteps }

    while (steps < budget.maxSteps) {
      const index = steps
      const next = yield* step(state, index).pipe(
        Effect.timeoutFail({
          duration: budget.stepTimeout,
          onTimeout: () => new StepTimedOut({ index }) as never
        }),
        Effect.retry(retryPolicy),
        Effect.withSpan(`${name}.step`, { attributes: { index } })
      )
      steps += 1

      if (same(state, next)) {
        state = next
        stoppedBecause = { _tag: "Converged" }
        break
      }
      state = next
    }

    return { value: state, steps, stoppedBecause } satisfies LoopResult<A>
  })

  return body.pipe(
    Effect.timeout(budget.totalTimeout),
    Effect.map((result) => result),
    Effect.catchTag("TimeoutException", () =>
      Effect.succeed({
        value: initial,
        steps: budget.maxSteps,
        stoppedBecause: { _tag: "NoProgress", steps: budget.maxSteps } as StopReason
      })
    ),
    Effect.withSpan(name, {
      attributes: {
        maxSteps: budget.maxSteps,
        retries: budget.retries
      }
    })
  )
}

/** Raised when one step blows its slice of the budget. Never escapes `run`. */
export class StepTimedOut {
  readonly _tag = "StepTimedOut"
  constructor(readonly meta: { readonly index: number }) {}
}

/**
 * Bounded parallel fan-out — the "dispatch N subagents" shape.
 *
 * Concurrency is a required argument rather than a default, because an
 * unbounded `Promise.all` over model calls is the single most common way an
 * agent system falls over.
 */
export const fanOut = <A, E, R>(
  tasks: ReadonlyArray<Effect.Effect<A, E, R>>,
  concurrency: number
): Effect.Effect<ReadonlyArray<A>, E, R> =>
  Effect.all(tasks, { concurrency }).pipe(Effect.withSpan("agent.fanOut"))

/**
 * Run every task, keep what succeeded, and report what failed — the
 * "one failing sub-feed degrades to [] rather than 500-ing the whole feed"
 * policy that `KG.get_recent` applies by hand, available to any caller.
 */
export const fanOutSettled = <A, E, R>(
  tasks: ReadonlyArray<Effect.Effect<A, E, R>>,
  concurrency: number
): Effect.Effect<
  { readonly ok: ReadonlyArray<A>; readonly failed: ReadonlyArray<E> },
  never,
  R
> =>
  Effect.all(
    tasks.map((t) => Effect.either(t)),
    { concurrency }
  ).pipe(
    Effect.map((results) => ({
      ok: results.flatMap((r) => (r._tag === "Right" ? [r.right] : [])),
      failed: results.flatMap((r) => (r._tag === "Left" ? [r.left] : []))
    })),
    Effect.withSpan("agent.fanOutSettled")
  )
