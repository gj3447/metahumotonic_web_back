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

Build first, then create a new, reviewable batch file without posting it:

```sh
bash ts/scripts/with-node.sh npm --prefix ts run build
bash ts/scripts/with-node.sh node ts/scripts/collect-fixed-observations.mjs \
  --run-id fixed-probe-YYYYMMDD-001 \
  --observed-at 2026-09-28T08:00:00.000Z \
  --ttl-seconds 900 \
  --output /secure/operator/fixed-probe-YYYYMMDD-001.json
```

The command makes its HTTP observations once and writes a new mode-0600 file;
it refuses to overwrite an existing plan. It prints the plan's SHA-256, receipt
ID, and result summary. A later probe can produce different outcomes even with
the same run ID, so re-running the plan command is never an idempotent replay.

After review, the dedicated platform PostgreSQL path can submit exactly those
saved bytes. `--write` does **not** run any probes and accepts no run, TTL, or
target option:

```sh
MHB_PLATFORM_INGEST_ORIGIN=operator-managed-origin \
MHB_PLATFORM_WRITE_KEY=operator-managed-write-key \
bash ts/scripts/with-node.sh node ts/scripts/collect-fixed-observations.mjs \
  --write --input /secure/operator/fixed-probe-YYYYMMDD-001.json
```

The collector makes one `POST /api/platform/v1/observations` attempt and never
retries it automatically. If the response is uncertain, rerun the exact same
`--write --input` command. The stored bytes, receipt ID, and observation IDs
remain unchanged for the existing append-only receipt replay contract. Neither
the origin nor key is printed. The saved file must contain exactly the two
fixed target observations, their collector IDs, shared observation time and
bounded TTL, and the fixed evidence source; a generic platform batch is
rejected. The write origin must be HTTPS, except for a loopback HTTP operator
or test endpoint.
