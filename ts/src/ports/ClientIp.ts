/**
 * Client-IP resolution — port of `app/netutil.py`.
 *
 * The first version of this port keyed the feedback rate limiter on the
 * submission's *subject text* (`Handlers.ts`), which meant a flood could
 * bypass the limiter by varying one field, and two unrelated users who chose
 * the same subject throttled each other. `trustProxy` was parsed and never
 * read, so the XFF-spoofing defence that `tests/test_hardening.py:58-81`
 * pins had no implementation at all.
 *
 * The rule this restores (PROM16 A3S2): prefer the *proxy-appended* client IP
 * — `CF-Connecting-IP`, else the RIGHTMOST `X-Forwarded-For` entry — but only
 * when `MHB_TRUST_PROXY` is set. The leftmost XFF entry is attacker-controlled;
 * taking it would be worse than having no limiter, because it looks like one.
 */
import { HttpServerRequest } from "@effect/platform"
import { Context, Effect, Layer } from "effect"
import { AppConfigTag } from "../Config.js"

export interface ClientIp {
  /** The rate-limit key for the current request. Never throws. */
  readonly key: Effect.Effect<string, never, HttpServerRequest.HttpServerRequest>
}

export class ClientIpTag extends Context.Tag("ClientIp")<ClientIpTag, ClientIp>() {}

/** Pure — the header-precedence rule, testable without a server. */
export const resolve = (input: {
  readonly trustProxy: boolean
  readonly cfConnectingIp: string | undefined
  readonly xForwardedFor: string | undefined
  readonly remoteAddress: string | undefined
}): string => {
  if (input.trustProxy) {
    const cf = input.cfConnectingIp?.trim()
    if (cf !== undefined && cf !== "") return cf

    const fwd = input.xForwardedFor
    if (fwd !== undefined && fwd.trim() !== "") {
      // RIGHTMOST, not leftmost: the leftmost entry is whatever the client
      // sent, the rightmost is what our own proxy appended.
      const parts = fwd.split(",")
      const last = parts[parts.length - 1]?.trim()
      if (last !== undefined && last !== "") return last
    }
  }
  return input.remoteAddress?.trim() || "unknown"
}

export const ClientIpLive: Layer.Layer<ClientIpTag, never, AppConfigTag> = Layer.effect(
  ClientIpTag,
  Effect.gen(function* () {
    const cfg = yield* AppConfigTag
    return {
      key: Effect.gen(function* () {
        const req = yield* HttpServerRequest.HttpServerRequest
        const remote = yield* HttpServerRequest.HttpServerRequest.pipe(
          Effect.flatMap((r) => r.remoteAddress),
          Effect.catchAll(() => Effect.succeed(undefined as string | undefined))
        )
        return resolve({
          trustProxy: cfg.trustProxy,
          cfConnectingIp: req.headers["cf-connecting-ip"],
          xForwardedFor: req.headers["x-forwarded-for"],
          remoteAddress: remote
        })
      })
    }
  })
)

/** Fixed key, for tests that do not care about the IP. */
export const ClientIpFixed = (key: string): Layer.Layer<ClientIpTag> =>
  Layer.succeed(ClientIpTag, { key: Effect.succeed(key) })
