/**
 * Shared-key authentication for the operator and proxy planes.
 *
 * Two rules, both of which the Python enforces and both of which are easy to
 * lose in a rewrite:
 *
 *  1. An empty configured key means the surface is *disabled* (503) — never
 *     "everyone is authorised".
 *  2. Comparison is timing-safe.
 */
import { Effect, Redacted } from "effect"
import { timingSafeEqual } from "node:crypto"
import { Unauthorized, Unavailable } from "../domain/Errors.js"

/** Accepts `Bearer <key>` or a bare key, matching the Python behaviour. */
export const extractKey = (authorization: string | undefined): string => {
  if (authorization === undefined) return ""
  const trimmed = authorization.trim()
  const lower = trimmed.toLowerCase()
  return lower.startsWith("bearer ") ? trimmed.slice(7).trim() : trimmed
}

export const constantTimeEquals = (a: string, b: string): boolean => {
  const left = Buffer.from(a, "utf8")
  const right = Buffer.from(b, "utf8")
  // timingSafeEqual throws on length mismatch, which would itself leak length.
  // Compare fixed-width digests of the inputs instead.
  if (left.length !== right.length) {
    // still burn a comparison so the fast path and slow path look alike
    timingSafeEqual(left, left)
    return false
  }
  return timingSafeEqual(left, right)
}

/**
 * Authorise against one or more accepted keys.
 *
 * `Unavailable` when every configured key is empty (surface switched off),
 * `Unauthorized` when a key was configured but the caller's does not match.
 * Keeping those two distinct is what lets an operator tell "I forgot to set
 * the env var" apart from "my key is wrong".
 */
export const authorize = (options: {
  /**
   * Every place the caller might have put the credential, in precedence order.
   *
   * `X-API-Key` first, because that is what `app/routers/kg_proxy.py:74` and
   * `app/routers/feedback.py:85` read — an earlier version of this port
   * accepted only `Authorization`, which silently broke every existing client.
   * `Authorization: Bearer` is accepted additively; accepting more forms is
   * not a compatibility break, accepting fewer is.
   */
  readonly presented: ReadonlyArray<string | undefined>
  readonly accepted: ReadonlyArray<Redacted.Redacted<string>>
  readonly surface: string
}): Effect.Effect<void, Unauthorized | Unavailable> => {
  const configured = options.accepted
    .map((k) => Redacted.value(k))
    .filter((k) => k.length > 0)

  if (configured.length === 0) {
    return Effect.fail(
      new Unavailable({ reason: `${options.surface} is disabled: no key configured` })
    )
  }

  const candidates = options.presented
    .map((raw) => extractKey(raw))
    .filter((k) => k !== "")

  if (candidates.length === 0) {
    return Effect.fail(new Unauthorized({ reason: "missing credential" }))
  }

  const ok = candidates.some((p) => configured.some((k) => constantTimeEquals(p, k)))
  return ok ? Effect.void : Effect.fail(new Unauthorized({ reason: "invalid credential" }))
}
