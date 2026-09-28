#!/usr/bin/env bash
# Private platform PostgreSQL read canary. Default is a local, no-network dry-run.
set -Eeuo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; mode="${1:-dry-run}"
image="${MHB_PLATFORM_READ_CANARY_IMAGE:-}"; commit="${MHB_PLATFORM_READ_CANARY_COMMIT:-}"; data_host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
[[ "$mode" =~ ^(dry-run|status|run|cleanup)$ ]] || { echo 'usage: dry-run|status|run|cleanup' >&2; exit 64; }
[[ "$image" =~ ^sha256:[0-9a-f]{64}$ && "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo 'FAIL MHB_PLATFORM_READ_CANARY_IMAGE must be sha256:<64 hex> and COMMIT must be full SHA' >&2; exit 1; }
[[ "$data_host" =~ ^[A-Za-z0-9._@-]+$ ]] || { echo 'FAIL unsafe data host' >&2; exit 1; }
git -C "$repo_root" diff --quiet && git -C "$repo_root" diff --cached --quiet || { echo 'FAIL controller checkout must be clean' >&2; exit 1; }
git -C "$repo_root" cat-file -e "$commit^{commit}" || { echo 'FAIL requested commit is unavailable locally' >&2; exit 1; }
source_digest="$(git -C "$repo_root" archive --format=tar "$commit" | sha256sum | awk '{print $1}')"
if [[ "$mode" == dry-run ]]; then printf '{"schema":"metahumotonic/platform-read-canary@1","mode":"dry-run","image":"%s","commit":"%s","sourceArchiveSha256":"%s","data01Mutation":false,"publicIngressChanged":false,"prerequisites":["image must already be staged on data-01","reader role must be separately provisioned and ACL-reviewed"]}\n' "$image" "$commit" "$source_digest"; exit 0; fi
for c in ssh scp openssl; do command -v "$c" >/dev/null || { echo "FAIL $c is required" >&2; exit 1; }; done
nonce="$(openssl rand -hex 12)"; remote_dir="/var/tmp/mhb-platform-read-canary-$nonce"; helper="$remote_dir/helper.sh"
cleanup() { ssh -o BatchMode=yes "$data_host" "sudo -n rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true; }; trap cleanup EXIT
ssh -o BatchMode=yes "$data_host" "install -d -m 700 '$remote_dir'"
scp -q -o BatchMode=yes "$repo_root/ops/remote/run-ts-platform-read-canary.sh" "$data_host:$helper"
ssh -o BatchMode=yes "$data_host" "test \"\$(stat -c '%U:%G:%a' '$remote_dir')\" = '$(id -un):$(id -gn):700' && test -f '$helper' && test ! -L '$helper'"
ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' '$mode' '$image' '$commit' '$nonce' '$source_digest'"
