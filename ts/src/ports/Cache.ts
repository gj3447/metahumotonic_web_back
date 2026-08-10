/**
 * Bounded TTL cache with single-flight — port of `app/cache.py`.
 *
 * The first version of this port parsed three cache settings
 * (`statsCacheTtlSeconds`, `researchCacheTtlSeconds`, `cacheMaxEntries`) and
 * implemented none of them. In live mode that made every `/api/stats` and
 * `/api/research/*` request an uncached Bolt round-trip where the Python does
 * one every 120s / 300s. It is a load regression that only shows up under real
 * traffic, which is exactly the kind that ships.
 *
 * Two properties matter and both are easy to get wrong:
 *
 *  1. **Bounded.** The cache key includes attacker-controlled query params
 *     (`?cycle`, `?domain`, `?offset`), so an unbounded map is a memory-growth
 *     vector. Eviction is oldest-insertion-first once `maxEntries` is reached.
 *  2. **Single-flight.** N concurrent misses on the same key must produce ONE
 *     upstream call, not N. Without it the cache makes a thundering herd worse,
 *     because every miss arrives at once when the entry expires.
 *
 * `Clock` rather than `Date.now()` so expiry is testable with `TestClock`.
 */
import { Clock, Deferred, Effect, Ref } from "effect"

interface Entry<A> {
  readonly value: A
  readonly expiresAtMillis: number
  /** Insertion order, for eviction. */
  readonly seq: number
}

interface State<A> {
  readonly entries: ReadonlyMap<string, Entry<A>>
  readonly inFlight: ReadonlyMap<string, Deferred.Deferred<A, never>>
  readonly seq: number
}

export interface TtlCache<A> {
  /** Cached read. `produce` runs only on a miss, and only once per key. */
  readonly get: <E, R>(key: string, produce: Effect.Effect<A, E, R>) => Effect.Effect<A, E, R>
  readonly invalidate: (key: string) => Effect.Effect<void>
  readonly clear: Effect.Effect<void>
  readonly size: Effect.Effect<number>
}

export const make = <A>(options: {
  readonly ttlSeconds: number
  readonly maxEntries: number
}): Effect.Effect<TtlCache<A>> =>
  Effect.gen(function* () {
    const state = yield* Ref.make<State<A>>({
      entries: new Map(),
      inFlight: new Map(),
      seq: 0
    })
    const ttlMillis = Math.max(0, options.ttlSeconds) * 1000

    const put = (key: string, value: A, now: number) =>
      Ref.update(state, (s) => {
        const entries = new Map(s.entries)
        entries.set(key, { value, expiresAtMillis: now + ttlMillis, seq: s.seq })

        // Bounded: evict oldest insertions until we are back under the cap.
        if (entries.size > options.maxEntries) {
          const ordered = Array.from(entries.entries()).sort((a, b) => a[1].seq - b[1].seq)
          const excess = entries.size - options.maxEntries
          for (let i = 0; i < excess; i += 1) entries.delete(ordered[i]![0])
        }
        return { ...s, entries, seq: s.seq + 1 }
      })

    const get: TtlCache<A>["get"] = (key, produce) =>
      Effect.gen(function* () {
        // TTL 0 disables the cache entirely, matching the Python settings.
        if (ttlMillis === 0) return yield* produce

        const now = yield* Clock.currentTimeMillis
        const current = yield* Ref.get(state)

        const hit = current.entries.get(key)
        if (hit !== undefined && hit.expiresAtMillis > now) return hit.value

        // Single-flight lives entirely in the atomic claim below. An earlier
        // version also had a non-atomic fast path here — read `inFlight`, join
        // if present — which was pure optimisation: the claim already closes
        // the read-then-modify race that path was trying to win. Mutation
        // testing exposed it as redundant (deleting EITHER path alone left the
        // guarantee intact, so no test could tell them apart) and PROMPT T says
        // prefer the boring single mechanism.
        const deferred = yield* Deferred.make<A, never>()
        const claimed = yield* Ref.modify(state, (s) => {
          if (s.inFlight.has(key)) return [false, s] as const
          const inFlight = new Map(s.inFlight)
          inFlight.set(key, deferred)
          return [true, { ...s, inFlight }] as const
        })

        if (!claimed) {
          const other = (yield* Ref.get(state)).inFlight.get(key)
          return other === undefined ? yield* produce : yield* Deferred.await(other)
        }

        // onExit, not a plain finally: an interrupted producer must still
        // release the slot, or the key deadlocks for every later caller.
        return yield* produce.pipe(
          Effect.onExit((exit) =>
            Effect.gen(function* () {
              if (exit._tag === "Success") {
                const at = yield* Clock.currentTimeMillis
                yield* put(key, exit.value, at)
                yield* Deferred.succeed(deferred, exit.value)
              } else {
                yield* Deferred.interrupt(deferred)
              }
              yield* Ref.update(state, (s) => {
                const inFlight = new Map(s.inFlight)
                inFlight.delete(key)
                return { ...s, inFlight }
              })
            })
          )
        )
      })

    return {
      get,
      invalidate: (key) =>
        Ref.update(state, (s) => {
          const entries = new Map(s.entries)
          entries.delete(key)
          return { ...s, entries }
        }),
      clear: Ref.update(state, (s) => ({ ...s, entries: new Map() })),
      size: Ref.get(state).pipe(Effect.map((s) => s.entries.size))
    } satisfies TtlCache<A>
  })
