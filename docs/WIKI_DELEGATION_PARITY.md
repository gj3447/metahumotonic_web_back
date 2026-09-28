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

The dedicated stateful helper consumes prebuilt exact-commit `Dockerfile` and
Python `Dockerfile.legacy` images. It starts the TS image only on the canary
Docker network, without a published port, and gives it a newly written minimal
environment: its only upstream is the disposable Python canary. Mongo, Neo4j,
Redis, and platform PostgreSQL configuration are empty or disabled in the TS
gateway. The Python canary environment copies only the Wiki database URL from
the root-only source file, rewrites it to the receipt-owned disposable database,
uses disposable Redis, and generates separate session and moderation secrets.

The gateway issues a browser session, checks CSRF rejection and acceptance,
then issues an agent session and checks idempotent create replay, conflict, and
stale-head CAS through the TS proxy. Direct reads from the disposable Python
owner must observe the delegated writes. Container labels bind the gateway to
the exact commit and canary nonce; cleanup removes only that gateway container
before the existing runtime helper removes the network, Redis, and work directory.
The prebuilt TS image is label-checked and deliberately retained because the
standalone controller did not create or own it.
No production Wiki database, session, Redis, MongoDB, Neo4j, public ingress,
or runtime container is used.

## Standalone private run

The stateful gate is intentionally separate from `release-web-back-vm100.sh`.
That release controller continues to build and roll out only the Python owner.
`ops/run-ts-wiki-stateful-canary-vm100.sh` is a repeatable private drill. It
requires the exact root-only `VERIFIED` `current-production` encrypted backup
receipt through `MHB_WIKI_STATEFUL_BACKUP_RECEIPT`. On data-01 it validates the
receipt, encrypted dump, and key hashes, then invokes the existing
receipt/COMMENT-owned canary database helper to create the exact disposable
database `metahumotonic_wiki_canary_<commit12>_<nonce12>`. The controller never
reads or prints backup key material; the existing data helper remains the only
database cleanup authority.

The caller supplies two already-built VM100 images whose OCI revision labels
match the full commit: the Python `Dockerfile.legacy` image and the TS
`Dockerfile` image. It defaults to `dry-run`; `run` stages only user-owned
remote helper copies and starts the disposable database and network resources.
On success or failure its EXIT cleanup removes only exact label-bound runtime
resources and tells the data helper to drop the exact receipt-owned database.
A drop failure stays durable in the data receipt and fails the controller.
`cleanup` retries that exact cleanup with the same commit and nonce. The controller does not build an image, so a successful build
followed by a failed canary invocation cannot leave a controller-created image.
The existing release workspace at `releases/<commit>` must be root-owned
`0700`; the runtime helper reserves its nonce-owned work directory there.

```sh
MHB_WIKI_STATEFUL_COMMIT='<40-hex>' \
MHB_WIKI_STATEFUL_NONCE='<32-hex>' \
MHB_WIKI_STATEFUL_PYTHON_IMAGE='metahumotonic-web-back:<version>-x86' \
MHB_WIKI_STATEFUL_GATEWAY_IMAGE='metahumotonic-web-back-ts:<version>-x86' \
MHB_WIKI_STATEFUL_DATABASE='metahumotonic_wiki_canary_<commit12>_<nonce12>' \
MHB_WIKI_STATEFUL_BACKUP_RECEIPT='/var/lib/metahumotonic-wiki/releases/<backup-commit>-<backup-nonce>/current-backup-receipt.json' \
  ops/run-ts-wiki-stateful-canary-vm100.sh dry-run
```

Before `run`, the controller validates the original production backup receipt
and its root-only encrypted artifacts. It creates no public route and performs
no mutation of `metahumotonic_wiki`. If the release directory is absent, it
creates an empty root-owned `0700` directory; if present, it validates that
exact ownership and mode. Controller helper staging uses unique user-owned
`mktemp -d` directories and removes only its known regular helper files before
`rmdir`; it never recursively removes a privileged shared path.
