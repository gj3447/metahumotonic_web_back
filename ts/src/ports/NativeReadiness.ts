import { Context, Effect, Layer, Redacted } from "effect"
import { AppConfigTag } from "../Config.js"
import { MongoTag } from "./Mongo.js"
import { RedisTag } from "./RedisLimiter.js"
import { PlatformInventoryStoreTag } from "./PlatformInventoryStore.js"

export interface NativeReadiness {
  readonly read: Effect.Effect<{
    readonly mongo_required: boolean; readonly mongo_live: boolean
    readonly redis_required: boolean; readonly redis_live: boolean
    readonly platform_postgres_required: boolean; readonly platform_postgres_live: boolean
  }>
}
export class NativeReadinessTag extends Context.Tag("NativeReadiness")<NativeReadinessTag, NativeReadiness>() {}

/** Read the same pools used by the native adapters. No writes or index changes. */
export const NativeReadinessLive = Layer.effect(NativeReadinessTag, Effect.gen(function* () {
  const cfg = yield* AppConfigTag
  const { db } = yield* MongoTag
  const { client } = yield* RedisTag
  const inventory = yield* PlatformInventoryStoreTag
  const probe = (operation: () => Promise<boolean>) => Effect.tryPromise({ try: operation, catch: () => false })
    .pipe(Effect.timeoutOption("2 seconds"), Effect.map((value) => value._tag === "Some" && value.value), Effect.catchAll(() => Effect.succeed(false)))
  return { read: Effect.all({
    mongo_required: Effect.succeed(cfg.feedbackRequireDurable || Redacted.value(cfg.mongoUri).length > 0),
    mongo_live: db ? probe(async () => (await db.command({ ping: 1 }, { timeoutMS: 1500 }))["ok"] === 1) : Effect.succeed(false),
    redis_required: Effect.succeed(Redacted.value(cfg.redisUrl).length > 0),
    redis_live: client ? probe(async () => client.isReady && await client.ping() === "PONG") : Effect.succeed(false)
  }, { concurrency: "unbounded" }).pipe(Effect.zipWith(inventory.readiness, (native, postgres) => ({ ...native, ...postgres }))) } satisfies NativeReadiness
}))
