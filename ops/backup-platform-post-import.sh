#!/usr/bin/env bash
# Controller for the root-only dedicated platform post-import backup helper.
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; mode="${1:-status}"
data_host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"; container="${MHB_PLATFORM_POSTGRES_CONTAINER:-postgresql}"
[[ "$mode" =~ ^(status|capture)$ && "$data_host" =~ ^[A-Za-z0-9._@-]+$ && "$container" =~ ^[A-Za-z0-9._-]+$ ]] || { echo 'usage: status|capture' >&2; exit 64; }
for command in ssh scp openssl; do command -v "$command" >/dev/null || { echo "FAIL $command is required" >&2; exit 1; }; done
helper="/var/tmp/mhb-platform-post-import-backup-${$}.sh"
trap 'ssh -o BatchMode=yes "$data_host" "rm -f -- '\''$helper'\''" >/dev/null 2>&1 || true' EXIT
scp -q -o BatchMode=yes "$root/ops/remote/backup-platform-post-import.sh" "$data_host:$helper"
if [[ "$mode" == status ]]; then
  ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' status '$container'"
else
  nonce="$(openssl rand -hex 16)"; backup_key="$(openssl rand -hex 32)"
  printf '%s\n' "$backup_key" | ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' capture '$container' '$nonce'"
fi
