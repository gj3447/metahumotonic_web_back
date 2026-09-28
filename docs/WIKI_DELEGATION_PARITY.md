# Private Wiki·ontology delegation parity gate

`ops/run-ts-backend-canary-vm100.sh` starts an unpublished, resource-limited
TS/Effect candidate in the network namespace of one existing Python replica.
It does not load an operation environment file, publish a port, change ingress,
or mutate Wiki pages or other domain data. Legacy Wiki GETs may increment the
Python read limiter in Redis or in-process memory. Its delegation gate compares the
Python domain owner at loopback with the TS proxy for these fixed paths:

- `GET` and `HEAD /api/wiki/v1`
- `GET` and `HEAD /api/wiki/v1/pages?limit=1`
- `GET` and `HEAD` for the fixed missing Wiki slug
- `GET` and `HEAD /api/v1/ontology/schema`

For each pair it requires equal status and `content-type`, `cache-control`,
`etag`, `location`, and `www-authenticate` headers. GET bodies are bounded to
1 MiB and compared only by in-process SHA-256; no response body, URL with
credentials, cookie, token, or header values are written to the receipt. The
proxy must add its `x-mhb-service=legacy-domain` boundary marker. Ontology may
be disabled; equal 401/404/503 behavior is still meaningful parity.

The canary receipt records only the check name
`wiki-ontology-read-parity`, exact commit, image ID, and isolation facts. A
PASS is evidence for fixed anonymous reads only. It does **not** validate
session issuance, cookies, CSRF, bearer-agent mutation, idempotency replay,
ETag conflict, page/revision write semantics, moderation authorization,
private-route ingress exclusion, Redis outage handling, or data durability.
Those tests require a disposable Wiki database/runtime canary before any
public route cutover. Python remains the Wiki and ontology owner throughout
this gate.

The first private read parity PASS and its exact image are recorded in
[`docs/evidence/company-private-wiki-delegation-read-canary-2026-09-28.json`](evidence/company-private-wiki-delegation-read-canary-2026-09-28.json).

## Disposable stateful delegation gate

The Wiki release canary additionally builds the default `Dockerfile` from the
same exact archive as the Python `Dockerfile.legacy` image. It starts that TS
image only on the canary Docker network, without a published port, and gives it
a newly written minimal environment: its only upstream is the disposable
Python canary. Mongo, Neo4j, Redis, and platform PostgreSQL configuration are
empty or disabled in the TS gateway. The Python canary environment also clears
Mongo and Neo4j and rewrites Redis and Wiki PostgreSQL to its disposable
instances.

The gateway issues a browser session, checks CSRF rejection and acceptance,
then issues an agent session and checks idempotent create replay, conflict, and
stale-head CAS through the TS proxy. Direct reads from the disposable Python
owner must observe the delegated writes. Container labels bind the gateway to
the exact commit and canary nonce; cleanup removes it before the existing
runtime helper removes the network, Redis, and work directory. The TS image is
label-checked against the archive commit and removed after a successful gate.
No production Wiki database, session, Redis, MongoDB, Neo4j, public ingress,
or runtime container is used.

## Standalone private run

The stateful gate is intentionally separate from `release-web-back-vm100.sh`.
That release controller continues to build and roll out only the Python owner.
Use `ops/run-ts-wiki-stateful-canary-vm100.sh` only after a data-01 operator has
created the exact receipt-owned disposable database named
`metahumotonic_wiki_canary_<commit12>_<nonce12>` through the existing Wiki
backup/restore and canary-database helpers. The controller never creates,
backs up, restores, or drops a database; the existing data helper remains the
only database cleanup authority.

The caller supplies two already-built VM100 images whose OCI revision labels
match the full commit: the Python `Dockerfile.legacy` image and the TS
`Dockerfile` image. It defaults to `dry-run`; `run` stages only root-owned
remote helper copies and starts the disposable network resources. `cleanup`
uses the same commit and nonce and invokes the existing label-bound runtime
cleanup helper. The controller does not build an image, so a successful build
followed by a failed canary invocation cannot leave a controller-created image.

```sh
MHB_WIKI_STATEFUL_COMMIT='<40-hex>' \
MHB_WIKI_STATEFUL_NONCE='<32-hex>' \
MHB_WIKI_STATEFUL_PYTHON_IMAGE='metahumotonic-web-back:<version>-x86' \
MHB_WIKI_STATEFUL_GATEWAY_IMAGE='metahumotonic-web-back-ts:<version>-x86' \
MHB_WIKI_STATEFUL_DATABASE='metahumotonic_wiki_canary_<commit12>_<nonce12>' \
  ops/run-ts-wiki-stateful-canary-vm100.sh dry-run
```
