# Domain projection contract (2026-09-17)

The Python fix in b96487f is retained without replacement: valid records survive,
malformed records are omitted with X-Records-Omitted and X-Data-Quality. If all
records are invalid the route returns explicit 503, not a false empty success.
A live empty list is different from an unavailable live backend (snapshot).
No graph row is mutated and no missing count is filled with a made-up zero.

The parallel unknown-count sentinel proposal was tested but not adopted for
production. Future Effect route parity must include body, status, quality/source
headers, omissions, caching and the all-invalid case, not only successful rows.
The TypeScript candidate is not a replacement for the stateful Python service.

Use ts/scripts/with-node.sh npm test for the exact pinned Node toolchain.
Tests use one worker to respect process/cgroup limits. Socket smoke requests to
localhost are excluded from package proxies.
