/**
 * Time-based circuit breaker — port of `app/breaker.py`.
 *
 * The Python version guarded a mutable field with no lock and read a monotonic
 * clock straight from the OS. Here the state lives in a `Ref` and the clock
 * comes from Effect's `Clock` service, which means `TestClock` can advance the
 * cooldown instantly instead of a test sleeping for 30 real seconds.
 */
import { Clock, Effect, Ref } from "effect"

export const DEFAULT_COOLDOWN_MILLIS = 30_000

export interface Breaker {
  /** True ⇒ skip the dependency (recently failed, still cooling down). */
  readonly isOpen: Effect.Effect<boolean>
  readonly trip: Effect.Effect<void>
  readonly reset: Effect.Effect<void>
}

export const make = (
  cooldownMillis: number = DEFAULT_COOLDOWN_MILLIS
): Effect.Effect<Breaker> =>
  Effect.gen(function* () {
    const retryAfter = yield* Ref.make(0)

    return {
      isOpen: Effect.gen(function* () {
        const now = yield* Clock.currentTimeMillis
        return now < (yield* Ref.get(retryAfter))
      }),
      trip: Effect.gen(function* () {
        const now = yield* Clock.currentTimeMillis
        yield* Ref.set(retryAfter, now + cooldownMillis)
      }),
      reset: Ref.set(retryAfter, 0)
    }
  })

/**
 * Run `effect`, but short-circuit to `whenOpen` while the breaker is tripped.
 * A failure trips it; a success resets it. This is the whole degradation
 * policy of the service, expressed once instead of at every call site.
 */
export const guard = <A, E, R>(
  breaker: Breaker,
  effect: Effect.Effect<A, E, R>,
  whenOpen: Effect.Effect<A>
): Effect.Effect<A, never, R> =>
  Effect.gen(function* () {
    if (yield* breaker.isOpen) return yield* whenOpen
    const result = yield* Effect.either(effect)
    if (result._tag === "Left") {
      yield* breaker.trip
      return yield* whenOpen
    }
    yield* breaker.reset
    return result.right
  })
