import { Context, Effect, Layer, Redacted } from "effect"
import { MongoClient, type Db } from "mongodb"
import { AppConfigTag } from "../Config.js"
import { Unavailable } from "../domain/Errors.js"

export class MongoTag extends Context.Tag("Mongo")<MongoTag, { readonly db: Db | null }>() {}

/** One scoped pool for all native Mongo adapters; driver deadlines also bound promises. */
export const MongoLive = Layer.scoped(MongoTag, Effect.gen(function* () {
  const cfg = yield* AppConfigTag
  const uri = Redacted.value(cfg.mongoUri)
  if (!uri) return { db: null }
  const client = yield* Effect.acquireRelease(
    Effect.try({ try: () => new MongoClient(uri, {
      serverSelectionTimeoutMS: 3000, connectTimeoutMS: 3000, socketTimeoutMS: 5000,
      maxPoolSize: 10, retryWrites: true
    }), catch: () => new Unavailable({ reason: "invalid Mongo configuration" }) }).pipe(Effect.orDie),
    (client) => Effect.promise(() => client.close()).pipe(Effect.catchAllCause(() => Effect.void))
  )
  return { db: client.db(cfg.mongoDb) }
}))

export const mongoAttempt = <A>(operation: () => Promise<A>) => Effect.tryPromise({
  try: operation,
  catch: () => new Unavailable({ reason: "storage_unavailable" })
})
