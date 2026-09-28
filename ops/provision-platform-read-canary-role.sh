#!/usr/bin/env bash
# Default dry-run; this controller only reaches data-01 and never uses Wiki credentials.
set -Eeuo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; data_host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"; mode="${1:-dry-run}"
[[ "$mode" =~ ^(dry-run|status|apply|rollback-empty)$ ]] || { echo 'usage: dry-run|status|apply|rollback-empty' >&2; exit 64; }
[[ "$data_host" =~ ^[A-Za-z0-9._@-]+$ ]] || { echo 'FAIL unsafe data host' >&2; exit 1; }
for c in ssh scp openssl; do command -v "$c" >/dev/null || { echo "FAIL $c is required" >&2; exit 1; }; done
nonce="$(openssl rand -hex 12)"; remote_dir="/var/tmp/mhb-platform-reader-$nonce"; helper="$remote_dir/helper.sh"
cleanup() { ssh -o BatchMode=yes "$data_host" "sudo -n rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true; }; trap cleanup EXIT
ssh -o BatchMode=yes "$data_host" "install -d -m 700 '$remote_dir'"
scp -q -o BatchMode=yes "$repo_root/ops/remote/provision-platform-read-canary-role.sh" "$data_host:$helper"
ssh -o BatchMode=yes "$data_host" "test \"\$(stat -c %u '$remote_dir')\" = \"\$(id -u)\" && test \"\$(stat -c %a '$remote_dir')\" = 700 && test ! -L '$remote_dir' && test -f '$helper'"
if [[ "$mode" == apply ]]; then
  password="$(openssl rand -hex 32)"
  printf '%s\n' "$password" | ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' apply"
else
  ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' '$mode'"
fi
