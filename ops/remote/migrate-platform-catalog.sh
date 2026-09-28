#!/usr/bin/env bash
# Root-only data-01 helper. Runs a pinned backend image against only the
# dedicated platform DB; it never accepts a DSN or password in argv/stdout.
set -Eeuo pipefail
mode="${1:-status}"; image="${2:-}"; commit="${3:-}"; catalog="${4:-}"; catalog_sha="${5:-}"; provision="${6:-}"
database="metahumotonic_platform"; owner="mhb_platform_owner"; runtime="mhb_platform_runtime"; container="postgresql"
root="/var/lib/metahumotonic-platform/migration"; receipt="$root/receipt.json"; secret_dir="/etc/metahumotonic/platform"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(status|stage-image|apply)$ ]] || fail 'invalid mode'
[[ "$commit" =~ ^[0-9a-f]{40}$ && ( "$image" =~ @sha256:[0-9a-f]{64}$ || "$image" =~ ^sha256:[0-9a-f]{64}$ ) && "$catalog" =~ ^/[A-Za-z0-9._/-]+$ && "$catalog_sha" =~ ^[0-9a-f]{64}$ && "$provision" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'invalid pinned inputs'
q() { docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -Atqc "$1"; }
qd() { docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" -Atqc "$1"; }
[[ -f "$catalog" && "$(sha256sum "$catalog" | awk '{print $1}')" == "$catalog_sha" ]] || fail 'catalog artifact digest mismatch'; command -v openssl >/dev/null || fail 'openssl required'
if [[ "$mode" == stage-image && "$image" != sha256:* ]]; then docker pull "$image" >/dev/null; fi
image_id="$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null)" || fail 'pinned image unavailable locally; run stage-image first'
[[ "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$commit" ]] || fail 'image revision does not match exact commit'
if [[ "$mode" == stage-image ]]; then
  printf '{"schema":"metahumotonic/platform-migration@1","mode":"stage-image","commit":"%s","image":"%s","databaseWrites":false,"secretMaterialPrinted":false,"wikiTouched":false}\n' "$commit" "$image_id"
  exit 0
fi
docker inspect "$container" >/dev/null 2>&1 || fail 'PostgreSQL container not found'
owned="$(q "SELECT count(*) FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname='$database' AND r.rolname='$owner'")"
[[ "$owned" == 1 ]] || fail 'dedicated platform DB ownership check failed'
for table in schema_migrations catalog_versions asset_versions ingest_receipts observations; do qd "SELECT to_regclass('mhb_platform.$table') IS NOT NULL" | grep -qx t || { [[ "$mode" == apply ]] || fail 'migration has not created required tables'; break; }; done
if [[ "$mode" == status || "$mode" == stage-image ]]; then
  printf '{"schema":"metahumotonic/platform-migration@1","mode":"status","commit":"%s","image":"%s","database":"metahumotonic_platform","secretMaterialPrinted":false,"wikiTouched":false}\n' "$commit" "$image_id"
  exit 0
fi
if [[ -e "$receipt" ]]; then
  receipt_state="$(python3 - "$receipt" "$commit" "$image_id" "$catalog_sha" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as source:
        receipt = json.load(source)
except (OSError, ValueError):
    raise SystemExit(1)
if (receipt.get('schema') == 'metahumotonic/platform-migration@1'
        and receipt.get('status') == 'VERIFIED_BEFORE_RUNTIME_GRANT'
        and receipt.get('commit') == sys.argv[2]
        and receipt.get('image') == sys.argv[3]
        and receipt.get('catalogArtifactSha256') == sys.argv[4]):
    print('RESUME_RUNTIME_GRANT')
PY
  )" || fail 'existing migration receipt is not an exact verified-grant recovery state'
  [[ "$receipt_state" == RESUME_RUNTIME_GRANT ]] || fail 'existing migration receipt refuses overwrite'
  "$provision" grant-runtime >/dev/null || fail 'runtime grant recovery failed; verified receipt retained for retry'
  python3 - "$receipt" <<'PY'
import json, os, pathlib, sys
p = pathlib.Path(sys.argv[1]); receipt = json.loads(p.read_text(encoding='utf-8'))
receipt['status'] = 'PASS'
t = p.with_suffix('.tmp'); t.write_text(json.dumps(receipt, sort_keys=True)+'\n', encoding='utf-8'); os.chmod(t, 0o600); t.replace(p)
PY
  printf '{"schema":"metahumotonic/platform-migration@1","status":"PASS","commit":"%s","image":"%s","secretMaterialPrinted":false,"wikiTouched":false}\n' "$commit" "$image_id"
  exit 0
fi
install -d -m 700 -o root -g root "$root" "$secret_dir"
manager="mhb_platform_migrator_$(openssl rand -hex 6)"; manager_password="$(openssl rand -hex 32)"; env_file="$(mktemp "$secret_dir/.migration-env.XXXXXX")"
chmod 600 "$env_file"; printf 'MHB_PLATFORM_DATABASE_URL=postgresql://%s:%s@127.0.0.1:5432/%s\n' "$manager" "$manager_password" "$database" >"$env_file"
manager_may_exist=false
write_cleanup_failure() {
  printf '%s\n' '{"schema":"metahumotonic/platform-migration@1","status":"FAILED_MIGRATOR_CLEANUP_REQUIRES_OPERATOR","database":"metahumotonic_platform","secretMaterialPrinted":false,"wikiTouched":false}' >"$receipt"
  chmod 600 "$receipt"
}
cleanup_manager() {
  local failed=false role_count
  if [[ "$manager_may_exist" == true ]]; then
    if ! role_count="$(q "SELECT count(*) FROM pg_roles WHERE rolname='$manager'" 2>/dev/null)"; then
      failed=true
    elif [[ "$role_count" == 1 ]]; then
      # Ownership is database-local: do this in the platform database before
      # removing the cluster-wide role membership and login role.
      docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" -c "REASSIGN OWNED BY $manager TO $owner; DROP OWNED BY $manager" >/dev/null 2>&1 || failed=true
      docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -c "REVOKE $owner FROM $manager; DROP ROLE $manager" >/dev/null 2>&1 || failed=true
    elif [[ "$role_count" != 0 ]]; then
      failed=true
    fi
    if ! role_count="$(q "SELECT count(*) FROM pg_roles WHERE rolname='$manager'" 2>/dev/null)" || [[ "$role_count" != 0 ]]; then failed=true; fi
  fi
  rm -f "$env_file"; unset manager_password
  if [[ "$failed" == true ]]; then write_cleanup_failure; return 1; fi
  manager_may_exist=false
  return 0
}
cleanup_on_exit() {
  local status=$?
  trap - EXIT
  if ! cleanup_manager; then exit 1; fi
  exit "$status"
}
trap cleanup_on_exit EXIT
manager_may_exist=true
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null <<SQL
CREATE ROLE $manager LOGIN PASSWORD '$manager_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
GRANT $owner TO $manager;
REVOKE ALL ON DATABASE $database FROM $manager;
GRANT CONNECT,TEMPORARY ON DATABASE $database TO $manager;
SQL
run() { timeout 120 docker run --rm --read-only --tmpfs /tmp:size=64m --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 512m --cpus 1 --network "container:$container" --env-file "$env_file" -v "$catalog:/run/platform-catalog.json:ro" "$image" node scripts/platform-db.mjs "$1" /run/platform-catalog.json; }
expected="$(run inspect)"; run migrate >/dev/null; run import >/dev/null
readback="$(run readback)"
python3 - "$receipt" "$commit" "$image_id" "$expected" "$readback" "$catalog_sha" <<'PY'
import json, os, pathlib, sys
expected=json.loads(sys.argv[4])['result']; result=json.loads(sys.argv[5])['result']; assert expected == result
out={'schema':'metahumotonic/platform-migration@1','status':'VERIFIED_BEFORE_RUNTIME_GRANT','commit':sys.argv[2],'image':sys.argv[3],'database':'metahumotonic_platform','catalogArtifactSha256':sys.argv[6],**result,'secretMaterialPrinted':False,'wikiTouched':False}
p=pathlib.Path(sys.argv[1]); t=p.with_suffix('.tmp'); t.write_text(json.dumps(out,sort_keys=True)+'\n'); os.chmod(t,0o600); t.replace(p)
PY
if ! cleanup_manager; then trap - EXIT; exit 1; fi
if ! "$provision" grant-runtime >/dev/null; then fail 'runtime grant failed; verified receipt retained for retry'; fi
python3 - "$receipt" <<'PY'
import json, os, pathlib, sys
p = pathlib.Path(sys.argv[1]); receipt = json.loads(p.read_text(encoding='utf-8'))
assert receipt.get('status') == 'VERIFIED_BEFORE_RUNTIME_GRANT'
receipt['status'] = 'PASS'
t = p.with_suffix('.tmp'); t.write_text(json.dumps(receipt, sort_keys=True)+'\n', encoding='utf-8'); os.chmod(t, 0o600); t.replace(p)
PY
trap - EXIT
printf '{"schema":"metahumotonic/platform-migration@1","status":"PASS","commit":"%s","image":"%s","secretMaterialPrinted":false,"wikiTouched":false}\n' "$commit" "$image_id"
