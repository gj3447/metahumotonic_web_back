/**
 * Feedback intake — port of `app/store.py`.
 *
 * The one rule that matters here: an in-memory acknowledgement is *not* the
 * same as a durable one. The Python service encodes that with a
 * `feedback_require_durable` flag checked at the call site; here `durable` is
 * a property of the store itself, so the handler cannot forget to look.
 */
import { Context, Effect, Layer, Ref } from "effect"
import { FeedbackRecord } from "../domain/Contracts.js"
import type { FeedbackRequest, FeedbackStatus } from "../domain/Contracts.js"
import { NotFound } from "../domain/Errors.js"

export interface SaveResult {
  readonly id: string
  /** `stored` ⇒ it survived to a real database. `accepted` ⇒ memory only. */
  readonly status: "stored" | "accepted"
}

export interface FeedbackStore {
  /** False for the in-memory fallback. The feedback handler consults this
   *  before acknowledging when `feedbackRequireDurable` is on. */
  readonly durable: boolean
  readonly ensureIndexes: Effect.Effect<void>
  readonly save: (
    input: FeedbackRequest,
    meta: { readonly now: string; readonly id: string }
  ) => Effect.Effect<SaveResult>
  readonly list: (opts: {
    readonly limit: number
  }) => Effect.Effect<{ readonly items: ReadonlyArray<FeedbackRecord>; readonly count: number }>
  readonly triage: (
    id: string,
    patch: { readonly status: FeedbackStatus; readonly operatorNote: string; readonly now: string }
  ) => Effect.Effect<FeedbackRecord, NotFound>
  readonly close: Effect.Effect<void>
}

export class FeedbackStoreTag extends Context.Tag("FeedbackStore")<
  FeedbackStoreTag,
  FeedbackStore
>() {}

/**
 * In-memory store — the zero-infra default, matching `mongo_uri = ""`.
 *
 * Newest-first, and it never reports itself as durable.
 */
export const FeedbackStoreMemory = Layer.effect(
  FeedbackStoreTag,
  Effect.gen(function* () {
    const items = yield* Ref.make<ReadonlyArray<FeedbackRecord>>([])

    return {
      durable: false,
      ensureIndexes: Effect.void,

      save: (input, meta) =>
        Ref.update(items, (current) => [
          new FeedbackRecord({
            id: meta.id,
            created_at: meta.now,
            type: input.type,
            subject: input.subject,
            body: input.body,
            email: input.email,
            source_path: input.source_path,
            contact_consent: input.contact_consent,
            status: "new",
            operator_note: "",
            reviewed_at: null
          }),
          ...current
        ]).pipe(Effect.as({ id: meta.id, status: "accepted" as const })),

      list: ({ limit }) =>
        Ref.get(items).pipe(
          Effect.map((all) => ({ items: all.slice(0, limit), count: all.length }))
        ),

      triage: (id, patch) =>
        Ref.modify(items, (all) => {
          const idx = all.findIndex((it) => it.id === id)
          if (idx === -1) return [null, all] as const
          const before = all[idx]!
          const after = new FeedbackRecord({
            ...before,
            status: patch.status,
            operator_note: patch.operatorNote,
            reviewed_at: patch.now
          })
          const next = all.slice()
          next[idx] = after
          return [after, next] as const
        }).pipe(
          Effect.flatMap((updated) =>
            updated === null
              ? Effect.fail(new NotFound({ reason: `no feedback record ${id}` }))
              : Effect.succeed(updated)
          )
        ),

      close: Effect.void
    } satisfies FeedbackStore
  })
)
