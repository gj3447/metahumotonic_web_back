/**
 * Identifiers and timestamps as a *service*, not as ambient globals.
 *
 * `uuid4()` and `datetime.utcnow()` are the two things that make a Python
 * handler untestable without patching. Behind a tag they become injectable:
 * production gets randomness and the wall clock, a test gets a counter and a
 * frozen instant, and the handler is unchanged either way.
 */
import { Context, Effect, Layer, Ref } from "effect"
import { randomUUID } from "node:crypto"

export interface Ids {
  readonly newId: Effect.Effect<string>
  /** ISO-8601 UTC, matching what the Mongo documents already store. */
  readonly nowIso: Effect.Effect<string>
}

export class IdsTag extends Context.Tag("Ids")<IdsTag, Ids>() {}

export const IdsLive = Layer.succeed(IdsTag, {
  newId: Effect.sync(() => randomUUID()),
  nowIso: Effect.sync(() => new Date().toISOString())
})

/** Deterministic ids and a fixed instant, for tests. */
export const IdsDeterministic = (options?: {
  readonly prefix?: string
  readonly instant?: string
}): Layer.Layer<IdsTag> =>
  Layer.effect(
    IdsTag,
    Effect.gen(function* () {
      const counter = yield* Ref.make(0)
      const prefix = options?.prefix ?? "test"
      const instant = options?.instant ?? "2026-01-01T00:00:00.000Z"
      return {
        newId: Ref.updateAndGet(counter, (n) => n + 1).pipe(
          Effect.map((n) => `${prefix}-${String(n).padStart(4, "0")}`)
        ),
        nowIso: Effect.succeed(instant)
      }
    })
  )
