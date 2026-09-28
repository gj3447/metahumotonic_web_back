# Fixed platform probe collector

`ts/scripts/collect-fixed-observations.mjs` creates fresh observations for a
small, code-owned GET allowlist. It exists to add time-bounded operational
evidence beside the dated catalog snapshot; it does not rewrite or refresh the
109 imported observations.

The current fixed targets are the public home responses for `service:website`
and `service:soopoolim`. A successful result only means the configured HTTP
status was returned. It does not establish container health, backend readiness,
deployment identity, feature completeness, or ownership.

The CLI has no URL, command, subject-ID, catalog-path, header, or concurrency
argument. Each GET uses a five-second timeout, no redirects, and does not read
the response body. At most two fixed probes run concurrently. Failure is stored
as a normal `failed` observation with only `timeout`, `network`, or
`unexpected-status` in the evidence note.

Build first, then create a reviewable batch without writing it:

```sh
bash ts/scripts/with-node.sh npm --prefix ts run build
bash ts/scripts/with-node.sh node ts/scripts/collect-fixed-observations.mjs \
  --run-id fixed-probe-YYYYMMDD-001 \
  --observed-at 2026-09-28T08:00:00.000Z \
  --ttl-seconds 900 --dry-run
```

`runId` and `observedAt` are required and form the receipt and observation
identities. Repeating the same probe inputs yields the same batch for the
existing append-only receipt replay contract. The collector never retries a
POST automatically. When an operator has reviewed the dry-run payload and the
dedicated platform PostgreSQL path is active, `--write` additionally requires
operator-injected `MHB_PLATFORM_INGEST_ORIGIN` and `MHB_PLATFORM_WRITE_KEY`.
Neither is printed. The request is only `POST /api/platform/v1/observations`.
