# VM100 backend operations

This is the canonical operations path for `metahumotonic-web-back` as of
2026-08-08. The retired DGX Deployment and OMD workflow are not valid control
paths.

## Live topology

```text
public request
  -> Cloudflare/cloudflared
  -> Traefik in k3s on VM100 (cpu-edge-01, 192.168.0.24)
  -> selectorless Service infra/web-back
  -> manual EndpointSlices
       -> 192.168.0.24:18210 -> Docker web-back-pve-1
       -> 192.168.0.24:18211 -> Docker web-back-pve-2
```

VM100 is both the edge host and the k3s control plane. It is a production
runtime, not a general development machine. Edit and run zero-infrastructure
tests in the canonical checkout or CI. Use a separate, resource-capped VM/LXC
for integration tests that require real services.

## One supported control path

Run the read-only checker from the repository root:

```sh
ops/check-web-back-live.sh
```

It connects through `ssh metahumo` and executes `kubectl` inside guest 100 with
QEMU guest-agent execution. It verifies the context, node, Service,
EndpointSlices, both Docker containers, direct health/readiness, public API, and
the intentionally private health boundary. It also requires both replicas, the
local image tag, and the running containers to resolve to the same immutable
Docker image ID.

Do not run cluster commands from DGX. DGX has no active kubeconfig and an
unconfigured `kubectl` falls back to `http://localhost:8080`. Historical DGX
kubeconfigs must not be restored blindly.

For QEMU guest-agent JSON, `exitcode` is the command result. `exited: 1` means
the command has finished; it is not an error code.

## Endpoint boundary

| Surface | Path | Expected result |
|---|---|---|
| Public | `/api/research/summary` | `200 application/json`, `source=live` |
| Public | `/api/stats`, `/api/domains`, `/api/skills`, `/api/research/*`, `/api/feedback`, `/api/kg/*`, `/api/mcp/*` | Routed by explicit prefix |
| Planned public wiki | `/api/wiki/v1/*` | Not live until explicit route activation and end-to-end readback |
| Internal container | `/health` | `200`, process/version only |
| Internal container | `/ready` | `200`, dependency state |
| Direct/LAN operator plane | `/internal/wiki/moderation/*` | Root-key authenticated; never routed by Traefik |
| Public negative assertion | `/health`, `/ready`, `/internal/wiki/moderation/reports` | `404`; these paths are not exposed |

The frontend owns `/wiki/`, `/wiki/data.json`, and the community UI at
`/wiki/community/`. A backend API probe cannot prove that any frontend page was
deployed. Conversely, the existing static `/wiki/` cannot prove that the
community API or durable PostgreSQL store is live. See [`WIKI.md`](WIKI.md).

## Deployment contract

Backend deployment is a guarded, commit-bound operation. Do not improvise an
`rsync -> docker inspect env -> recreate` sequence and do not apply anything
from `deploy/legacy/`.

An authorized release must satisfy all of these invariants:

1. Start from a clean, pushed, tested `main` commit and build an x86 image
   tagged with that exact commit; record the immutable image digest.
2. Read secrets only from the root-owned VM100 environment file. Never copy
   plaintext secrets into Git, shell history, logs, or a Docker inspection
   receipt.
3. Replace one container at a time. In one bounded loop, require direct
   `/health`, direct `/ready`, Docker `State.Running=true`, and Docker
   `State.Health.Status=healthy` on its host port before touching the second
   replica. Strictly revalidate both candidate identities, topology, and health
   immediately before publishing `AWAITING_PUBLIC_READBACK`.
4. Before touching either production replica, run the exact built image against
   a commit-named disposable PostgreSQL database and isolated Redis/container
   network. The synthetic gate must pass browser cookie+CSRF, bearer,
   idempotency replay/conflict, create/edit/stale CAS, history/diff/recent,
   submit/report, internal exact-head quarantine/public exclusion/release/report
   resolution, CLI, and official MCP initialize/list/live-call checks. Its
   application containers, Redis, network, and database must be deleted before
   the PASS receipt is accepted; production wiki data is never mutated.
5. Keep `/api/wiki` at its initial route state while both private replicas are
   replaced and directly checked; enable it only for the public verification phase.
6. Run `ops/check-web-back-live.sh` after route activation. A `200` from only
   one endpoint or from the frontend root is insufficient.
7. Retain both previous containers until a durable
   `DONE_ROLLBACK_RETAINED` receipt is committed. Final deletion is retryable;
   `DONE` is written only after both old containers are absent and both current
   replicas are running. Keep the transaction-bound pre-wiki env backup for
   receipt-guided recovery.

For an authorized, already-tested and pushed release, pass the exact commit:

```sh
ops/release-web-back-vm100.sh "$(git rev-parse HEAD)"
```

The script fails unless the checkout is clean `main` and local `HEAD` equals
`origin/main`. On VM100 it verifies the source archive checksum, builds an x86
image with source labels, passes the disposable synthetic gate, replaces
replicas sequentially, activates the exact wiki routes, and runs the public
checker. If direct readiness, route activation, or public readback fails, it
must restore both prior containers and the initial route state. A failed stop,
remove, rename, start, or route restoration is reported as operator-required;
it is never converted to PASS. A successful run records a commit-bound
deployment receipt.

The wiki has an additional one-time storage prerequisite:

```sh
ops/provision-wiki-storage.sh --create
```

Run these Mac-side commands only when `ssh -o BatchMode=yes` and passwordless
`sudo -n` work for `metahumotonic27@192.168.0.24` and `.25`. VM100 must provide
Docker, curl, `ss`, Python 3 and access to the configured Redis canary image;
data-01 must provide the running `postgresql` container, `psql`, `pg_dump`,
`pg_restore`, OpenSSL and SHA-256 tools. VM100 must reach PostgreSQL over TLS and
the configured shared Redis. Provisioning additionally requires an absent
`mhb_wiki` role, absent `metahumotonic_wiki` database, no existing provision
receipt, and no wiki DSN/session secret in the runtime env. The first public
release should begin with both wiki ingress routes disabled.

This creates the isolated `mhb_wiki` PostgreSQL role and
`metahumotonic_wiki` database on data-01, captures an encrypted bootstrap dump,
restores it into a disposable database, verifies the restore, and writes a
root-only receipt. It then backs up and installs the VM100 runtime env without
echoing generated secrets. A failed second phase restores the exact prior env
and compensates the matching database transaction. It refuses an existing
role/database, receipt, or wiki DSN rather than rotating credentials.
Run it only once, before the first wiki release. The runtime must also have a
shared `MHB_WIKI_SESSION_SECRET`, a distinct root-only
`MHB_WIKI_MODERATION_ADMIN_KEY` of at least 32 bytes, durable PostgreSQL,
shared Redis, secure cookies, and explicit public-write opt-in as listed in
[`WIKI.md`](WIKI.md).

Neither command is evidence that the wiki is live by itself. The IngressRoute
must explicitly include `/api/wiki`, and the deployed commit must pass direct,
public, browser, CLI and MCP readback. As of 2026-08-08 this production wiki
sequence has not completed.

The release script performs route activation in its public phase. For recovery
or inspection, the route tool supports explicit modes:

```sh
ops/enable-wiki-route-vm100.sh --status
MHB_WIKI_ROUTE_NONCE="$(openssl rand -hex 16)" ops/enable-wiki-route-vm100.sh --enable
MHB_WIKI_ROUTE_NONCE="$(openssl rand -hex 16)" ops/enable-wiki-route-vm100.sh --disable
```

Record the generated 32-hex nonce before mutation. Reuse it only to resume the
same interrupted target action. A release binds a distinct deterministic
`ROUTE_ROLLBACK_NONCE` for convergence to its recorded prior state; never
invent a new nonce to continue a mixed HTTP/TLS transaction.

The script preflights `spec.routes[0]` on both `web-back-api` and
`web-back-api-tls`, requires their `web-back:8000` target, and uses a JSON Patch
`test` operation so a concurrent route change fails closed. Re-running it is a
no-op after exact readback. If the second patch fails, it attempts to restore
the first route's exact original match string. It does not deploy application
containers or broaden the route to all of `/api`.
Before the first patch it publishes a root-only route transaction receipt with
both resourceVersions, prior/target match strings and SHA-256 digests. Each
patched resource advances the receipt phase. A SIGKILL after the first patch is
resumed only when both live matches are one of that receipt's exact prior/target
values; foreign drift is refused.

### Interrupted-release recovery

`active-rollout.env` is root-owned mode `0600`. A new release fails closed if
it finds `PREPARING`, `AWAITING_PUBLIC_READBACK`, `FINALIZING`,
`DONE_ROLLBACK_RETAINED`, or `ROLLBACK_FAILED_REQUIRES_OPERATOR`; it never
blindly overwrites incomplete state. Inspect the retained receipt/state and use
one explicit command:

```sh
# Resume the exact active nonce. AWAITING_PUBLIC_READBACK runs route activation
# and candidate readback; FINALIZING/DONE_ROLLBACK_RETAINED resumes cleanup;
# DONE only repeats the final checker.
ops/release-web-back-vm100.sh --resume-public EXACT_40_HEX_COMMIT

# Restore both retained prior containers and the route state recorded before
# the interrupted release. The commit argument is optional but recommended.
ops/release-web-back-vm100.sh --recover-rollback EXACT_40_HEX_COMMIT
```

Both modes validate the state file, commit, rollout nonce, canary receipt SHA,
prior/candidate container identities, and original route state. Recovery exits nonzero if either container rollback
or route restoration is incomplete. Do not delete the state, receipt, or
rollback containers to force a retry. Once `DONE_ROLLBACK_RETAINED` has been
committed, the rollback boundary is closed and only `--resume-public` is valid;
this prevents a partially deleted backup set from being misreported as a full
rollback.

Deployment receipts are nonce-specific at
`releases/<commit>/deployment-receipt-<nonce>.env`. Only a fully finalized
receipt is selected by the atomically replaced `deployment-current.env`
pointer. Final checking also requires `active-rollout.env` to be DONE for that
exact commit and nonce, so an older same-commit receipt cannot satisfy a newer
interrupted rollout.
At the FINALIZING boundary, an older same-commit current pointer is atomically
preserved as `deployment-current.previous-<new-nonce>.env` before the new
receipt can become DONE. Therefore the interruption snapshots immediately after
receipt DONE, after pointer replacement, and before state DONE are all
resumable: the pointer is respectively absent or exact, never silently old.

## Wiki rollback and data recovery

Every provision and release first acquires crash-visible root-only mutexes on
data-01 and VM100. A busy or stale lock is a hard stop; inspect the owner file
under `/run/lock/mhb-wiki-*-operation.lock/owner` and release it only with the
recorded 32-hex owner through `manage-wiki-operation-lock.sh`. Never delete the
directory by hand while an operator may still be active.

Each release fetches `origin/main`, requires normal releases to equal the
fetched remote commit, and labels both the OCI image and running replicas with
the exact revision. `--resume-public` is different: it validates the recorded
active-rollout commit and its receipts, so it remains resumable if origin has
advanced. The checker must be called with `--expected-commit EXACT_40_HEX`.

Before every release (including later releases), data-01 captures the current
production database into a commit-and-nonce-specific AES-256 encrypted backup,
restores it into a unique disposable database, and runs the candidate image's
migrations and full canary there. The verified receipt and digest are copied
into rollout state and the durable deployment receipt. Application rollback
restores the prior containers and route only; it does not reverse schema or
wiki history. The encrypted snapshot is retained for explicit disaster
recovery after an operator verifies its receipt. Redis canaries use only the
digest-pinned reference in `ops/redis-canary-image.txt`; tag-only overrides are
rejected.

The ordinary release path is deliberately not a migration mechanism. It hashes
the exact `migrations/wiki/*.sql` tree, binds that hash into the image, rollout
state, schema-gate receipt and deployment receipt, and requires it to equal the
currently deployed image's hash. Any change fails before production mutation
and requires a separately reviewed maintenance migration flow. The only
bootstrap exception is the pre-wiki 0.9.1 image: it has no wiki revision label,
must still be running, and the exact `/api/wiki` route must be disabled. Its
one-shot transactional schema application occurs only after the complete
rollback state is atomically published and before either replica is replaced.
Retry is allowed only for the empty schema or the exact same candidate checksums.

Rollout state is written as a root-owned mode-600 temporary file and atomically
renamed only after every receipt digest is known. Every later status transition
uses the same temp-and-rename protocol; partial lines are never appended. The
current-backup receipt becomes immutable `VERIFIED` immediately after the clone
restore succeeds. Migration evidence lives separately in the commit-bound
canary and schema-gate receipts.

Canary ports are Docker-assigned loopback ports. Canary databases have a
commit-and-nonce `RESERVED` receipt plus an exact PostgreSQL COMMENT and owner;
only the data-01 helper may drop them. Both the normal success path and EXIT
path call the same cleanup coordinator. It makes at most five exact attempts
per release process with
1, 2, 4, and 8 second waits, covering the observed application disconnect
grace while keeping stdout/stderr diagnostics. Once exhausted, the EXIT trap
returns nonzero immediately without starting a second retry cycle. A SIGKILL or five failed cleanup
attempts may leave both the operation lock and receipt intentionally in place.
Every new release validates the root-only receipt directory and fails closed if
any exact receipt is not `DROPPED`; it never broadly deletes leftovers. Recover
one listed transaction through the same host locks with:

```bash
ops/release-web-back-vm100.sh --recover-canary-db COMMIT40 NONCE32
```

This recovery first applies the same strict validator used by the pending scan:
the root directory and receipt must be non-symlink directories/files with exact
root ownership and modes 700/600, and schema, filename, commit, nonce, derived
database name, and status must agree. It then validates the database owner and
COMMENT, drops only that database, and atomically records `DROPPED`. Corrupt,
renamed, permission-drifted, non-regular, or symlink receipts block before any
Docker/PostgreSQL mutation.

For example, after confirming that the recorded lock owner is no longer
running, copy the exact helper to data-01 and use receipt-bound arguments (the
nonempty dump/key sentinels preserve positional arguments in `status`/`drop`
mode):

```bash
sudo bash /var/tmp/mhb-manage-wiki-canary-db-RECORDED.sh status postgresql \
  metahumotonic_wiki_canary_COMMIT12_NONCE12 mhb_wiki \
  UNUSED_ENCRYPTED_DUMP UNUSED_KEY_FILE COMMIT40 NONCE32
sudo bash /var/tmp/mhb-manage-wiki-canary-db-RECORDED.sh drop postgresql \
  metahumotonic_wiki_canary_COMMIT12_NONCE12 mhb_wiki \
  UNUSED_ENCRYPTED_DUMP UNUSED_KEY_FILE COMMIT40 NONCE32
```

The nonempty `UNUSED_*` placeholders are positional sentinels. SSH flattens a
remote command into a shell command string, so empty arguments can disappear
and shift commit/nonce into the dump/key slots. Status and drop ignore the
sentinel values but require their positions to remain present.

The helper refuses cleanup unless the exact receipt, PostgreSQL database owner,
and transaction COMMENT agree. Preserve the resulting `DROPPED` receipt as the
cleanup evidence before releasing the stale operation lock with its exact owner.

Application canary receipts are also rollout-nonce-specific:
`releases/<commit>/wiki-release-canary-<nonce>.json`. A same-commit redeploy
must create a new file and may never replace the receipt referenced by an older
DONE deployment. Before either old replica is stopped, the release inspects
both replicas together and requires identical image IDs/references, revision and
migration labels, running/healthy state, restart policy and exact port bindings.
The canonical container topology is exactly one `8000/tcp` binding per replica:
`0.0.0.0:18210` for `web-back-pve-1` and `0.0.0.0:18211` for
`web-back-pve-2`; extra IPv4/IPv6 bindings fail closed.
The one-time unlabeled 0.9.1 bootstrap may report Docker's equivalent empty
`HostIp` value; the release normalizes that value to `DOCKER_DEFAULT_ALL` and
binds it exactly in rollback state. Every labeled wiki release requires the
explicit canonical `0.0.0.0` value.
For labeled deployments those values must also match the prior DONE receipt.
The active state binds that complete prior identity, and both predicted backup
names must be absent before deployment. Rollback permits the normal stop,
rename, and pre-health crash boundaries, but validates every backup as the
exact prior container and every surviving replacement as the exact candidate
before deletion or rename. Finalization validates both backups and both healthy
candidate replicas before deleting the first backup; stale or foreign names
fail closed.

Both candidate and final live checks validate the same schema gate and data-01
artifacts. Candidate mode reads the complete `active-rollout.env`, requires
`AWAITING_PUBLIC_READBACK`, then verifies the actual data receipt, encrypted
dump and separate key on data-01 before finalization can delete rollback
containers. Both file SHA-256 values are receipt-bound and recomputed without
printing key plaintext. Final mode repeats those checks from the durable DONE
receipt.

Runtime Docker canaries have their own durable root-only receipt under
`/var/lib/metahumotonic-web-back/runtime-canaries/<commit>-<nonce>.json`.
The receipt is atomically RESERVED before network/container/workdir creation;
each Docker object carries exact commit and nonce labels. Normal completion uses
`manage-wiki-canary-runtime.sh cleanup COMMIT40 NONCE32` and retains CLEANED
evidence. After SIGKILL, first copy the canonical helper back to VM100, run
`status` with the exact receipt commit/nonce, and then run `cleanup` with those
same arguments. It refuses foreign or mismatched labels and verifies removal of
both containers, the network and the root-only secret workdir. Never glob or
sweep other Docker resources.

The secret workdir stage is created exclusively under umask `077` with mode
`0700`, then bound into both receipt and marker by device/inode identity before
atomic rename. Existing stage/final paths or mkdir failure publish a durable
`REFUSED_*` receipt. Cleanup never removes an unmarked or identity-mismatched
stage; an interruption before ownership binding is preserved for inspection.

The release script's automatic rollback restores the two previous application
containers and the initial route state. It does not reverse PostgreSQL events
or restore a database. Wiki
events, receipts, projections and outbox rows are durable state and must remain
forward-compatible across application rollback.

Provisioning now enforces an encrypted PostgreSQL bootstrap backup, records the
encrypted dump SHA-256, key-file SHA-256, and locations in the root-only
receipt, and completes a restore drill into a disposable database. Preflight
recomputes both live digests; only digests are emitted and the key plaintext is
never logged.
The release preflight revalidates the receipt, encrypted dump checksum, and
transaction-bound env backup before touching either replica or enabling the
route. This is a first-deploy gate, not a complete recurring retention policy;
after real wiki data exists, periodic backups and representative restoration of
pages, immutable history, receipts, and outbox rows still require operations.
Provisioning artifacts and runtime secrets are `root:root` mode `0600`; their
dedicated directories are `root:root` mode `0700`. If exact DB drops fail during
compensation, the transaction directory, key, and a
`FAILED_COMPENSATION_REQUIRES_OPERATOR` receipt are retained. The command exits
nonzero and must not be rerun until that exact receipt has been recovered.

Runtime env installation publishes a nonce-bound `RESERVED` receipt before the
named backup, then advances atomically through `BACKUP_STAGED`,
`BACKUP_CREATED`, `ENV_STAGED`, `ENV_INSTALLED`, and `INSTALLED`. Original,
backup, and installed SHA-256 digests are bound in every applicable phase.
Rollback accepts only the status-specific pre/post-move digest set; it never
overwrites an absent or foreign live env, and retains the receipt on refusal.

If automatic provisioning compensation reports operator recovery, use the
32-hex `transaction_id` from the retained receipt. Restore the env first, then
drop only the receipt-bound DB resources; both helper rollback modes are
idempotent and remain nonzero while exact cleanup is incomplete:

```sh
tx=EXACT_32_HEX_TRANSACTION_ID
scp ops/remote/install-wiki-runtime-env.sh \
  metahumotonic27@192.168.0.24:/var/tmp/mhb-wiki-env-recovery.sh
scp ops/remote/provision-wiki-database.sh \
  metahumotonic27@192.168.0.25:/var/tmp/mhb-wiki-db-recovery.sh
printf '%s\n' "$tx" | ssh -o BatchMode=yes metahumotonic27@192.168.0.24 \
  sudo -n bash /var/tmp/mhb-wiki-env-recovery.sh rollback web-back-pve-1 \
  /etc/metahumotonic/web-back.env 192.168.0.25 "$tx" \
  /etc/metahumotonic/wiki-provision-receipt.json
printf '%s\n' "$tx" | ssh -o BatchMode=yes metahumotonic27@192.168.0.25 \
  sudo -n bash /var/tmp/mhb-wiki-db-recovery.sh rollback postgresql \
  mhb_wiki metahumotonic_wiki
```

Remove the two `/var/tmp/*-recovery.sh` copies only after both commands report
PASS and readback confirms the receipt-bound artifacts are absent. Never hand
delete the retained encrypted dump/key before successful DB compensation.

## Secret rotation

The live backend does not consume Kubernetes Secret `web-back-secrets`.
Rotation must update the root-owned VM100 environment file through an
authorized, non-echoing operator path, then recreate the two containers
sequentially under the deployment contract above. Patching a Kubernetes Secret
or restarting `deployment/web-back` changes no live backend process.
The moderation key is not a public API token and must remain distinct from the
session-signing secret. Operators send it only as `X-Wiki-Moderator-Key` over a
direct/LAN-authorized connection to `/internal/wiki/moderation/*`. The public
Traefik allowlist remains exactly `/api/wiki`; the live checker requires public
`/internal/wiki/moderation/reports` to remain `404`.

## Known limitation

The two manual EndpointSlices have static `ready: true` conditions. Docker
health and k3s endpoint readiness can therefore diverge. The checker detects
that drift, but it cannot heal it. Prefer a later native k3s Deployment (or an
active-health-check controller) so readiness owns endpoint membership. Two
containers on one VM improve process rollout but do not provide host-level HA.

The release image and deployment receipt carry source commit/archive labels,
but there is no external registry receipt. The checker proves replica/image-byte
consistency on VM100; it does not establish an external supply-chain attestation.
