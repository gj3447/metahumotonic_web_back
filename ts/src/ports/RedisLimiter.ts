import { Context, Effect, Layer, Redacted } from "effect"
import { createClient } from "redis"
import { createHash, randomUUID } from "node:crypto"
import { AppConfigTag } from "../Config.js"
import { Unavailable } from "../domain/Errors.js"
import { denyAll, makeInProcess, type LimiterPolicy, type RateLimiter } from "./RateLimiter.js"

/** Redis TIME gives every replica the same clock; check-and-record is one atomic script. */
export const SLIDING_WINDOW = `
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local window = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, math.max(1, math.ceil((tonumber(oldest[2]) + window - now) / 1000))}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window)
return {1, 0}`

type Client = ReturnType<typeof createClient>
export class RedisTag extends Context.Tag("Redis")<RedisTag, { readonly client: Client | null }>() {}
export const RedisLive = Layer.scoped(RedisTag, Effect.gen(function* () {
  const cfg = yield* AppConfigTag
  const url = Redacted.value(cfg.redisUrl)
  if (!url) return { client: null }
  const client = yield* Effect.acquireRelease(Effect.sync(() => {
    const client = createClient({ url, socket: { connectTimeout: 3000, reconnectStrategy: (attempt) => Math.min(1000 + attempt * 100, 5000) }, disableOfflineQueue: true })
    client.on("error", () => { /* Per-operation typed failures; never log credential-bearing URLs. */ })
    return client
  }), (client) => Effect.sync(() => { if (client.isOpen) client.destroy() }))
  yield* Effect.forkScoped(Effect.tryPromise({ try: () => client.connect(), catch: () => new Unavailable({ reason: "Redis unavailable" }) })
    .pipe(Effect.catchAll(() => Effect.void)))
  return { client }
}))

export const redisLimiterLayer = <I>(tag: Context.Tag<I, RateLimiter>, namespace: string, policy: LimiterPolicy) =>
  Layer.effect(tag, Effect.gen(function* () {
    const { client } = yield* RedisTag
    const fallback = policy.failClosed ? denyAll("distributed rate limiter unavailable") : yield* makeInProcess(policy)
    if (!client) return fallback
    const call = <A>(f: () => Promise<A>) => Effect.tryPromise({ try: f, catch: () => new Unavailable({ reason: "distributed rate limiter unavailable" }) })
      .pipe(Effect.timeoutFail({ duration: "3 seconds", onTimeout: () => new Unavailable({ reason: "distributed rate limiter unavailable" }) }))
    return {
      check: (key: string) => call(() => client.eval(SLIDING_WINDOW, {
        // Share Python's feedback window during a rolling runtime transition.
        keys: [namespace === "feedback" ? `mhb:rl:${key}` : `mhb:rate:v2:${namespace}:${createHash("sha256").update(key).digest("hex")}`],
        arguments: [String(policy.windowSeconds * 1000), String(policy.maxEvents), randomUUID()]
      })).pipe(Effect.flatMap((value) => Array.isArray(value) && value.length === 2
        ? Effect.succeed({ allowed: Number(value[0]) === 1, retryAfterSeconds: Number(value[1]) })
        : Effect.fail(new Unavailable({ reason: "invalid rate limit response" }))),
      Effect.catchAll(() => fallback.check(key))),
      ready: call(() => client.ping()).pipe(Effect.map((value) => value === "PONG"), Effect.catchAll(() => Effect.succeed(false))),
      // No production-wide flush; Redis TTL owns cleanup.
      reset: Effect.void
    } satisfies RateLimiter
  }))
