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
| Internal container | `/health` | `200`, process/version only |
| Internal container | `/ready` | `200`, dependency state |
| Public negative assertion | `/health`, `/ready` | `404`; these paths are not exposed |

The frontend owns `/wiki/` and `/wiki/data.json`. A backend API probe cannot
prove that the static wiki was deployed.

## Deployment contract

Backend deployment is currently a guarded manual operation. Do not improvise an
`rsync -> docker inspect env -> recreate` sequence and do not apply anything
from `deploy/legacy/`.

An authorized release must satisfy all of these invariants:

1. Start from a pushed, tested commit and build an x86 image tagged with that
   exact commit; record the immutable image digest.
2. Read secrets only from the root-owned VM100 environment file. Never copy
   plaintext secrets into Git, shell history, logs, or a Docker inspection
   receipt.
3. Replace one container at a time. Require direct `/health` and `/ready` on its
   host port before touching the second replica.
4. Run `ops/check-web-back-live.sh` after both replicas are healthy. A `200` from
   only one endpoint or from the frontend root is insufficient.
5. Retain the previous image digest and environment-file backup until public
   readback succeeds, so rollback is deterministic.

The next deployment hardening step is a commit/digest-bound VM100 release
script with sequential rollout, receipts, and automatic rollback. Until that is
implemented and tested separately, this document intentionally provides no
one-command mutating deployment shortcut.

## Secret rotation

The live backend does not consume Kubernetes Secret `web-back-secrets`.
Rotation must update the root-owned VM100 environment file through an
authorized, non-echoing operator path, then recreate the two containers
sequentially under the deployment contract above. Patching a Kubernetes Secret
or restarting `deployment/web-back` changes no live backend process.

## Known limitation

The two manual EndpointSlices have static `ready: true` conditions. Docker
health and k3s endpoint readiness can therefore diverge. The checker detects
that drift, but it cannot heal it. Prefer a later native k3s Deployment (or an
active-health-check controller) so readiness owns endpoint membership. Two
containers on one VM improve process rollout but do not provide host-level HA.

The current image has no source-commit label or external registry receipt. The
checker proves replica/image-byte consistency on VM100, not source provenance.
That gap is why a future release must add the commit/digest-bound receipt in the
deployment contract above before automated backend rollout is enabled.
