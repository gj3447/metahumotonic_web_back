/**
 * Response-shaping middleware — the parts of the Python contract that live in
 * headers and status codes rather than in a handler's return value.
 *
 * Both of these were missing from the first cut of this port, and both are
 * things a client or an operator can observe.
 */
import { HttpApiBuilder, HttpServerRequest, HttpServerResponse } from "@effect/platform"
import type { HttpApp, HttpBody } from "@effect/platform"
import { Effect } from "effect"

/**
 * Validation failures answer 422, matching FastAPI.
 *
 * `HttpApiDecodeError` is a built-in with a fixed 400, and `Schema` refinement
 * failures surface through it. FastAPI answers 422 for the same class of
 * failure (`Query(..., ge=1, le=100)`, a rejected request body), so a client
 * that branches on the status sees a different answer from the two services.
 *
 * Only a decode error is rewritten. A handler that deliberately fails with
 * `BadRequest` still answers 400, which is what the KG proxy relies on to
 * distinguish "your Cypher is wrong" from "your request is malformed".
 */
/** A response body is one of several shapes; only some carry readable bytes. */
const bodyText = (body: HttpBody.HttpBody): string => {
  if (body._tag === "Uint8Array") return new TextDecoder().decode(body.body)
  if (body._tag === "Raw") {
    const raw = (body as { body: unknown }).body
    return typeof raw === "string" ? raw : JSON.stringify(raw)
  }
  return ""
}

export const decodeErrorsAre422 = <E, R>(app: HttpApp.Default<E, R>): HttpApp.Default<E, R> =>
  Effect.gen(function* () {
    const response = yield* app
    if (process.env["MHB_DEBUG_MW"] === "1") {
      yield* Effect.logInfo(`[mw] status=${response.status} bodyTag=${response.body._tag}`)
    }
    if (response.status !== 400) return response

    const text = bodyText(response.body)
    const isDecodeError = text.includes('"HttpApiDecodeError"')
    if (process.env["MHB_DEBUG_MW"] === "1") {
      yield* Effect.logInfo(`[mw] textLen=${text.length} isDecodeError=${isDecodeError}`)
    }
    if (!isDecodeError) return response
    const rewritten = HttpServerResponse.setStatus(response, 422)
    if (process.env["MHB_DEBUG_MW"] === "1") {
      yield* Effect.logInfo(`[mw] rewritten.status=${rewritten.status}`)
    }
    return rewritten
  })

/**
 * `Cache-Control: private, no-store` on the operator plane.
 *
 * `app/routers/feedback.py:107,127,155` sets it on every `/internal/*`
 * response. Without it an intermediary may cache an inbox page containing
 * submitters' contact addresses.
 */
export const operatorPlaneIsNeverCached = <E, R>(
  app: HttpApp.Default<E, R>
): HttpApp.Default<E, R> =>
  Effect.gen(function* () {
    const request = yield* HttpServerRequest.HttpServerRequest
    const response = yield* app
    if (process.env["MHB_DEBUG_MW"] === "1") {
      yield* Effect.logInfo(`[mw] url=${JSON.stringify(request.url)}`)
    }
    // `url` may be a path or an absolute URL depending on the adapter.
    if (!/^(?:https?:\/\/[^/]+)?\/internal\//.test(request.url)) return response
    return HttpServerResponse.setHeaders(response, {
      "cache-control": "private, no-store",
      pragma: "no-cache"
    })
  })

/**
 * The same no-store stamp, as an `HttpApi`-level middleware Layer.
 *
 * `toWebHandler({ middleware })` and `HttpApiBuilder.serve(middleware)` both
 * run their middleware — verified by logging — but the response it returns is
 * discarded before the client sees it. `HttpApiBuilder.middleware` composes
 * *inside* the API instead of around it, which is the placement that actually
 * shapes the response.
 */
export const OperatorPlaneNoStore = HttpApiBuilder.middleware(operatorPlaneIsNeverCached)
