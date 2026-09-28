# Public MCP directory scope

The public `/api/mcp/*` registry lists only reviewed metadata. It does not
publish connection recipes, credentials, internal addresses, or authorization
to call a tool. The operator-owned source for the first entries is
[`ops/mcp-public-directory-2026-09-28.json`](../ops/mcp-public-directory-2026-09-28.json).

Ontology and HSPINE are dated `verified` entries because the
[owner/REST/MCP readback](evidence/company-mcp-live-readback-2026-09-28.json)
checked their fixed read tools at 2026-09-28 07:08 UTC. The registry marks
their verification stale after 24 hours unless a new owner readback is
recorded. Maple Lineage is `available`: its development corpus has a declared
binding, but this directory has no live tool-call readback for it. Generic
`tools` capability hints are category inferred, not an activated tool list.

Other internal/admin/write MCP services remain in the private platform
catalogue. The public registry is a discovery surface, while the company
gateway's authenticated fixed allowlist controls actual calls.
