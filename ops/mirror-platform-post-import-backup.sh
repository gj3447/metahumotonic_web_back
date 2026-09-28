#!/usr/bin/env bash
# Copies only a verified encrypted platform backup from data-01 to VM100.
set -Eeuo pipefail
mode="${1:-status}"; data_host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"; vm_host="${MHB_VM100_HOST:-metahumotonic27@192.168.0.24}"
receipt="${MHB_PLATFORM_BACKUP_RECEIPT:-}"
[[ "$mode" =~ ^(status|mirror)$ && "$data_host" =~ ^[A-Za-z0-9._@-]+$ && "$vm_host" =~ ^[A-Za-z0-9._@-]+$ && "$receipt" =~ ^/var/lib/metahumotonic-platform/post-import-backups/[0-9a-f]{40}-[0-9a-f]{32}/receipt\.json$ ]] || { echo 'usage: MHB_PLATFORM_BACKUP_RECEIPT=/var/lib/.../receipt.json mirror-platform-post-import-backup.sh status|mirror' >&2; exit 64; }
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; helper="/var/tmp/mhb-platform-offhost-mirror-${$}.sh"
for command in ssh scp; do command -v "$command" >/dev/null || { echo "FAIL $command is required" >&2; exit 1; }; done
trap 'ssh -o BatchMode=yes "$data_host" "rm -f -- '\''$helper'\''" >/dev/null 2>&1 || true; ssh -o BatchMode=yes "$vm_host" "rm -f -- '\''$helper'\''" >/dev/null 2>&1 || true' EXIT
scp -q -o BatchMode=yes "$root/ops/remote/mirror-platform-post-import-backup.sh" "$data_host:$helper"
scp -q -o BatchMode=yes "$root/ops/remote/mirror-platform-post-import-backup.sh" "$vm_host:$helper"
read -r backup_id encrypted_path key_path encrypted_sha key_sha receipt_sha < <(ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' source-manifest '$receipt'")
[[ "$backup_id" =~ ^[0-9a-f]{40}-[0-9a-f]{32}$ && "$encrypted_path" =~ ^/ && "$key_path" =~ ^/ && "$encrypted_sha" =~ ^[0-9a-f]{64}$ && "$key_sha" =~ ^[0-9a-f]{64}$ && "$receipt_sha" =~ ^[0-9a-f]{64}$ ]] || { echo 'FAIL invalid source manifest' >&2; exit 1; }
if [[ "$mode" == status ]]; then
  printf '{"schema":"metahumotonic/platform-offhost-mirror@1","mode":"status","backupId":"%s","sourceReceiptSha256":"%s","databaseWrites":false,"wikiTouched":false,"secretMaterialPrinted":false}\n' "$backup_id" "$receipt_sha"; exit 0
fi
ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' destination-prepare '$backup_id' '$encrypted_sha' '$key_sha' '$receipt_sha'"
ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' source-stream '$receipt' encrypted" | ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' destination-receive '$backup_id' encrypted '$encrypted_sha'"
ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' source-stream '$receipt' key" | ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' destination-receive '$backup_id' key '$key_sha'"
ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' source-stream '$receipt' receipt" | ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' destination-receive '$backup_id' receipt '$receipt_sha'"
ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' destination-finalize '$backup_id'"
