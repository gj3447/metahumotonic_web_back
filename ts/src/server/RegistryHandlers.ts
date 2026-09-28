import { HttpApiBuilder, HttpServerResponse } from "@effect/platform"
import { Effect } from "effect"
import { Api } from "../api/Api.js"
import { McpRegistryTag, REGISTRY_SCHEMA, VAULT_SPEC, registryStatusText } from "../ports/McpRegistry.js"

export const RegistryHandlers = HttpApiBuilder.group(Api, "registry", (handlers) => handlers
  .handle("discovery", () => Effect.succeed({ schema: REGISTRY_SCHEMA, source: "live", name: "metahumotonic MCP registry",
    description: "Company MCP registry. Operator CLI owns registry writes; /mcp exposes the authenticated platform gateway.",
    dashboard: "https://metahumotonic.com/mcp/", credential_vault: VAULT_SPEC,
    endpoints: ["manifest", "servers", "servers/{name}", "health", "status", "vault"].map((name) => ({ path: `/api/mcp/${name}`, method: "GET" })),
    mcp_endpoint: "/mcp"
  }))
  .handle("servers", () => Effect.flatMap(McpRegistryTag, (registry) => registry.read("servers")))
  .handle("server", ({ path }) => Effect.flatMap(McpRegistryTag, (registry) => registry.read("server", path.name)))
  .handle("manifest", () => Effect.flatMap(McpRegistryTag, (registry) => registry.read("manifest")))
  .handle("health", () => Effect.flatMap(McpRegistryTag, (registry) => registry.read("health")))
  .handle("status", ({ urlParams }) => Effect.flatMap(McpRegistryTag, (registry) => registry.read("status")).pipe(
    Effect.map((payload) => urlParams.format === "text" ? HttpServerResponse.text(registryStatusText(payload)) : payload)))
  .handle("vault", () => Effect.flatMap(McpRegistryTag, (registry) => registry.read("vault")))
)
