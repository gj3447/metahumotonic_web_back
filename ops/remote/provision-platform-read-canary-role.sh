#!/usr/bin/env bash
# Root-only data-01 helper. It creates a SELECT-only canary login for the already-owned platform DB.
set -Eeuo pipefail
mode="${1:-dry-run}"; container="${2:-postgresql}"
root="${3:-/var/lib/metahumotonic-platform/read-canary-reader}"; secret_root="${4:-/etc/metahumotonic/platform-read-canary}"
database="metahumotonic_platform"; owner="mhb_platform_owner"; reader="mhb_platform_shadow_reader"; marker="metahumotonic-platform-shadow-reader@1"
receipt="$root/receipt.json"; password_file="$secret_root/reader-password"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(dry-run|status|apply|rollback-empty)$ ]] || fail 'invalid mode'
[[ "$container" =~ ^[A-Za-z0-9._-]+$ && "$root" =~ ^/[A-Za-z0-9._/-]+$ && "$secret_root" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'unsafe path or container'
command -v docker >/dev/null || fail 'docker is required'; docker inspect "$container" >/dev/null 2>&1 || fail 'PostgreSQL container not found'
q() { docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -Atqc "$1"; }
qd() { docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" -Atqc "$1"; }
role_comment() { q "SELECT COALESCE(shobj_description(oid,'pg_authid'),'') FROM pg_roles WHERE rolname='$reader'"; }
platform_ready() {
  [[ "$(q "SELECT COALESCE(shobj_description(oid,'pg_database'),'') FROM pg_database WHERE datname='$database'")" == metahumotonic-platform-bootstrap@1 ]] || return 1
  [[ "$(q "SELECT COALESCE(shobj_description(oid,'pg_authid'),'') FROM pg_roles WHERE rolname='$owner'")" == metahumotonic-platform-bootstrap@1 ]] || return 1
  for table in schema_migrations catalog_versions asset_versions ingest_receipts observations; do qd "SELECT to_regclass('mhb_platform.$table') IS NOT NULL" | grep -qx t || return 1; done
}
state() {
  if ! platform_ready; then printf PLATFORM_NOT_READY
  elif [[ "$(q "SELECT count(*) FROM pg_roles WHERE rolname='$reader'")" == 0 ]]; then printf ABSENT
  elif [[ "$(role_comment)" == "$marker" ]]; then printf OWNED
  else printf FOREIGN
  fi
}
emit() { printf '{"schema":"metahumotonic/platform-read-canary-reader@1","mode":"%s","state":"%s","database":"metahumotonic_platform","readerRole":"mhb_platform_shadow_reader","secretMaterialPrinted":false,"wikiTouched":false,"detail":"%s"}\n' "$1" "$2" "$3"; }
verify_privileges() {
  local checks
  checks="$(qd "SELECT has_database_privilege('$reader','$database','CONNECT'),has_database_privilege('$reader','$database','TEMPORARY'),has_schema_privilege('$reader','mhb_platform','USAGE'),has_schema_privilege('$reader','mhb_platform','CREATE'),bool_and(has_table_privilege('$reader',format('%I.%I',schemaname,tablename),'SELECT')),bool_or(has_table_privilege('$reader',format('%I.%I',schemaname,tablename),'INSERT') OR has_table_privilege('$reader',format('%I.%I',schemaname,tablename),'UPDATE') OR has_table_privilege('$reader',format('%I.%I',schemaname,tablename),'DELETE') OR has_table_privilege('$reader',format('%I.%I',schemaname,tablename),'TRUNCATE')) FROM pg_tables WHERE schemaname='mhb_platform'")"
  [[ "$checks" == 't|f|t|f|t|f' ]] || fail 'reader privilege-denial verification failed'
}
current="$(state)"
case "$mode" in
  dry-run) emit dry-run "$current" 'would create a root-secret SELECT-only reader only after platform ownership and schema checks'; exit 0;;
  status) emit status "$current" 'read-only ownership and platform schema check'; exit 0;;
esac
if [[ "$mode" == apply ]]; then
  [[ "$current" == ABSENT ]] || fail 'refusing non-absent reader state'
  read -r password; [[ "$password" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid reader secret'
  install -d -m 700 -o root -g root "$root" "$secret_root"
  [[ ! -e "$receipt" && ! -e "$password_file" ]] || fail 'existing reader receipt or secret refuses overwrite'
  cleanup_failed_apply() {
    trap - ERR; set +e
    if [[ "$(role_comment 2>/dev/null || true)" == "$marker" ]] && [[ "$(q "SELECT count(*) FROM pg_shdepend d JOIN pg_roles r ON d.refclassid='pg_authid'::regclass AND d.refobjid=r.oid WHERE r.rolname='$reader' AND d.deptype='o'" 2>/dev/null || true)" == 0 ]]; then
      docker exec "$container" dropuser -U postgres "$reader"; rm -f "$password_file"
    fi
    printf '%s\n' '{"schema":"metahumotonic/platform-read-canary-reader@1","status":"FAILED_RECOVERY_REQUIRES_OPERATOR","secretMaterialPrinted":false}' >"$receipt"; chmod 600 "$receipt"
    exit 1
  }
  trap cleanup_failed_apply ERR
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null <<SQL
CREATE ROLE $reader LOGIN PASSWORD '$password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
COMMENT ON ROLE $reader IS '$marker';
REVOKE ALL ON DATABASE $database FROM $reader;
GRANT CONNECT ON DATABASE $database TO $reader;
SQL
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" >/dev/null <<SQL
REVOKE ALL ON SCHEMA mhb_platform FROM $reader;
GRANT USAGE ON SCHEMA mhb_platform TO $reader;
GRANT SELECT ON ALL TABLES IN SCHEMA mhb_platform TO $reader;
SQL
  verify_privileges
  printf '%s\n' "$password" >"$password_file"; chmod 600 "$password_file"
  printf '%s\n' '{"schema":"metahumotonic/platform-read-canary-reader@1","status":"PROVISIONED","database":"metahumotonic_platform","readerRole":"mhb_platform_shadow_reader","privilegeDenialVerified":true,"secretMaterialPrinted":false,"wikiTouched":false}' >"$receipt"; chmod 600 "$receipt"; trap - ERR
  emit apply OWNED 'reader created with SELECT-only grants and denial verification'; exit 0
fi
[[ "$current" == OWNED && -f "$receipt" ]] || fail 'missing owned reader receipt'
[[ "$(q "SELECT count(*) FROM pg_shdepend d JOIN pg_roles r ON d.refclassid='pg_authid'::regclass AND d.refobjid=r.oid WHERE r.rolname='$reader' AND d.deptype='o'")" == 0 ]] || fail 'reader owns objects; rollback refuses destructive action'
docker exec "$container" dropuser -U postgres "$reader"; rm -f "$password_file" "$receipt"; emit rollback-empty ABSENT 'owned reader role and root-only secret removed'
