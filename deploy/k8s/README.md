# Active k3s boundary

This directory intentionally contains no applyable backend Deployment manifest.

The live service is hosted by Docker on Proxmox VM100 and routed through a
selectorless `infra/web-back` Service plus two manually managed EndpointSlices.
The former `dgx-worker` Deployment and Secret template were moved to
`../legacy/` and are historical evidence only.

Use the canonical runbook and read-only topology checker:

```sh
ops/check-web-back-live.sh
```

- Operations: [`../../docs/OPERATIONS_VM100.md`](../../docs/OPERATIONS_VM100.md)
- Retired manifest: [`../legacy/web-back-dgx-retired.yaml`](../legacy/web-back-dgx-retired.yaml)

Do not add an applyable Deployment here until the Docker-to-k3s migration has a
commit/digest-bound image source, secret handoff, readiness behavior, sequential
rollout, rollback, and a verified VM100 context guard.
