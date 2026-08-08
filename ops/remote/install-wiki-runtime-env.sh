#!/usr/bin/env bash
# Root-only, receipt-first runtime env install with crash-safe compensation.
set -Eeuo pipefail
umask 077

mode="${1:-}"; container="${2:-web-back-pve-1}"; env_file="${3:-/etc/metahumotonic/web-back.env}"
db_host="${4:-192.168.0.25}"; transaction_id="${5:-}"; receipt_file="${6:-/etc/metahumotonic/wiki-provision-receipt.json}"
[[ "$mode" == install || "$mode" == rollback ]]
[[ "$container" =~ ^[A-Za-z0-9._-]+$ ]]; [[ "$env_file" =~ ^/[A-Za-z0-9._/-]+$ ]]
[[ "$db_host" =~ ^[0-9.]+$ ]]; [[ "$transaction_id" =~ ^[0-9a-f]{32}$ ]]; [[ "$receipt_file" =~ ^/[A-Za-z0-9._/-]+$ ]]
env_backup="${env_file}.pre-wiki-${transaction_id}"
backup_stage="${env_backup}.stage"
env_stage="${env_file}.wiki-stage-${transaction_id}"

atomic_receipt() {
  local status="$1" env_preexisting="$2" original_sha="$3" backup_sha="$4" installed_sha="$5" database_file="${6:-}"
  python3 - "$receipt_file" "$transaction_id" "$status" "$env_backup" "$backup_stage" "$env_stage" \
    "$env_preexisting" "$original_sha" "$backup_sha" "$installed_sha" "$database_file" <<'PY'
import json, os, pathlib, sys
p=pathlib.Path(sys.argv[1]); t=p.with_name(p.name+f'.tmp.{os.getpid()}')
b={"schema":"metahumotonic/wiki-provision-receipt@1","transaction_id":sys.argv[2],"status":sys.argv[3],
   "env_backup":sys.argv[4],"env_backup_stage":sys.argv[5],"env_stage":sys.argv[6],
   "env_preexisting":sys.argv[7]=="true","original_env_sha256":sys.argv[8],
   "env_backup_sha256":sys.argv[9],"installed_env_sha256":sys.argv[10]}
if sys.argv[11]: b["database_backup"]=json.loads(pathlib.Path(sys.argv[11]).read_text())
t.write_text(json.dumps(b,sort_keys=True)+"\n"); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
}

load_receipt() {
  test "$(stat -c '%U:%G:%a' "$receipt_file")" = root:root:600
  eval "$(python3 - "$receipt_file" "$transaction_id" "$env_backup" "$backup_stage" "$env_stage" <<'PY'
import json,pathlib,shlex,sys
b=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert b["schema"]=="metahumotonic/wiki-provision-receipt@1" and b["transaction_id"]==sys.argv[2]
assert b["env_backup"]==sys.argv[3] and b["env_backup_stage"]==sys.argv[4] and b["env_stage"]==sys.argv[5]
for k in ("status","original_env_sha256","env_backup_sha256","installed_env_sha256"):
 print(f'{k}={shlex.quote(str(b.get(k,"")))}')
print('env_preexisting='+('true' if b.get('env_preexisting') else 'false'))
PY
)"
}

if [[ "$mode" == rollback ]]; then
  if ! test -f "$receipt_file"; then
    test ! -e "$env_backup" && test ! -e "$backup_stage" && test ! -e "$env_stage" \
      || { printf 'FAIL runtime artifact exists without receipt\n' >&2; exit 1; }
    printf 'PASS no runtime env resources found for transaction %s\n' "$transaction_id"; exit 0
  fi
  load_receipt
  [[ "$status" == RESERVED || "$status" == BACKUP_STAGED || "$status" == BACKUP_CREATED || "$status" == ENV_STAGED || "$status" == ENV_INSTALLED || "$status" == INSTALLED || "$status" == FAILED_COMPENSATION_REQUIRES_OPERATOR ]]
  if test -e "$env_backup"; then
    [[ "$env_backup_sha256" =~ ^[0-9a-f]{64}$ ]]
    test "$(sha256sum "$env_backup" | awk '{print $1}')" = "$env_backup_sha256" || { printf 'FAIL env backup digest mismatch\n' >&2; exit 1; }
  fi
  if test -e "$backup_stage"; then
    [[ "$env_backup_sha256" =~ ^[0-9a-f]{64}$ ]]
    test "$(sha256sum "$backup_stage" | awk '{print $1}')" = "$env_backup_sha256" || { printf 'FAIL env backup stage digest mismatch\n' >&2; exit 1; }
  fi
  if [[ "$env_preexisting" == true ]]; then
    [[ "$original_env_sha256" =~ ^[0-9a-f]{64}$ ]]
    test -f "$env_file" || { printf 'FAIL refusing to overwrite absent preexisting runtime env\n' >&2; exit 1; }
    live_sha="$(sha256sum "$env_file" | awk '{print $1}')"
    case "$status" in
      RESERVED|BACKUP_STAGED|BACKUP_CREATED) [[ "$live_sha" == "$original_env_sha256" ]] ;;
      ENV_STAGED|FAILED_COMPENSATION_REQUIRES_OPERATOR) [[ "$live_sha" == "$original_env_sha256" || "$live_sha" == "$installed_env_sha256" ]] ;;
      ENV_INSTALLED|INSTALLED) [[ "$live_sha" == "$installed_env_sha256" ]] ;;
    esac || { printf 'FAIL refusing to overwrite foreign runtime env digest\n' >&2; exit 1; }
    if test -f "$env_backup"; then
      install -m 600 -o root -g root "$env_backup" "$env_stage"
      mv -T "$env_stage" "$env_file"
    else
      test -f "$env_file" && test "$(sha256sum "$env_file" | awk '{print $1}')" = "$original_env_sha256"
    fi
  else
    case "$status" in
      RESERVED|BACKUP_STAGED|BACKUP_CREATED) test ! -e "$env_file" || { printf 'FAIL refusing unexpected runtime env before install\n' >&2; exit 1; } ;;
      ENV_STAGED|FAILED_COMPENSATION_REQUIRES_OPERATOR)
        if test -e "$env_file"; then test "$(sha256sum "$env_file" | awk '{print $1}')" = "$installed_env_sha256" || { printf 'FAIL refusing foreign runtime env\n' >&2; exit 1; }; fi
        ;;
      ENV_INSTALLED|INSTALLED)
        test -f "$env_file" && test "$(sha256sum "$env_file" | awk '{print $1}')" = "$installed_env_sha256" || { printf 'FAIL installed runtime env digest mismatch\n' >&2; exit 1; }
        ;;
    esac
    rm -f -- "$env_file"
  fi
  rm -f -- "$env_backup" "$backup_stage" "$env_stage" "$receipt_file"
  printf 'PASS restored pre-wiki runtime env %s\n' "$transaction_id"; exit 0
fi

IFS= read -r password; IFS= read -r session_secret; IFS= read -r moderation_admin_key; IFS= read -r database_receipt_base64
[[ "$password" =~ ^[0-9a-f]{64}$ ]]; [[ "$session_secret" =~ ^[0-9a-f]{64}$ ]]; [[ "$moderation_admin_key" =~ ^[0-9a-f]{64}$ ]]
[[ "$database_receipt_base64" =~ ^[A-Za-z0-9+/=]+$ ]]
docker container inspect "$container" >/dev/null
test ! -e "$env_backup"; test ! -e "$backup_stage"; test ! -e "$env_stage"; test ! -e "$receipt_file"
install -d -m 700 -o root -g root "$(dirname "$env_file")" "$(dirname "$receipt_file")"
env_preexisting=false; original_sha=ABSENT
if test -f "$env_file"; then env_preexisting=true; original_sha="$(sha256sum "$env_file" | awk '{print $1}')"; fi
atomic_receipt RESERVED "$env_preexisting" "$original_sha" "" ""
cleanup_failed_install() {
  local original=$?; trap - ERR
  if ! bash "$0" rollback "$container" "$env_file" "$db_host" "$transaction_id" "$receipt_file"; then
    load_receipt || true
    atomic_receipt FAILED_COMPENSATION_REQUIRES_OPERATOR "${env_preexisting:-false}" "${original_env_sha256:-$original_sha}" "${env_backup_sha256:-}" "${installed_env_sha256:-}"
    printf 'FAIL runtime env compensation incomplete; evidence retained at %s\n' "$receipt_file" >&2
  fi
  exit "$original"
}
trap cleanup_failed_install ERR

if [[ "$env_preexisting" == true ]]; then
  install -m 600 -o root -g root "$env_file" "$backup_stage"
else
  docker inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' | awk '/^MHB_/' >"$backup_stage"
  chown root:root "$backup_stage"; chmod 600 "$backup_stage"
fi
backup_sha="$(sha256sum "$backup_stage" | awk '{print $1}')"
atomic_receipt BACKUP_STAGED "$env_preexisting" "$original_sha" "$backup_sha" ""
mv -T "$backup_stage" "$env_backup"
atomic_receipt BACKUP_CREATED "$env_preexisting" "$original_sha" "$backup_sha" ""

db_receipt_tmp="$(mktemp "$(dirname "$receipt_file")/.wiki-db-receipt.${transaction_id}.XXXXXX")"
printf '%s' "$database_receipt_base64" | base64 -d >"$db_receipt_tmp"; chmod 600 "$db_receipt_tmp"
python3 - "$db_receipt_tmp" "$transaction_id" <<'PY'
import json,pathlib,sys
b=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert b.get("transaction_id")==sys.argv[2] and b.get("status")=="VERIFIED" and b.get("restore_drill")=="PASS" and len(b.get("backup_sha256",""))==64
PY
awk '/^MHB_/ && $0 !~ /^MHB_WIKI_/' "$env_backup" >"$env_stage"
cat >>"$env_stage" <<ENV
MHB_WIKI_DATABASE_URL=postgresql://mhb_wiki:${password}@${db_host}:5432/metahumotonic_wiki?sslmode=require
MHB_WIKI_SESSION_SECRET=${session_secret}
MHB_WIKI_MODERATION_ADMIN_KEY=${moderation_admin_key}
MHB_WIKI_PUBLIC_WRITES=true
MHB_WIKI_REQUIRE_REDIS=true
MHB_WIKI_SESSION_COOKIE_SECURE=true
ENV
grep -q '^MHB_NEO4J_URI=' "$env_stage"; grep -q '^MHB_MONGO_URI=' "$env_stage"; grep -Eq '^MHB_REDIS_URL=.+$' "$env_stage"
chown root:root "$env_stage"; chmod 600 "$env_stage"
installed_sha="$(sha256sum "$env_stage" | awk '{print $1}')"
atomic_receipt ENV_STAGED "$env_preexisting" "$original_sha" "$backup_sha" "$installed_sha"
mv -T "$env_stage" "$env_file"
atomic_receipt ENV_INSTALLED "$env_preexisting" "$original_sha" "$backup_sha" "$installed_sha"
atomic_receipt INSTALLED "$env_preexisting" "$original_sha" "$backup_sha" "$installed_sha" "$db_receipt_tmp"
rm -f -- "$db_receipt_tmp"
trap - ERR
test "$(stat -c '%U:%G:%a' "$env_file")" = root:root:600; test "$(sha256sum "$env_file" | awk '{print $1}')" = "$installed_sha"
printf 'PASS installed receipt-bound wiki runtime env %s\n' "$transaction_id"
