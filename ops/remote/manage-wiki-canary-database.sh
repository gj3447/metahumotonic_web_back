#!/usr/bin/env bash
# Receipt/COMMENT-owned disposable restore DB. Never performs broad cleanup.
set -Eeuo pipefail
mode="${1:-}"; container="${2:-postgresql}"; database="${3:-}"; role="${4:-mhb_wiki}"
encrypted_dump="${5:-}"; key_file="${6:-}"; commit="${7:-}"; nonce="${8:-}"
expected_key_sha="${9:-}"; receipt_root="${10:-/var/lib/metahumotonic-wiki/canaries}"
[[ "$mode" == create || "$mode" == drop || "$mode" == status ]]
[[ "$container" =~ ^[A-Za-z0-9._-]+$ ]]; [[ "$role" =~ ^[a-z][a-z0-9_]{2,30}$ ]]
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]; [[ "$nonce" =~ ^[0-9a-f]{32}$ ]]
[[ "$database" == "metahumotonic_wiki_canary_${commit:0:12}_${nonce:0:12}" ]]
receipt="$receipt_root/${commit}-${nonce}.json"; comment="mhb-wiki-canary:${commit}:${nonce}"

atomic_receipt() {
  local status="$1"; install -d -m 700 -o root -g root "$receipt_root"
  python3 - "$receipt" "$commit" "$nonce" "$database" "$status" <<'PY'
import json,os,pathlib,sys,datetime
p=pathlib.Path(sys.argv[1]); t=p.with_suffix('.tmp')
b={"schema":"metahumotonic/wiki-canary-db@1","commit":sys.argv[2],"rollout_nonce":sys.argv[3],"database":sys.argv[4],"status":sys.argv[5],"updated_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}
t.write_text(json.dumps(b,sort_keys=True)+"\n"); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
}
validate_receipt() { python3 - "$receipt" "$commit" "$nonce" "$database" <<'PY'
import json,pathlib,sys
b=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert b["commit"]==sys.argv[2] and b["rollout_nonce"]==sys.argv[3] and b["database"]==sys.argv[4]; assert b["status"] in {"RESERVED","RESTORED","CLEANUP_REQUIRED","DROPPED"}
PY
}
is_owned() {
  owner="$(docker exec "$container" psql -U postgres -Atqc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='$database'")"
  marker="$(docker exec "$container" psql -U postgres -Atqc "SELECT COALESCE(shobj_description(oid,'pg_database'),'') FROM pg_database WHERE datname='$database'")"
  test "$owner" = "$role" && test "$marker" = "$comment"
}
is_reserved_uncommented() {
  validate_receipt
  status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$receipt")"
  [[ "$status" == RESERVED || "$status" == CLEANUP_REQUIRED ]] || return 1
  owner="$(docker exec "$container" psql -U postgres -Atqc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='$database'")"
  marker="$(docker exec "$container" psql -U postgres -Atqc "SELECT COALESCE(shobj_description(oid,'pg_database'),'') FROM pg_database WHERE datname='$database'")"
  test "$owner" = "$role" && test -z "$marker"
}
drop_owned() {
  validate_receipt
  exists="$(docker exec "$container" psql -U postgres -Atqc "SELECT count(*) FROM pg_database WHERE datname='$database'")"
  if [[ "$exists" == 1 ]]; then
    is_owned || is_reserved_uncommented || { printf 'FAIL canary ownership mismatch; refusing drop %s\n' "$database" >&2; return 1; }
    docker exec "$container" dropdb -U postgres --force "$database" >/dev/null
  fi
  test "$(docker exec "$container" psql -U postgres -Atqc "SELECT count(*) FROM pg_database WHERE datname='$database'")" = 0
  atomic_receipt DROPPED
}

if [[ "$mode" == status ]]; then validate_receipt; cat "$receipt"; exit 0; fi
if [[ "$mode" == drop ]]; then drop_owned; printf 'PASS exact owned canary removed: %s\n' "$database"; exit 0; fi
[[ "$encrypted_dump" =~ ^/[A-Za-z0-9._/-]+$ ]]; [[ "$key_file" =~ ^/[A-Za-z0-9._/-]+$ ]]
[[ "$expected_key_sha" =~ ^[0-9a-f]{64}$ ]]
test "$(sha256sum "$key_file" | awk '{print $1}')" = "$expected_key_sha"
test ! -e "$receipt"; test "$(docker exec "$container" psql -U postgres -Atqc "SELECT count(*) FROM pg_database WHERE datname='$database'")" = 0
atomic_receipt RESERVED
cleanup_failed() { status=$?; trap - ERR; atomic_receipt CLEANUP_REQUIRED; drop_owned >/dev/null 2>&1 || true; exit "$status"; }
trap cleanup_failed ERR
docker exec "$container" createdb -U postgres -O "$role" "$database"
printf '%s\n' "COMMENT ON DATABASE $database IS '$comment';" | docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres >/dev/null
openssl enc -d -aes-256-cbc -pbkdf2 -in "$encrypted_dump" -pass "file:$key_file" | docker exec -i "$container" pg_restore -U postgres --exit-on-error -d "$database"
is_owned
atomic_receipt RESTORED
trap - ERR
printf 'PASS receipt-owned canary restored: %s\n' "$database"
