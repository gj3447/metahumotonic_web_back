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
import { Conflict } from "../domain/Errors.js"

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
  /**
   * A BOUNDED transition, matching `app/store.py:164-168`:
   *   reviewed <- {new}
   *   archived <- {new, reviewed}
   *   spam     <- {new, reviewed}
   * Unknown id and a forbidden transition are indistinguishable to the caller
   * on purpose — both are 409, exactly as the Python does, so the endpoint is
   * not an existence oracle for record ids.
   */
  readonly triage: (
    id: string,
    patch: { readonly status: FeedbackStatus; readonly operatorNote: string; readonly now: string }
  ) => Effect.Effect<FeedbackRecord, Conflict>
  /** Permanently erase the record AND its contact address. Returns false if absent. */
  readonly erase: (id: string) => Effect.Effect<boolean>
  readonly close: Effect.Effect<void>
}

export class FeedbackStoreTag extends Context.Tag("FeedbackStore")<
  FeedbackStoreTag,
  FeedbackStore
>() {}

/** The transition table from `app/store.py:164-168`, verbatim. */
const ALLOWED_FROM: Record<FeedbackStatus, ReadonlySet<FeedbackStatus>> = {
  new: new Set(),
  reviewed: new Set(["new"]),
  archived: new Set(["new", "reviewed"]),
  spam: new Set(["new", "reviewed"])
}

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
          if (!ALLOWED_FROM[patch.status].has(before.status)) return [null, all] as const
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
              ? Effect.fail(
                  new Conflict({
                    reason: "feedback not found or transition is not allowed"
                  })
                )
              : Effect.succeed(updated)
          )
        ),

      erase: (id) =>
        Ref.modify(items, (all) => {
          const next = all.filter((it) => it.id !== id)
          return [next.length !== all.length, next] as const
        }),

      close: Effect.void
    } satisfies FeedbackStore
  })
)
