#!/usr/bin/env bash
# Controller for the VM100 root-only off-host platform restore drill.
set -Eeuo pipefail
mode="${1:-status}"; vm_host="${MHB_VM100_HOST:-metahumotonic27@192.168.0.24}"; backup_id="${MHB_PLATFORM_OFFHOST_BACKUP_ID:-}"
[[ "$mode" =~ ^(status|drill)$ && "$vm_host" =~ ^[A-Za-z0-9._@-]+$ && "$backup_id" =~ ^[0-9a-f]{40}-[0-9a-f]{32}$ ]] || { echo 'usage: MHB_PLATFORM_OFFHOST_BACKUP_ID=<commit>-<nonce> restore-platform-offhost-mirror.sh status|drill' >&2; exit 64; }
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; helper="/var/tmp/mhb-platform-offhost-restore-${$}.sh"
for command in ssh scp openssl; do command -v "$command" >/dev/null || { echo "FAIL $command is required" >&2; exit 1; }; done
trap 'ssh -o BatchMode=yes "$vm_host" "rm -f -- '\''$helper'\''" >/dev/null 2>&1 || true' EXIT
scp -q -o BatchMode=yes "$root/ops/remote/restore-platform-offhost-mirror.sh" "$vm_host:$helper"
if [[ "$mode" == status ]]; then ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' status '$backup_id'"; else nonce="$(openssl rand -hex 16)"; ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' drill '$backup_id' '$nonce'"; fi
