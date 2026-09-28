#!/usr/bin/env bash
# Serialized capture -> off-host mirror -> VM100 isolated restore drill.
set -Eeuo pipefail
mode="${1:-dry-run}"; root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"; vm_host="${MHB_VM100_HOST:-metahumotonic27@192.168.0.24}"
[[ "$mode" =~ ^(dry-run|status|run)$ && "$data_host" =~ ^[A-Za-z0-9._@-]+$ && "$vm_host" =~ ^[A-Za-z0-9._@-]+$ ]] || { echo 'usage: dry-run|status|run' >&2; exit 64; }
if [[ "$mode" == dry-run ]]; then printf '{"schema":"metahumotonic/platform-backup-workflow@1","mode":"dry-run","cadence":"weekly","steps":["capture","mirror","restore-drill"],"databaseWrites":false,"wikiTouched":false,"secretMaterialPrinted":false}\n'; exit 0; fi
if [[ "$mode" == status ]]; then
  receipt="${MHB_PLATFORM_BACKUP_RECEIPT:-}"; [[ "$receipt" =~ ^/var/lib/metahumotonic-platform/post-import-backups/[0-9a-f]{40}-[0-9a-f]{32}/receipt\.json$ ]] || { echo 'FAIL MHB_PLATFORM_BACKUP_RECEIPT is required for status' >&2; exit 1; }
  MHB_PLATFORM_BACKUP_RECEIPT="$receipt" "$root/ops/mirror-platform-post-import-backup.sh" status
  backup_id="${receipt%/receipt.json}"; backup_id="${backup_id##*/}"
  MHB_PLATFORM_OFFHOST_BACKUP_ID="$backup_id" "$root/ops/restore-platform-offhost-mirror.sh" status
  exit 0
fi
for command in ssh scp openssl python3; do command -v "$command" >/dev/null || { echo "FAIL $command is required" >&2; exit 1; }; done
token="$(openssl rand -hex 16)"; remote_dir="/var/tmp/mhb-platform-backup-workflow-$token"; helper="$remote_dir/helper"; stage="stage_helpers"; data_locked=false; vm_locked=false
cleanup_staging() {
  local host="$1"
  ssh -o BatchMode=yes "$host" "test ! -L '$remote_dir' -a ! -L '$helper'; test \"\$(stat -c '%u:%a' '$remote_dir')\" = \"\$(id -u):700\"; test \"\$(stat -c '%u:%a' '$helper')\" = \"\$(id -u):700\"; rm -f -- '$helper'; rmdir -- '$remote_dir'"
}
release() {
  status=$?; trap - EXIT
  local release_failed=false
  if [[ "$vm_locked" == true ]]; then
    stage="lock_release_vm"
    if ! ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' release '$token'" >/dev/null 2>&1; then release_failed=true; fi
  fi
  if [[ "$data_locked" == true ]]; then
    stage="lock_release_data"
    if ! ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' release '$token'" >/dev/null 2>&1; then release_failed=true; fi
  fi
  if [[ "$release_failed" == true ]]; then status=1; fi
  if [[ "$status" != 0 ]]; then ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' failure '$token' '$stage'" >/dev/null 2>&1 || true; fi
  # Retain the exact helper and staging directory if a lock could not be
  # released; an operator needs that helper to inspect/recover the owned lock.
  if [[ "$release_failed" == false ]]; then
    stage="staging_cleanup"
    if ! cleanup_staging "$data_host" || ! cleanup_staging "$vm_host"; then
      status=1
      ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' failure '$token' '$stage'" >/dev/null 2>&1 || true
    fi
  fi
  exit "$status"
}
trap release EXIT
ssh -o BatchMode=yes "$data_host" "umask 077; mkdir '$remote_dir'; test ! -L '$remote_dir'; test \"\$(stat -c '%u:%a' '$remote_dir')\" = \"\$(id -u):700\""
ssh -o BatchMode=yes "$vm_host" "umask 077; mkdir '$remote_dir'; test ! -L '$remote_dir'; test \"\$(stat -c '%u:%a' '$remote_dir')\" = \"\$(id -u):700\""
scp -q -o BatchMode=yes "$root/ops/remote/manage-platform-backup-workflow.sh" "$data_host:$helper"; scp -q -o BatchMode=yes "$root/ops/remote/manage-platform-backup-workflow.sh" "$vm_host:$helper"
ssh -o BatchMode=yes "$data_host" "test ! -L '$helper'; chmod 700 '$helper'; test \"\$(stat -c '%u:%a' '$helper')\" = \"\$(id -u):700\""
ssh -o BatchMode=yes "$vm_host" "test ! -L '$helper'; chmod 700 '$helper'; test \"\$(stat -c '%u:%a' '$helper')\" = \"\$(id -u):700\""
stage="lock_data"
ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' acquire '$token'"; data_locked=true
stage="lock_vm"; ssh -o BatchMode=yes "$vm_host" "sudo -n bash '$helper' acquire '$token'"; vm_locked=true
stage="capture"; capture="$("$root/ops/backup-platform-post-import.sh" capture)"
receipt="$(printf '%s' "$capture" | python3 -c 'import json,sys; b=json.load(sys.stdin); p=b.get("receiptPath",""); assert p.startswith("/var/lib/metahumotonic-platform/post-import-backups/") and p.endswith("/receipt.json"); print(p)')"
stage="mirror"; MHB_PLATFORM_BACKUP_RECEIPT="$receipt" "$root/ops/mirror-platform-post-import-backup.sh" mirror >/dev/null
backup_id="${receipt%/receipt.json}"; backup_id="${backup_id##*/}"
stage="restore_drill"; MHB_PLATFORM_OFFHOST_BACKUP_ID="$backup_id" "$root/ops/restore-platform-offhost-mirror.sh" drill
stage="complete"
