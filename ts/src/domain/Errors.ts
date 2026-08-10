/**
 * The error channel.
 *
 * In the Python service a failure is an exception: invisible in the signature,
 * discovered at runtime. Here every way a request can fail is a tagged type
 * that shows up in `Effect<A, E, R>`'s `E` slot, so the compiler — not a test
 * run — tells you when a caller forgot to handle one.
 *
 * These mirror the HTTP surface of `app/routers/*.py` one-for-one.
 */
import { HttpApiSchema } from "@effect/platform"
import { Schema } from "effect"

/** 400 — the request itself is malformed beyond schema decoding. */
export class BadRequest extends Schema.TaggedError<BadRequest>()("BadRequest", {
  reason: Schema.String
}, HttpApiSchema.annotations({ status: 400 })) {}

/** 401 — a shared-key surface was called without a usable credential. */
export class Unauthorized extends Schema.TaggedError<Unauthorized>()("Unauthorized", {
  reason: Schema.String
}, HttpApiSchema.annotations({ status: 401 })) {}

/** 403 — authenticated, but this principal may not do this. */
export class Forbidden extends Schema.TaggedError<Forbidden>()("Forbidden", {
  reason: Schema.String
}, HttpApiSchema.annotations({ status: 403 })) {}

/** 404 — no such node / page / server. */
export class NotFound extends Schema.TaggedError<NotFound>()("NotFound", {
  reason: Schema.String
}, HttpApiSchema.annotations({ status: 404 })) {}

/** 409 — optimistic-concurrency clash on a wiki revision. */
export class Conflict extends Schema.TaggedError<Conflict>()("Conflict", {
  reason: Schema.String
}, HttpApiSchema.annotations({ status: 409 })) {}

/** 413 — body over `wiki_max_body_bytes`. */
export class PayloadTooLarge extends Schema.TaggedError<PayloadTooLarge>()("PayloadTooLarge", {
  reason: Schema.String,
  maxBytes: Schema.Number
}, HttpApiSchema.annotations({ status: 413 })) {}

/** 429 — rate limiter said no. Carries the retry hint the limiter computed. */
export class RateLimited extends Schema.TaggedError<RateLimited>()("RateLimited", {
  reason: Schema.String,
  retryAfterSeconds: Schema.Number
}, HttpApiSchema.annotations({ status: 429 })) {}

/**
 * 503 — an opt-in surface is switched off, or its backing store is unreachable
 * and the surface is configured fail-closed.
 *
 * This is the tag that encodes the Python service's central design rule:
 * every external dependency degrades rather than crashes, *except* where the
 * operator explicitly asked for fail-closed behaviour.
 */
export class Unavailable extends Schema.TaggedError<Unavailable>()("Unavailable", {
  reason: Schema.String
}, HttpApiSchema.annotations({ status: 503 })) {}

/** Anything the KG driver itself refused — kept distinct from `Unavailable`
 *  so a Cypher syntax error is never reported as "the KG is down". */
export class KgQueryFailed extends Schema.TaggedError<KgQueryFailed>()("KgQueryFailed", {
  reason: Schema.String
}, HttpApiSchema.annotations({ status: 502 })) {}

/**
 * 503 with the full readiness payload.
 *
 * `app/routers/meta.py:75-77` returns 503 *carrying the same body* when the
 * wiki plane is required but not live. Modelling it as an error keeps that
 * status/body pairing in the type rather than in a handler branch.
 */
export class NotReady extends Schema.TaggedError<NotReady>()("NotReady", {
  status: Schema.Literal("not_ready"),
  kg_live: Schema.Boolean,
  wiki_required: Schema.Boolean,
  wiki_live: Schema.Boolean,
  wiki_store_live: Schema.Boolean,
  wiki_rate_limit_live: Schema.Boolean,
  degraded: Schema.Boolean
}, HttpApiSchema.annotations({ status: 503 })) {}

/** Every error the public API may return. Attached once, at the API root. */
export const ApiError = Schema.Union(
  BadRequest,
  Unauthorized,
  Forbidden,
  NotFound,
  Conflict,
  PayloadTooLarge,
  RateLimited,
  Unavailable,
  KgQueryFailed
)

export type ApiError = Schema.Schema.Type<typeof ApiError>
