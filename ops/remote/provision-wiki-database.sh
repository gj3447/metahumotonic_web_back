#!/usr/bin/env bash
# Runs as root on data-01. Creates or compensates one exact bootstrap transaction.
set -Eeuo pipefail

mode="${1:-}"
container="${2:-postgresql}"
role="${3:-mhb_wiki}"
database="${4:-metahumotonic_wiki}"
backup_root="${5:-/var/lib/metahumotonic-wiki/bootstrap}"
key_file="${6:-/etc/metahumotonic/wiki-backup.key}"

[[ "$mode" == create || "$mode" == rollback ]]
[[ "$container" =~ ^[A-Za-z0-9._-]+$ ]]
[[ "$role" =~ ^[a-z][a-z0-9_]{2,30}$ ]]
[[ "$database" =~ ^[a-z][a-z0-9_]{2,62}$ ]]
[[ "$backup_root" =~ ^/[A-Za-z0-9._/-]+$ ]]
[[ "$key_file" =~ ^/[A-Za-z0-9._/-]+$ ]]

IFS= read -r transaction_id
[[ "$transaction_id" =~ ^[0-9a-f]{32}$ ]]
run_dir="$backup_root/$transaction_id"
receipt="$run_dir/receipt.json"
restore_database="${database}_restore_${transaction_id:0:12}"

owned_comment() {
  printf 'mhb-wiki-provision:%s' "$transaction_id"
}

is_owned() {
  local kind="$1" name="$2" query
  if [[ "$kind" == role ]]; then
    query="SELECT COALESCE(shobj_description(oid, 'pg_authid'),'') FROM pg_roles WHERE rolname='$name'"
  else
    query="SELECT COALESCE(shobj_description(oid, 'pg_database'),'') FROM pg_database WHERE datname='$name'"
  fi
  test "$(docker exec "$container" psql -U postgres -Atqc "$query")" = "$(owned_comment)"
}

is_reserved_uncommented_database() {
  local name="$1" marker owner status
  test -f "$receipt" || return 1
  status="$(python3 - "$receipt" "$transaction_id" <<'PY'
import json,pathlib,sys
b=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert b.get("transaction_id")==sys.argv[2] and b.get("ownership_nonce")==sys.argv[2]; print(b.get("status",""))
PY
)"
  [[ "$status" == OWNERSHIP_RESERVED || "$status" == FAILED_COMPENSATION_REQUIRES_OPERATOR ]] || return 1
  is_owned role "$role" || return 1
  owner="$(docker exec "$container" psql -U postgres -Atqc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='$name'")"
  marker="$(docker exec "$container" psql -U postgres -Atqc "SELECT COALESCE(shobj_description(oid,'pg_database'),'') FROM pg_database WHERE datname='$name'")"
  test "$owner" = "$role" && test -z "$marker"
}

drop_exact_resources() {
  local failed=false
  if is_owned database "$restore_database" || is_reserved_uncommented_database "$restore_database"; then docker exec "$container" dropdb -U postgres "$restore_database" >/dev/null 2>&1 || failed=true; fi
  if is_owned database "$database" || is_reserved_uncommented_database "$database"; then docker exec "$container" dropdb -U postgres "$database" >/dev/null 2>&1 || failed=true; fi
  if is_owned role "$role"; then docker exec "$container" dropuser -U postgres "$role" >/dev/null 2>&1 || failed=true; fi
  role_left="$(docker exec "$container" psql -U postgres -Atqc "SELECT count(*) FROM pg_roles WHERE rolname = '$role'")" || failed=true
  database_left="$(docker exec "$container" psql -U postgres -Atqc "SELECT count(*) FROM pg_database WHERE datname IN ('$database', '$restore_database')")" || failed=true
  # Existing resources without this transaction's ownership nonce are never deleted.
  [[ "${role_left:-1}" == 0 && "${database_left:-1}" == 0 ]] || failed=true
  [[ "$failed" == false ]]
}

record_compensation_failure() {
  local reason="$1" created_at
  created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  install -d -m 700 -o root -g root "$run_dir"
  python3 - "$receipt" "$transaction_id" "$database" "$role" "$created_at" "$reason" <<'PY'
import json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
payload.update({
    "schema": "metahumotonic/wiki-bootstrap-receipt@1",
    "transaction_id": sys.argv[2],
    "database": sys.argv[3],
    "role": sys.argv[4],
    "compensation_failed_at": sys.argv[5],
    "status": "FAILED_COMPENSATION_REQUIRES_OPERATOR",
    "failure": sys.argv[6],
})
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
temporary.replace(path)
PY
}

if [[ "$mode" == rollback ]]; then
  if test -f "$receipt"; then
    python3 - "$receipt" "$transaction_id" "$database" <<'PY'
import json, pathlib, sys
body = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert body.get("transaction_id") == sys.argv[2]
assert body.get("ownership_nonce") == sys.argv[2]
assert body.get("database") == sys.argv[3]
assert body.get("status") in {"OWNERSHIP_RESERVED", "VERIFIED", "FAILED_COMPENSATION_REQUIRES_OPERATOR"}
PY
  elif ! test -d "$run_dir"; then
    printf 'PASS no database resources found for transaction %s\n' "$transaction_id"
    exit 0
  else
    printf 'FAIL ownership receipt missing; refusing database compensation\n' >&2
    exit 1
  fi
  if ! drop_exact_resources; then
    record_compensation_failure "rollback could not remove exact database resources"
    printf 'FAIL database compensation incomplete; evidence retained at %s\n' "$receipt" >&2
    exit 1
  fi
  rm -rf -- "$run_dir"
  rm -f -- "$key_file"
  printf 'PASS compensated wiki database bootstrap %s\n' "$transaction_id"
  exit 0
fi

IFS= read -r password
IFS= read -r backup_key
[[ "$password" =~ ^[0-9a-f]{64}$ ]]
[[ "$backup_key" =~ ^[0-9a-f]{64}$ ]]

role_exists="$(docker exec "$container" psql -U postgres -Atqc \
  "SELECT count(*) FROM pg_roles WHERE rolname = '$role'")"
database_exists="$(docker exec "$container" psql -U postgres -Atqc \
  "SELECT count(*) FROM pg_database WHERE datname = '$database'")"
test "$role_exists" = 0
test "$database_exists" = 0
test ! -e "$run_dir"
test ! -e "$key_file"

cleanup_failed_create() {
  local status=$?
  trap - ERR
  set +e
  if drop_exact_resources; then
    rm -rf -- "$run_dir"
    rm -f -- "$key_file"
  else
    record_compensation_failure "create failed and automatic compensation was incomplete"
    printf 'FAIL database create compensation incomplete; evidence retained at %s\n' "$receipt" >&2
  fi
  exit "$status"
}
trap cleanup_failed_create ERR

install -d -m 700 -o root -g root "$backup_root" "$(dirname "$key_file")"
install -d -m 700 -o root -g root "$run_dir"
plain_dump="$run_dir/bootstrap.dump"
encrypted_dump="$run_dir/bootstrap.dump.enc"
python3 - "$receipt" "$transaction_id" "$database" "$role" <<'PY'
import json, os, pathlib, sys
p=pathlib.Path(sys.argv[1]); t=p.with_suffix('.tmp')
t.write_text(json.dumps({"schema":"metahumotonic/wiki-bootstrap-receipt@1","transaction_id":sys.argv[2],"ownership_nonce":sys.argv[2],"database":sys.argv[3],"role":sys.argv[4],"status":"OWNERSHIP_RESERVED"},sort_keys=True)+"\n")
os.chmod(t,0o600); t.replace(p)
PY
test "$(stat -c '%U:%G:%a' "$receipt")" = root:root:600

sql="BEGIN; CREATE ROLE $role LOGIN PASSWORD '$password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT; COMMENT ON ROLE $role IS '$(owned_comment)'; COMMIT;"
printf '%s\n' "$sql" | docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null
docker exec "$container" createdb -U postgres -O "$role" "$database"
printf '%s\n' "COMMENT ON DATABASE $database IS '$(owned_comment)';" | docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null
printf '%s\n' \
  "REVOKE ALL ON DATABASE $database FROM PUBLIC;" \
  "GRANT CONNECT, TEMPORARY ON DATABASE $database TO $role;" \
  | docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null

# Capture an encrypted bootstrap baseline, then prove that it restores into a
# disposable database before publishing the VERIFIED receipt.
docker exec "$container" pg_dump -U postgres -Fc "$database" >"$plain_dump"
printf '%s' "$backup_key" | openssl enc -aes-256-cbc -pbkdf2 -salt \
  -in "$plain_dump" -out "$encrypted_dump" -pass stdin
rm -f -- "$plain_dump"
umask 077
printf '%s\n' "$backup_key" >"$key_file"
chown root:root "$key_file"
chmod 600 "$key_file" "$encrypted_dump"
test "$(stat -c '%U:%G:%a' "$key_file")" = root:root:600
test "$(stat -c '%U:%G:%a' "$encrypted_dump")" = root:root:600
test "$(stat -c '%U:%G:%a' "$run_dir")" = root:root:700

docker exec "$container" createdb -U postgres -O "$role" "$restore_database"
printf '%s\n' "COMMENT ON DATABASE $restore_database IS '$(owned_comment)';" | docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null
printf '%s' "$backup_key" | openssl enc -d -aes-256-cbc -pbkdf2 \
  -in "$encrypted_dump" -pass stdin \
  | docker exec -i "$container" pg_restore -U postgres --exit-on-error -d "$restore_database"
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$restore_database" -Atqc \
  "SELECT current_database()" | grep -qx "$restore_database"
docker exec "$container" dropdb -U postgres "$restore_database"

backup_sha="$(sha256sum "$encrypted_dump" | awk '{print $1}')"
key_sha="$(sha256sum "$key_file" | awk '{print $1}')"
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$receipt" "$transaction_id" "$database" "$role" "$encrypted_dump" \
  "$backup_sha" "$created_at" "$key_file" "$key_sha" <<'PY'
import json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
payload = {
    "schema": "metahumotonic/wiki-bootstrap-receipt@1",
    "transaction_id": sys.argv[2],
    "ownership_nonce": sys.argv[2],
    "database": sys.argv[3],
    "role": sys.argv[4],
    "encrypted_backup": sys.argv[5],
    "backup_sha256": sys.argv[6],
    "created_at": sys.argv[7],
    "encryption": "AES-256-CBC-PBKDF2",
    "key_file": sys.argv[8],
    "key_sha256": sys.argv[9],
    "restore_drill": "PASS",
    "status": "VERIFIED",
}
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
temporary.replace(path)
print(json.dumps(payload, sort_keys=True))
PY
test "$(stat -c '%U:%G:%a' "$receipt")" = root:root:600
test "$(stat -c '%U:%G:%a' "$run_dir")" = root:root:700
test "$(stat -c '%U:%G:%a' "$backup_root")" = root:root:700
test "$(stat -c '%U:%G:%a' "$(dirname "$key_file")")" = root:root:700

trap - ERR
