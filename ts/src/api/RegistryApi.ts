import { HttpApiEndpoint, HttpApiGroup } from "@effect/platform"
import { Schema } from "effect"
import { NotFound } from "../domain/Errors.js"

export const RegistryGroup = HttpApiGroup.make("registry")
  .add(HttpApiEndpoint.get("discovery", "/api/mcp").addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("servers", "/api/mcp/servers").addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("server", "/api/mcp/servers/:name").setPath(Schema.Struct({ name: Schema.String.pipe(Schema.maxLength(200)) })).addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("manifest", "/api/mcp/manifest").addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("health", "/api/mcp/health").addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("status", "/api/mcp/status").setUrlParams(Schema.Struct({ format: Schema.optional(Schema.String) })).addSuccess(Schema.Unknown))
  .add(HttpApiEndpoint.get("vault", "/api/mcp/vault").addSuccess(Schema.Unknown))
  .addError(NotFound)
