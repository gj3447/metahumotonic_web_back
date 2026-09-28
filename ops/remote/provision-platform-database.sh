#!/usr/bin/env bash
# Root-only data-01 helper. It owns only the dedicated platform database.
set -Eeuo pipefail
mode="${1:-dry-run}"; container="${2:-postgresql}"
root="${3:-/var/lib/metahumotonic-platform/bootstrap}"; secret_root="${4:-/etc/metahumotonic/platform}"
database="metahumotonic_platform"; owner="mhb_platform_owner"; runtime="mhb_platform_runtime"; receipt="$root/receipt.json"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(dry-run|status|apply|grant-runtime|rollback-empty)$ ]] || fail 'invalid mode'
[[ "$container" =~ ^[A-Za-z0-9._-]+$ && "$root" =~ ^/[A-Za-z0-9._/-]+$ && "$secret_root" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'unsafe path or container'
command -v docker >/dev/null || fail 'docker is required'; command -v openssl >/dev/null || fail 'openssl is required'; docker inspect "$container" >/dev/null 2>&1 || fail 'PostgreSQL container not found'
q() { docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -Atqc "$1"; }
qd() { docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" -Atqc "$1"; }
comment() { q "SELECT COALESCE(shobj_description(oid,'$2'),'') FROM $1 WHERE $3='$4'"; }
state() {
  local a b c; a="$(q "SELECT count(*) FROM pg_roles WHERE rolname='$owner'")"; b="$(q "SELECT count(*) FROM pg_roles WHERE rolname='$runtime'")"; c="$(q "SELECT count(*) FROM pg_database WHERE datname='$database'")"
  if [[ "$a,$b,$c" == 0,0,0 ]]; then printf ABSENT; elif [[ "$a,$b,$c" == 1,1,1 && "$(comment pg_roles pg_authid rolname "$owner")" == metahumotonic-platform-bootstrap@1 && "$(comment pg_roles pg_authid rolname "$runtime")" == metahumotonic-platform-bootstrap@1 && "$(comment pg_database pg_database datname "$database")" == metahumotonic-platform-bootstrap@1 ]]; then printf OWNED; else printf FOREIGN_OR_PARTIAL; fi
}
emit() { printf '{"schema":"metahumotonic/platform-storage@1","mode":"%s","state":"%s","database":"metahumotonic_platform","ownerRole":"mhb_platform_owner","runtimeRole":"mhb_platform_runtime","secretMaterialPrinted":false,"wikiTouched":false,"detail":"%s"}\n' "$1" "$2" "$3"; }
current="$(state)"
case "$mode" in dry-run) emit dry-run "$current" 'would create only absent dedicated roles/database; no SQL mutation performed'; exit 0;; status) emit status "$current" 'read-only ownership check'; exit 0;; esac
[[ "$current" == OWNED ]] || [[ "$mode" == apply && "$current" == ABSENT ]] || fail 'refusing foreign, partial or already-provisioned state'
if [[ "$mode" == apply ]]; then
  read -r runtime_password; [[ "$runtime_password" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid runtime secret'
  install -d -m 700 -o root -g root "$root" "$secret_root"; [[ ! -e "$receipt" && ! -e "$secret_root/runtime-password" ]] || fail 'existing platform receipt or secret refuses overwrite'
  compensate_apply_failure() {
    local schemas="" objects=""
    trap - ERR; set +e
    schemas="$(qd "SELECT count(*) FROM pg_namespace WHERE nspname NOT IN ('pg_catalog','information_schema','public') AND nspname NOT LIKE 'pg_%'" 2>/dev/null)"
    objects="$(qd "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_%' AND c.relkind IN ('r','p','v','m','S','f')" 2>/dev/null)"
    if [[ "$(state)" == OWNED && "${schemas:-nonempty}" == 0 && "${objects:-nonempty}" == 0 ]]; then
      docker exec "$container" dropdb -U postgres "$database" && docker exec "$container" dropuser -U postgres "$runtime" && docker exec "$container" dropuser -U postgres "$owner"
      rm -f "$secret_root/runtime-password" "$secret_root/backup-key" "$root/baseline.dump" "$root/baseline.dump.enc" "$receipt"
    else
      printf '%s\n' '{"schema":"metahumotonic/platform-storage-receipt@1","status":"FAILED_RECOVERY_REQUIRES_OPERATOR","database":"metahumotonic_platform","wikiTouched":false}' >"$receipt"
      chmod 600 "$receipt"
    fi
    exit 1
  }
  trap compensate_apply_failure ERR
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null <<SQL
CREATE ROLE $owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
COMMENT ON ROLE $owner IS 'metahumotonic-platform-bootstrap@1';
CREATE ROLE $runtime LOGIN PASSWORD '$runtime_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
COMMENT ON ROLE $runtime IS 'metahumotonic-platform-bootstrap@1';
SQL
  docker exec "$container" createdb -U postgres -O "$owner" "$database"
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null <<SQL
COMMENT ON DATABASE $database IS 'metahumotonic-platform-bootstrap@1';
REVOKE ALL ON DATABASE $database FROM PUBLIC;
GRANT CONNECT,TEMPORARY ON DATABASE $database TO $runtime;
SQL
  printf '%s\n' "$runtime_password" >"$secret_root/runtime-password"; chmod 600 "$secret_root/runtime-password"
  backup_key="$(openssl rand -hex 32)"; printf '%s\n' "$backup_key" >"$secret_root/backup-key"; chmod 600 "$secret_root/backup-key"
  docker exec "$container" pg_dump -U postgres -Fc "$database" >"$root/baseline.dump"
  printf '%s' "$backup_key" | openssl enc -aes-256-cbc -pbkdf2 -salt -in "$root/baseline.dump" -out "$root/baseline.dump.enc" -pass stdin
  rm -f "$root/baseline.dump"; backup_sha="$(sha256sum "$root/baseline.dump.enc" | awk '{print $1}')"
  printf '{"schema":"metahumotonic/platform-storage-receipt@1","status":"PROVISIONED","database":"metahumotonic_platform","ownerRole":"mhb_platform_owner","runtimeRole":"mhb_platform_runtime","migration":"NOT_APPLIED","baselineBackupSha256":"%s","wikiTouched":false}\n' "$backup_sha" >"$receipt"; chmod 600 "$receipt"; trap - ERR
  emit apply OWNED 'dedicated database and roles created; migration/import and runtime grants remain explicit'; exit 0
fi
if [[ "$mode" == grant-runtime ]]; then
  [[ -f "$receipt" ]] || fail 'missing owned provisioning receipt'
  for table in schema_migrations catalog_versions asset_versions ingest_receipts observations; do qd "SELECT 1 FROM mhb_platform.$table LIMIT 1" >/dev/null; done
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" >/dev/null <<SQL
GRANT USAGE ON SCHEMA mhb_platform TO $runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA mhb_platform TO $runtime;
GRANT INSERT ON mhb_platform.observations,mhb_platform.ingest_receipts TO $runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mhb_platform TO $runtime;
SQL
  emit grant-runtime OWNED 'runtime grants applied after verified migration'; exit 0
fi
[[ -f "$receipt" ]] || fail 'missing owned receipt'
[[ "$(qd "SELECT count(*) FROM information_schema.tables WHERE table_schema='mhb_platform'")" == 0 ]] || fail 'migration or data exists; rollback-empty refuses destructive drop'
docker exec "$container" dropdb -U postgres "$database"; docker exec "$container" dropuser -U postgres "$runtime"; docker exec "$container" dropuser -U postgres "$owner"; rm -f "$secret_root/runtime-password" "$secret_root/backup-key" "$root/baseline.dump.enc" "$receipt"; emit rollback-empty ABSENT 'empty dedicated bootstrap compensated'
