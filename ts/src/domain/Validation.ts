/**
 * 422 Unprocessable Entity — FastAPI's validation status.
 *
 * Twelve Python tests assert 422 for the surface this port already covers
 * (`tests/test_feedback.py:37,72,78,191,201,202`,
 * `tests/test_research_endpoints.py:64,65,66,72,277,278`), so it is a pinned
 * contract, not a detail.
 *
 * Getting it required a design choice. `@effect/platform` raises
 * `HttpApiDecodeError` — a built-in fixed at 400 — when an endpoint's declared
 * schema rejects a request, and a response-rewriting middleware does not work:
 * the framework re-serialises the error after the middleware returns (verified
 * — `setStatus` produced 422 and the client still received 400).
 *
 * So the split below. The endpoint declares the *wire shape* (field names and
 * base types, which is what OpenAPI needs), and the *constraints* are applied
 * inside the handler through `validate`, which fails with this error. The
 * constraints are not duplicated: the strict schema is built by piping filters
 * onto the same wire schema.
 */
import { HttpApiSchema } from "@effect/platform"
import { Effect, ParseResult, Schema } from "effect"

export class ValidationIssue extends Schema.Class<ValidationIssue>("ValidationIssue")({
  /** Dotted path to the offending field, e.g. `limit` or `body`. */
  path: Schema.String,
  message: Schema.String
}) {}

export class ValidationFailed extends Schema.TaggedError<ValidationFailed>()(
  "ValidationFailed",
  {
    reason: Schema.String,
    issues: Schema.Array(ValidationIssue)
  },
  HttpApiSchema.annotations({ status: 422 })
) {}

const issuesOf = (error: ParseResult.ParseError): ReadonlyArray<ValidationIssue> => {
  const formatted = ParseResult.ArrayFormatter.formatErrorSync(error)
  return formatted.map(
    (i) =>
      new ValidationIssue({
        path: i.path.length === 0 ? "" : i.path.join("."),
        message: i.message
      })
  )
}

/**
 * Decode `input` with `schema`, failing 422 rather than 400.
 *
 * `errors: "all"` so a client sees every problem at once instead of fixing
 * them one round-trip at a time.
 */
export const validate = <A, I>(
  schema: Schema.Schema<A, I>,
  input: unknown
): Effect.Effect<A, ValidationFailed> =>
  Schema.decodeUnknown(schema, { errors: "all" })(input).pipe(
    Effect.mapError((error) => {
      const issues = issuesOf(error)
      return new ValidationFailed({
        reason:
          issues.length === 0
            ? "validation failed"
            : issues.map((i) => (i.path === "" ? i.message : `${i.path}: ${i.message}`)).join("; "),
        issues
      })
    })
  )
