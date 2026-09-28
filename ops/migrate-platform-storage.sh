#!/usr/bin/env bash
# Exact-commit controller. Default status is read-only; apply is intentionally explicit.
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; mode="${1:-status}"; host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
commit="${MHB_PLATFORM_COMMIT:-$(git -C "$root" rev-parse HEAD)}"; image="${MHB_PLATFORM_IMAGE:-}"; helper="/var/tmp/mhb-platform-migrate-${$}.sh"; catalog="/var/tmp/mhb-platform-catalog-${$}.json"
[[ "$mode" =~ ^(status|apply)$ && "$host" =~ ^[A-Za-z0-9._@-]+$ && "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo 'FAIL invalid input' >&2; exit 1; }
git -C "$root" diff --quiet || { echo 'FAIL exact-commit operation requires clean worktree' >&2; exit 1; }
git -C "$root" cat-file -e "$commit^{commit}" || { echo 'FAIL commit unavailable' >&2; exit 1; }
[[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || { echo 'FAIL MHB_PLATFORM_IMAGE must be digest pinned' >&2; exit 1; }
trap 'ssh -o BatchMode=yes "$host" "rm -f -- '\''$helper'\'' '\''$catalog'\''" >/dev/null 2>&1 || true' EXIT
scp -q -o BatchMode=yes "$root/ops/remote/migrate-platform-catalog.sh" "$root/ops/remote/provision-platform-database.sh" "$root/ts/config/platform-catalog.json" "$host:/var/tmp/"
ssh -o BatchMode=yes "$host" "mv /var/tmp/migrate-platform-catalog.sh '$helper'; mv /var/tmp/platform-catalog.json '$catalog'; sudo -n bash '$helper' '$mode' '$image' '$commit' '$catalog'"
