#!/usr/bin/env bash
# Root-only, crash-visible mutex. A stale lock is never stolen implicitly.
set -Eeuo pipefail

mode="${1:-}"
lock_name="${2:-}"
owner="${3:-}"
[[ "$mode" == acquire || "$mode" == release || "$mode" == status ]]
[[ "$lock_name" =~ ^mhb-wiki-(data|runtime)-operation$ ]]
[[ "$owner" =~ ^[0-9a-f]{32}$ ]]
lock_dir="/run/lock/${lock_name}.lock"
owner_file="$lock_dir/owner"

case "$mode" in
  acquire)
    if ! mkdir -- "$lock_dir"; then
      printf 'FAIL operation lock busy: %s (inspect %s)\n' "$lock_name" "$owner_file" >&2
      exit 1
    fi
    trap 'rm -rf -- "$lock_dir"' ERR
    printf '%s\n' "$owner" >"$owner_file"
    chown root:root "$owner_file" "$lock_dir"
    chmod 600 "$owner_file"
    chmod 700 "$lock_dir"
    trap - ERR
    ;;
  release)
    test "$(stat -c '%U:%G:%a' "$owner_file")" = root:root:600
    test "$(cat "$owner_file")" = "$owner"
    rm -f -- "$owner_file"
    rmdir -- "$lock_dir"
    ;;
  status)
    test -f "$owner_file"
    test "$(cat "$owner_file")" = "$owner"
    ;;
esac
