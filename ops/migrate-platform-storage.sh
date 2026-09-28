#!/usr/bin/env bash
# Exact-commit controller. Default status is read-only; apply is intentionally explicit.
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; mode="${1:-status}"; host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
commit="${MHB_PLATFORM_COMMIT:-$(git -C "$root" rev-parse HEAD)}"; image="${MHB_PLATFORM_IMAGE:-}"; helper="/var/tmp/mhb-platform-migrate-${$}.sh"; catalog="/var/tmp/mhb-platform-catalog-${$}.json"; provision="/var/tmp/mhb-platform-provision-${$}.sh"
[[ "$mode" =~ ^(status|stage-image|apply)$ && "$host" =~ ^[A-Za-z0-9._@-]+$ && "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo 'FAIL invalid input' >&2; exit 1; }
[[ -z "$(git -C "$root" status --porcelain --untracked-files=all)" ]] || { echo 'FAIL exact-commit operation requires fully clean worktree' >&2; exit 1; }
git -C "$root" cat-file -e "$commit^{commit}" || { echo 'FAIL commit unavailable' >&2; exit 1; }
git -C "$root" merge-base --is-ancestor "$commit" origin/main || { echo 'FAIL commit must be reachable from origin/main' >&2; exit 1; }
[[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || { echo 'FAIL MHB_PLATFORM_IMAGE must be digest pinned' >&2; exit 1; }
local_dir="$(mktemp -d)"; trap 'rm -rf "$local_dir"; ssh -o BatchMode=yes "$host" "rm -f -- '\''$helper'\'' '\''$catalog'\'' '\''$provision'\''" >/dev/null 2>&1 || true' EXIT
git -C "$root" show "$commit:ops/remote/migrate-platform-catalog.sh" >"$local_dir/helper"
git -C "$root" show "$commit:ops/remote/provision-platform-database.sh" >"$local_dir/provision"
git -C "$root" show "$commit:ts/config/platform-catalog.json" >"$local_dir/catalog.json"
catalog_sha="$(sha256sum "$local_dir/catalog.json" | awk '{print $1}')"
scp -q -o BatchMode=yes "$local_dir/helper" "$local_dir/provision" "$local_dir/catalog.json" "$host:/var/tmp/"
ssh -o BatchMode=yes "$host" "mv /var/tmp/helper '$helper'; mv /var/tmp/provision '$provision'; mv /var/tmp/catalog.json '$catalog'; chmod 700 '$helper' '$provision'; sudo -n bash '$helper' '$mode' '$image' '$commit' '$catalog' '$catalog_sha' '$provision'"
