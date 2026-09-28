# Metahumotonic company backend — TypeScript / Effect

This is the candidate HTTP and MCP entrypoint built by the root Dockerfile and Compose.
It uses strict TypeScript, Effect services and scoped Layers. Production and
HTTP tests consume the same composition in `src/server/Composition.ts`.
The current VM100 public API still runs two Python `uvicorn` replicas; the
TypeScript image has not replaced their public traffic. See the
[reality assessment](../docs/BACKEND_REALITY_GRAPH.md) for code, deployment,
simulation and retirement states with their evidence.

Native services cover the existing KG/research APIs, durable Mongo feedback,
Redis rate limiting, the Mongo MCP registry, the evidence-backed program graph
and the authenticated Streamable HTTP MCP gateway. Wiki/ontology retain their
Python domain owner through an explicit private boundary. Existing CLI commands
continue using their current contracts. Python removal and a production traffic
switch are not implied.

See [the company backend guide](../docs/COMPANY_BACKEND.md) for the inventory,
authority boundaries, environment settings, endpoints and rollout requirements.
The [platform architecture](../docs/COMPANY_PLATFORM_ARCHITECTURE.md) describes
197 catalogued assets, searchable REST/MCP inventory, expiring observations,
independently checked JSON-LD/SHACL and the next PostgreSQL/telemetry/auth steps.
The optional [PostgreSQL inventory store](../docs/PLATFORM_POSTGRES.md) now persists
versioned definitions, append-only observations and idempotent ingest receipts.
Its explicit migration/import CLI and runtime role are separate from owner Wiki/product stores.

```sh
bash scripts/with-node.sh npm ci
bash scripts/with-node.sh npm run typecheck
bash scripts/with-node.sh npm test
bash scripts/with-node.sh npm run smoke
bash scripts/with-node.sh npm run test:persistence
bash scripts/with-node.sh npm run dev
```

`test:persistence` needs `redis-server` (or `MHB_TEST_REDIS_BINARY`) and downloads
a disposable Mongo binary. It only binds loopback, creates temporary databases,
and never reads application `.env`. Ordinary tests skip the external persistence
suite unless `MHB_TEST_MONGO_URI` and `MHB_TEST_REDIS_URL` explicitly point to test
instances. CI provides disposable service containers for that suite.

`MHB_PLATFORM_READ_KEY` and `MHB_PLATFORM_WRITE_KEY` are separate from existing
KG, feedback-admin and ontology keys. Empty keys disable company APIs and MCP.
The bundled MCP bindings are registered but remain unconfigured until their URL
environment variables are supplied. Catalog membership never grants a call.

The catalog is a labelled snapshot; `configured` only describes configuration.
Neither is a live health assertion. The gateway calls allowlisted upstream tools
without retry and leaves durable execution/reconciliation to each owner.
