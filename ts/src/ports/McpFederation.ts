import { Context, Effect, Layer, Redacted } from "effect"
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js"
import type { Tool, CallToolResult } from "@modelcontextprotocol/sdk/types.js"
import { PlatformConfigTag, type McpBinding } from "../platform/Config.js"
import { boundedFetch } from "../platform/BoundedHttp.js"
import { Forbidden, NotFound, Unavailable } from "../domain/Errors.js"

export interface McpFederation {
  readonly tools: (serverId: string, canWrite: boolean) => Effect.Effect<ReadonlyArray<Tool>, NotFound | Unavailable>
  readonly call: (serverId: string, tool: string, args: Record<string, unknown>, canWrite: boolean) => Effect.Effect<CallToolResult, NotFound | Forbidden | Unavailable>
}
export class McpFederationTag extends Context.Tag("McpFederation")<McpFederationTag, McpFederation>() {}
export const McpFederationLive = Layer.effect(McpFederationTag, Effect.gen(function* () {
  const cfg = yield* PlatformConfigTag
  const lookup = (id: string): Effect.Effect<McpBinding, NotFound> => {
    const binding = cfg.bindings.find((item) => item.id === id)
    return binding ? Effect.succeed(binding) : Effect.fail(new NotFound({ reason: "MCP binding not found" }))
  }
  const withClient = <A>(binding: McpBinding, f: (client: Client, signal: AbortSignal) => Promise<A>) =>
    Effect.tryPromise({ try: async (signal) => {
      if (binding.url === null) throw new Error("unconfigured MCP endpoint")
      const deadline = AbortSignal.any([signal, AbortSignal.timeout(cfg.timeoutMs)])
      const client = new Client({ name: "metahumotonic-platform", version: "1.0.0" })
      const token = Redacted.value(binding.token)
      const transport = new StreamableHTTPClientTransport(new URL(Redacted.value(binding.url)), {
        requestInit: { headers: token ? { Authorization: `Bearer ${token}` } : {}, signal: deadline },
        fetch: boundedFetch(cfg.timeoutMs, 1_048_576, deadline)
      })
      try {
        // SDK v1 declares optional callbacks/sessionId differently from its own Transport
        // under exactOptionalPropertyTypes. This assertion is confined to that SDK boundary.
        await client.connect(transport as Transport, { timeout: cfg.timeoutMs, signal: deadline })
        return await f(client, deadline)
      } finally { await client.close().catch(() => undefined) }
    }, catch: () => new Unavailable({ reason: "MCP upstream unavailable or response refused" }) })
  return {
    tools: (id, canWrite) => lookup(id).pipe(Effect.flatMap((binding) => withClient(binding, async (client, signal) => {
      const allowed = new Set(binding.tools.filter((tool) => canWrite || tool.access === "read").map((tool) => tool.name))
      const tools: Tool[] = []
      let cursor: string | undefined
      for (let page = 0; page < 4; page++) {
        const result = await client.listTools(cursor ? { cursor } : {}, { signal, timeout: cfg.timeoutMs })
        tools.push(...result.tools.filter((tool) => allowed.has(tool.name)))
        if (result.nextCursor === undefined) return tools
        cursor = result.nextCursor
      }
      throw new Error("MCP tool inventory exceeded page budget")
    }))),
    call: (id, tool, args, canWrite) => lookup(id).pipe(Effect.flatMap((binding): Effect.Effect<CallToolResult, Forbidden | Unavailable> => {
      const policy = binding.tools.find((item) => item.name === tool)
      if (!policy || (policy.access === "write" && !canWrite)) return Effect.fail(new Forbidden({ reason: "MCP tool is not allowed for this credential" }))
      return withClient(binding, async (client, signal) => {
        const result = await client.callTool({ name: tool, arguments: args }, undefined, { signal, timeout: cfg.timeoutMs })
        // Client's older compatibility result has no content. Refuse it at this boundary.
        if (!("content" in result) || !Array.isArray(result.content)) throw new Error("invalid tool result")
        return result as CallToolResult
      })
    }))
  } satisfies McpFederation
}))
