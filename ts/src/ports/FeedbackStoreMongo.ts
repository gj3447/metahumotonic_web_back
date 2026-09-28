import { Effect, Layer, Schema } from "effect"
import type { Document } from "mongodb"
import { AppConfigTag } from "../Config.js"
import { FeedbackRecord } from "../domain/Contracts.js"
import { Conflict, Unavailable } from "../domain/Errors.js"
import { FeedbackStoreMemory, FeedbackStoreTag, type FeedbackStore } from "./FeedbackStore.js"
import { MongoTag, mongoAttempt } from "./Mongo.js"

const decodeRecord = (row: Document): Effect.Effect<FeedbackRecord, Unavailable> =>
  Schema.decodeUnknown(FeedbackRecord)({
    ...row, id: String(row["_id"]),
    created_at: row["created_at"] instanceof Date ? row["created_at"].toISOString() : row["created_at"],
    reviewed_at: row["reviewed_at"] instanceof Date ? row["reviewed_at"].toISOString() : row["reviewed_at"] ?? null
  }).pipe(Effect.mapError(() => new Unavailable({ reason: "invalid stored feedback" })))

export const FeedbackStoreLive = Layer.unwrapEffect(Effect.gen(function* () {
  const cfg = yield* AppConfigTag
  const { db } = yield* MongoTag
  if (!db) return FeedbackStoreMemory
  const collection = db.collection<Document & { _id: string }>(cfg.mongoFeedbackCollection)
  const ensureIndexes = mongoAttempt(async () => {
    if (cfg.feedbackTtlDays <= 0) return
    const ttl = cfg.feedbackTtlDays * 86400
    const indexes = await collection.listIndexes().toArray().catch((error: unknown) => {
      if (typeof error === "object" && error !== null && "code" in error && error.code === 26) return []
      throw error
    })
    const existing = indexes.find((index) => Object.keys(index.key).length === 1 && index.key["created_at"] === 1)
    if (existing?.expireAfterSeconds === ttl) return
    // Updating retention is an explicit database migration, never a startup drop.
    if (existing) throw new Error("feedback TTL migration required")
    await collection.createIndex({ created_at: 1 }, { name: "feedback_created_at_ttl", expireAfterSeconds: ttl })
  })
  const store: FeedbackStore = {
    durable: true,
    ensureIndexes,
    save: (input, meta) => mongoAttempt(async () => {
      const result = await collection.insertOne({
        _id: meta.id, created_at: new Date(meta.now), type: input.type,
        subject: input.subject, body: input.body, email: input.email,
        source_path: input.source_path, contact_consent: input.contact_consent,
        status: "new", operator_note: "", reviewed_at: null
      }, { writeConcern: { w: "majority", wtimeoutMS: 5000 } })
      if (!result.acknowledged) throw new Error("unacknowledged write")
      return { id: meta.id, status: "stored" as const }
    }).pipe(Effect.tap((result) => Effect.logInfo("feedback_durably_stored").pipe(
      Effect.annotateLogs({ cid: result.id, backend: "mongo" })
    ))),
    list: ({ limit }) => mongoAttempt(() => collection.find({}, { projection: { source_ip: 0, user_agent: 0 } })
      .sort({ created_at: -1 }).limit(limit).toArray()).pipe(
      Effect.flatMap((rows) => Effect.forEach(rows, decodeRecord)),
      Effect.map((items) => ({ items, count: items.length }))
    ),
    triage: (id, patch) => {
      const allowed = patch.status === "reviewed" ? ["new"] : patch.status === "new" ? [] : ["new", "reviewed"]
      return mongoAttempt(() => collection.findOneAndUpdate({
        _id: id, $or: [{ status: { $in: allowed } }, ...(allowed.includes("new") ? [{ status: { $exists: false } }] : [])]
      }, { $set: { status: patch.status, operator_note: patch.operatorNote, reviewed_at: new Date(patch.now) } },
      { returnDocument: "after" })).pipe(Effect.flatMap((row): Effect.Effect<FeedbackRecord, Conflict | Unavailable> => row === null
        ? Effect.fail(new Conflict({ reason: "feedback not found or transition is not allowed" }))
        : decodeRecord(row)))
    },
    erase: (id) => mongoAttempt(() => collection.deleteOne({ _id: id }, { writeConcern: { w: "majority", wtimeoutMS: 5000 } }))
      .pipe(Effect.map((result) => result.deletedCount === 1)),
    close: Effect.void
  }
  // A transient outage remains recoverable, but is never reported as durable success.
  yield* ensureIndexes.pipe(Effect.catchAll(() => Effect.logWarning("feedback index unavailable; verify Mongo retention before release")))
  return Layer.succeed(FeedbackStoreTag, store)
}))
