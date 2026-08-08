#!/usr/bin/env bash
# One-time, transaction-bound wiki storage/env bootstrap with compensation.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_HOST="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
RUNTIME_HOST="${MHB_RUNTIME_HOST:-metahumotonic27@192.168.0.24}"
RUNTIME_ENV_FILE="${MHB_RUNTIME_ENV_FILE:-/etc/metahumotonic/web-back.env}"
PROVISION_RECEIPT="${MHB_WIKI_PROVISION_RECEIPT:-/etc/metahumotonic/wiki-provision-receipt.json}"
transaction_id="$(openssl rand -hex 16)"
lock_helper="/var/tmp/mhb-wiki-operation-lock-${transaction_id}.sh"
data_lock=false
runtime_lock=false

release_operation_locks() {
  local failed=false
  if [[ "$runtime_lock" == true ]]; then ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$lock_helper" release mhb-wiki-runtime-operation "$transaction_id" || failed=true; fi
  if [[ "$data_lock" == true ]]; then ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$lock_helper" release mhb-wiki-data-operation "$transaction_id" || failed=true; fi
  ssh -o BatchMode=yes "$DATA_HOST" "rm -f -- '$lock_helper'" >/dev/null 2>&1 || true
  ssh -o BatchMode=yes "$RUNTIME_HOST" "rm -f -- '$lock_helper'" >/dev/null 2>&1 || true
  [[ "$failed" == false ]]
}

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "${1:-}" == --create ]] || fail "usage: $0 --create"
[[ "$DATA_HOST" =~ ^[A-Za-z0-9._@-]+$ ]] || fail "unsafe data host"
[[ "$RUNTIME_HOST" =~ ^[A-Za-z0-9._@-]+$ ]] || fail "unsafe runtime host"
[[ "$RUNTIME_ENV_FILE" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "unsafe runtime env path"
[[ "$PROVISION_RECEIPT" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "unsafe receipt path"
for command in ssh scp openssl base64 python3; do
  command -v "$command" >/dev/null || fail "missing command: $command"
done

scp -q "$REPO_ROOT/ops/remote/manage-wiki-operation-lock.sh" "$DATA_HOST:$lock_helper"
scp -q "$REPO_ROOT/ops/remote/manage-wiki-operation-lock.sh" "$RUNTIME_HOST:$lock_helper"
ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$lock_helper" acquire mhb-wiki-data-operation "$transaction_id"
data_lock=true
trap 'release_operation_locks' EXIT
ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$lock_helper" acquire mhb-wiki-runtime-operation "$transaction_id"
runtime_lock=true

database_state="$(ssh -o BatchMode=yes "$DATA_HOST" \
  "sudo -n docker exec postgresql psql -U postgres -Atqc \"SELECT (SELECT count(*) FROM pg_roles WHERE rolname='mhb_wiki')::text || ':' || (SELECT count(*) FROM pg_database WHERE datname='metahumotonic_wiki')::text\"")"
[[ "$database_state" == 0:0 ]] || fail "wiki role/database already exists; recover using its provision receipt before retrying"
ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n sh -s -- "$RUNTIME_ENV_FILE" "$PROVISION_RECEIPT" <<'REMOTE'
set -eu
env_file="$1"; receipt="$2"
test ! -e "$receipt"
test ! -f "$env_file" || { ! grep -q '^MHB_WIKI_DATABASE_URL=' "$env_file" && ! grep -q '^MHB_WIKI_SESSION_SECRET=' "$env_file"; }
REMOTE

wiki_password="$(openssl rand -hex 32)"
wiki_session_secret="$(openssl rand -hex 32)"
wiki_moderation_admin_key="$(openssl rand -hex 32)"
backup_key="$(openssl rand -hex 32)"
db_attempted=false
env_attempted=false
db_helper="/var/tmp/mhb-provision-wiki-database-${transaction_id}.sh"
env_helper="/var/tmp/mhb-install-wiki-runtime-env-${transaction_id}.sh"

cleanup() {
  local status=$?
  trap - EXIT ERR
  set +e
  if [[ "$status" -ne 0 ]]; then
    compensation_ok=true
    if [[ "$env_attempted" == true ]]; then
      printf '%s\n' "$transaction_id" | ssh -o BatchMode=yes "$RUNTIME_HOST" \
        sudo -n bash "$env_helper" rollback web-back-pve-1 "$RUNTIME_ENV_FILE" \
        192.168.0.25 "$transaction_id" "$PROVISION_RECEIPT" || compensation_ok=false
    fi
    if [[ "$db_attempted" == true ]]; then
      printf '%s\n' "$transaction_id" | ssh -o BatchMode=yes "$DATA_HOST" \
        sudo -n bash "$db_helper" rollback postgresql mhb_wiki metahumotonic_wiki \
        || compensation_ok=false
    fi
    if [[ "$compensation_ok" == true ]]; then
      printf 'FAIL provision transaction %s compensated after partial failure\n' "$transaction_id" >&2
    else
      printf 'FAIL provision transaction %s requires receipt-guided operator recovery\n' "$transaction_id" >&2
    fi
  fi
  ssh -o BatchMode=yes "$DATA_HOST" "rm -f -- '$db_helper'" >/dev/null 2>&1 || true
  ssh -o BatchMode=yes "$RUNTIME_HOST" "rm -f -- '$env_helper'" >/dev/null 2>&1 || true
  release_operation_locks || status=1
  unset wiki_password wiki_session_secret wiki_moderation_admin_key backup_key database_receipt database_receipt_base64
  exit "$status"
}
trap cleanup EXIT

scp -q "$REPO_ROOT/ops/remote/provision-wiki-database.sh" "$DATA_HOST:$db_helper"
scp -q "$REPO_ROOT/ops/remote/install-wiki-runtime-env.sh" "$RUNTIME_HOST:$env_helper"

db_attempted=true
database_receipt="$(printf '%s\n%s\n%s\n' "$transaction_id" "$wiki_password" "$backup_key" \
  | ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$db_helper" create \
      postgresql mhb_wiki metahumotonic_wiki)"
printf '%s' "$database_receipt" | python3 -c '
import json, sys
body=json.load(sys.stdin)
assert body["status"] == "VERIFIED"
assert body["restore_drill"] == "PASS"
assert len(body["backup_sha256"]) == 64
assert len(body["key_sha256"]) == 64
' >/dev/null
database_receipt_base64="$(printf '%s' "$database_receipt" | base64 | tr -d '\n')"

env_attempted=true
printf '%s\n%s\n%s\n%s\n' "$wiki_password" "$wiki_session_secret" \
  "$wiki_moderation_admin_key" "$database_receipt_base64" \
  | ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$env_helper" install \
      web-back-pve-1 "$RUNTIME_ENV_FILE" 192.168.0.25 "$transaction_id" "$PROVISION_RECEIPT"

printf 'PASS provision transaction %s installed with encrypted backup and restore-drill receipt\n' "$transaction_id"
