/**
 * Rate limiting — port of `app/ratelimit.py`.
 *
 * PROM16 C2: an in-process limiter is wrong the moment there are multiple
 * replicas (the limit multiplies) or a restart happens (the counter resets).
 * The Python version therefore uses Redis when configured and degrades to an
 * in-process sliding window otherwise.
 *
 * Two things change in this port:
 *
 *  1. The in-process window needed a `threading.Lock`. `Ref.modify` is already
 *     atomic under Effect's fiber scheduler, so the lock disappears rather
 *     than being reimplemented.
 *  2. `time.monotonic()` becomes `Clock.currentTimeMillis`, so a window-expiry
 *     test advances `TestClock` instead of sleeping for real seconds.
 */
import { Clock, Context, Effect, Layer, Ref } from "effect"
import { RateLimited, Unavailable } from "../domain/Errors.js"

export interface Decision {
  readonly allowed: boolean
  readonly retryAfterSeconds: number
}

export interface RateLimiter {
  /** Records a hit and reports whether it is within the window. */
  readonly check: (key: string) => Effect.Effect<Decision, Unavailable>
  /** Is the distributed backend usable? Feeds startup fail-closed checks. */
  readonly ready: Effect.Effect<boolean>
  readonly reset: Effect.Effect<void>
}

export interface LimiterPolicy {
  readonly maxEvents: number
  readonly windowSeconds: number
  /** When true and the distributed backend is unusable, deny instead of
   *  falling back to the in-process window. */
  readonly failClosed: boolean
}

/** Raise `RateLimited` when the decision says no — the shape handlers use. */
export const enforce = (
  limiter: RateLimiter,
  key: string
): Effect.Effect<void, RateLimited | Unavailable> =>
  limiter.check(key).pipe(
    Effect.flatMap((d) =>
      d.allowed
        ? Effect.void
        : Effect.fail(
            new RateLimited({
              reason: "too many requests",
              retryAfterSeconds: d.retryAfterSeconds
            })
          )
    )
  )

/**
 * In-process sliding window. Pure `Ref` state + `Clock`; no wall-clock reads,
 * no locks, no globals.
 */
export const makeInProcess = (policy: LimiterPolicy): Effect.Effect<RateLimiter> =>
  Effect.gen(function* () {
    const hits = yield* Ref.make<ReadonlyMap<string, ReadonlyArray<number>>>(new Map())
    const windowMillis = policy.windowSeconds * 1000

    return {
      check: (key) =>
        Effect.gen(function* () {
          const now = yield* Clock.currentTimeMillis
          const cutoff = now - windowMillis

          type Modified = readonly [Decision, ReadonlyMap<string, ReadonlyArray<number>>]

          return yield* Ref.modify(hits, (map): Modified => {
            const kept = (map.get(key) ?? []).filter((t) => t >= cutoff)
            const next = new Map(map)

            if (kept.length >= policy.maxEvents) {
              const oldest = kept[0]!
              const retryAfterSeconds = Math.max(
                1,
                Math.ceil((oldest + windowMillis - now) / 1000)
              )
              next.set(key, kept)
              return [{ allowed: false, retryAfterSeconds }, next]
            }

            next.set(key, [...kept, now])
            return [{ allowed: true, retryAfterSeconds: 0 }, next]
          })
        }),

      // An in-process limiter is always "ready" — it just isn't distributed.
      ready: Effect.succeed(true),
      reset: Ref.set(hits, new Map())
    } satisfies RateLimiter
  })

/**
 * A limiter that denies everything with 503.
 *
 * This is what a fail-closed surface gets when its distributed backend is
 * missing: refusing service is the correct behaviour, and making it an
 * explicit limiter keeps that decision in one readable place.
 */
export const denyAll = (reason: string): RateLimiter => ({
  check: () => Effect.fail(new Unavailable({ reason })),
  ready: Effect.succeed(false),
  reset: Effect.void
})

// --- named limiters ---------------------------------------------------------
// The service runs four independent limiters. Each gets its own tag so a
// handler asks for exactly the one it is allowed to consume.

export class FeedbackLimiter extends Context.Tag("FeedbackLimiter")<
  FeedbackLimiter,
  RateLimiter
>() {}

export class WikiSessionLimiter extends Context.Tag("WikiSessionLimiter")<
  WikiSessionLimiter,
  RateLimiter
>() {}

export class WikiMutationLimiter extends Context.Tag("WikiMutationLimiter")<
  WikiMutationLimiter,
  RateLimiter
>() {}

export class WikiReadLimiter extends Context.Tag("WikiReadLimiter")<
  WikiReadLimiter,
  RateLimiter
>() {}

export const layerInProcess = <I, S extends RateLimiter>(
  tag: Context.Tag<I, S>,
  policy: LimiterPolicy
): Layer.Layer<I> => Layer.effect(tag, makeInProcess(policy) as Effect.Effect<S>)
