#!/usr/bin/env bash
# Exact-commit controller. Default status is read-only; apply is intentionally explicit.
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; mode="${1:-status}"; host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
command -v openssl >/dev/null || { echo 'FAIL openssl is required' >&2; exit 1; }
commit="${MHB_PLATFORM_COMMIT:-$(git -C "$root" rev-parse HEAD)}"; image="${MHB_PLATFORM_IMAGE:-}"; nonce="$(openssl rand -hex 16)"; remote_dir="/var/tmp/mhb-platform-migrate-$nonce"; helper="$remote_dir/helper"; catalog="$remote_dir/catalog.json"; provision="$remote_dir/provision"
[[ "$mode" =~ ^(status|stage-image|apply)$ && "$host" =~ ^[A-Za-z0-9._@-]+$ && "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo 'FAIL invalid input' >&2; exit 1; }
[[ -z "$(git -C "$root" status --porcelain --untracked-files=all)" ]] || { echo 'FAIL exact-commit operation requires fully clean worktree' >&2; exit 1; }
git -C "$root" cat-file -e "$commit^{commit}" || { echo 'FAIL commit unavailable' >&2; exit 1; }
git -C "$root" merge-base --is-ancestor "$commit" origin/main || { echo 'FAIL commit must be reachable from origin/main' >&2; exit 1; }
[[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || { echo 'FAIL MHB_PLATFORM_IMAGE must be digest pinned' >&2; exit 1; }
local_dir="$(mktemp -d)"; trap 'rm -rf "$local_dir"; ssh -o BatchMode=yes "$host" "rm -rf -- '\''$remote_dir'\''" >/dev/null 2>&1 || true' EXIT
git -C "$root" show "$commit:ops/remote/migrate-platform-catalog.sh" >"$local_dir/helper"
git -C "$root" show "$commit:ops/remote/provision-platform-database.sh" >"$local_dir/provision"
git -C "$root" show "$commit:ts/config/platform-catalog.json" >"$local_dir/catalog.json"
catalog_sha="$(sha256sum "$local_dir/catalog.json" | awk '{print $1}')"
helper_sha="$(sha256sum "$local_dir/helper" | awk '{print $1}')"; provision_sha="$(sha256sum "$local_dir/provision" | awk '{print $1}')"
ssh -o BatchMode=yes "$host" "umask 077; mkdir '$remote_dir'; test ! -L '$remote_dir'; chmod 700 '$remote_dir'"
scp -q -o BatchMode=yes "$local_dir/helper" "$local_dir/provision" "$local_dir/catalog.json" "$host:$remote_dir/"
ssh -o BatchMode=yes "$host" "test ! -L '$helper' -a ! -L '$provision' -a ! -L '$catalog'; test \"\$(stat -c %a '$remote_dir')\" = 700; test \"\$(sha256sum '$helper' | awk '{print \$1}')\" = '$helper_sha'; test \"\$(sha256sum '$provision' | awk '{print \$1}')\" = '$provision_sha'; test \"\$(sha256sum '$catalog' | awk '{print \$1}')\" = '$catalog_sha'; chmod 700 '$helper' '$provision'; sudo -n bash '$helper' '$mode' '$image' '$commit' '$catalog' '$catalog_sha' '$provision'"
