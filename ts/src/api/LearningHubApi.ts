import { HttpApiEndpoint, HttpApiGroup } from "@effect/platform"
import { Schema } from "effect"

export const LearningHubGroup = HttpApiGroup.make("learningHub")
  .add(HttpApiEndpoint.get("catalog", "/api/public/v1/hub").addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("linkedData", "/api/public/v1/hub/graph.jsonld").addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("usl", "/api/public/v1/hub/usl.json").addSuccess(Schema.Unknown))
