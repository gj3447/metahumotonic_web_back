#!/usr/bin/env bash
# Default is dry-run. It never uses the Wiki DB, role, env file or ingress.
set -Eeuo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; data_host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"; mode="${1:-dry-run}"
[[ "$mode" =~ ^(dry-run|status|apply|grant-runtime|rollback-empty)$ ]] || { echo 'usage: dry-run|status|apply|grant-runtime|rollback-empty' >&2; exit 64; }
[[ "$data_host" =~ ^[A-Za-z0-9._@-]+$ ]] || { echo 'FAIL unsafe data host' >&2; exit 1; }
for command in ssh scp openssl; do command -v "$command" >/dev/null || { echo "FAIL $command is required" >&2; exit 1; }; done
helper="/var/tmp/mhb-platform-provision-${$}.sh"; trap 'ssh -o BatchMode=yes "$data_host" "rm -f -- '\''$helper'\''" >/dev/null 2>&1 || true' EXIT
scp -q -o BatchMode=yes "$repo_root/ops/remote/provision-platform-database.sh" "$data_host:$helper"
if [[ "$mode" == apply ]]; then secret="$(openssl rand -hex 32)"; printf '%s\n' "$secret" | ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' apply"; else ssh -o BatchMode=yes "$data_host" "sudo -n bash '$helper' '$mode'"; fi
