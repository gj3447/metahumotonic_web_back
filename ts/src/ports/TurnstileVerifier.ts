/** Server-side Turnstile boundary. No credential or token is logged.
 * Fixed URL, no redirects, bounded response size and timeout; no success cache.
 */
import { Context, Data, Effect, Layer, Redacted } from "effect"
import { AppConfigTag, type AppConfig } from "../Config.js"

export const SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
const MAX_RESPONSE_BYTES = 16_384
export class TurnstileUnavailable extends Data.TaggedError("TurnstileUnavailable")<{}> {}
export class TurnstileBadResponse extends Data.TaggedError("TurnstileBadResponse")<{}> {}
export type TurnstileSettings = Pick<AppConfig,
  "turnstileSecret" | "turnstileHostname" | "turnstileAction" | "turnstileFailOpen">
export type Exchange = (secret: string, token: string) => Effect.Effect<
  unknown, TurnstileUnavailable | TurnstileBadResponse>
export interface TurnstileVerifier {
  readonly verify: (token: string) => Effect.Effect<boolean>
}
export class TurnstileVerifierTag extends Context.Tag("TurnstileVerifier")<
  TurnstileVerifierTag, TurnstileVerifier>() {}

/** Pure interpretation; a truthy string is not a successful verification. */
export const acceptsVerification = (body: unknown, cfg: TurnstileSettings): boolean => {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return false
  const result = body as Record<string, unknown>
  return result.success === true &&
    (!cfg.turnstileHostname || result.hostname === cfg.turnstileHostname) &&
    (!cfg.turnstileAction || result.action === cfg.turnstileAction)
}

export const exchangeWithFetch: Exchange = (secret, token) => Effect.tryPromise({
  try: async (signal) => {
    const response = await fetch(SITEVERIFY_URL, {
      method: "POST", redirect: "error", signal,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ secret, response: token }).toString()
    })
    if (!response.ok || response.body === null) {
      void response.body?.cancel().catch(() => {})
      throw new TurnstileBadResponse()
    }
    const reader = response.body.getReader()
    const chunks: Uint8Array[] = []
    let bytes = 0
    try {
      while (true) {
        const next = await reader.read()
        if (next.done) break
        bytes += next.value.byteLength
        if (bytes > MAX_RESPONSE_BYTES) {
          void reader.cancel().catch(() => {})
          throw new TurnstileBadResponse()
        }
        chunks.push(next.value)
      }
    } finally { reader.releaseLock() }
    try { return JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown }
    catch { throw new TurnstileBadResponse() }
  },
  catch: (error) => error instanceof TurnstileBadResponse
    ? error : new TurnstileUnavailable()
})

export const makeTurnstileVerifier = (
  cfg: TurnstileSettings, exchange: Exchange = exchangeWithFetch
): TurnstileVerifier => ({
  verify: (token) => {
    const secret = Redacted.value(cfg.turnstileSecret)
    if (!secret) return Effect.succeed(true)
    // Invalid input never becomes valid under transport-only fail-open.
    if (!token || token.length > 2048) return Effect.succeed(false)
    return exchange(secret, token).pipe(
      Effect.timeoutFail({ duration: "5 seconds", onTimeout: () => new TurnstileUnavailable() }),
      Effect.map((body) => acceptsVerification(body, cfg)),
      Effect.catchTag("TurnstileBadResponse", () => Effect.succeed(false)),
      Effect.catchTag("TurnstileUnavailable", () => Effect.succeed(cfg.turnstileFailOpen))
    )
  }
})
export const TurnstileVerifierLive = Layer.effect(TurnstileVerifierTag,
  Effect.map(AppConfigTag, (cfg) => makeTurnstileVerifier(cfg)))
