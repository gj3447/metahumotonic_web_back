import { HttpApiBuilder, HttpServerResponse } from "@effect/platform"
import { Effect } from "effect"
import { Api } from "../api/Api.js"
import { LearningHubTag } from "../ports/LearningHub.js"

const response = (part: "catalog" | "jsonld" | "usl") => Effect.map(LearningHubTag, (hub) => {
  const { jsonld, usl, ...catalog } = hub
  return HttpServerResponse.text(JSON.stringify(part === "catalog" ? catalog : part === "jsonld" ? jsonld : usl), {
    contentType: part === "jsonld" ? "application/ld+json" : "application/json",
    headers: { "cache-control": "public, max-age=300", "x-content-type-options": "nosniff", "x-hub-source-digest": hub.sourceDigest }
  })
})
export const LearningHubHandlers = HttpApiBuilder.group(Api, "learningHub", (handlers) => handlers
  .handle("catalog", () => response("catalog"))
  .handle("linkedData", () => response("jsonld"))
  .handle("usl", () => response("usl")))
