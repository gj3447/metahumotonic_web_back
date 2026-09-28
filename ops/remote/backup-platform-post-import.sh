#!/usr/bin/env bash
# Root-only, dedicated-DB post-import backup. It never accepts another DB name.
set -Eeuo pipefail
umask 077
mode="${1:-status}"; container="${2:-postgresql}"; nonce="${3:-}"
database="metahumotonic_platform"; owner="mhb_platform_owner"
backup_root="/var/lib/metahumotonic-platform/post-import-backups"
key_root="/etc/metahumotonic/platform-post-import-backups"
migration_receipt="/var/lib/metahumotonic-platform/migration/receipt.json"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(status|capture)$ && "$container" =~ ^[A-Za-z0-9._-]+$ ]] || fail 'invalid mode or container'
for command in docker openssl python3 sha256sum stat timeout; do command -v "$command" >/dev/null || fail "$command is required"; done
docker inspect "$container" >/dev/null 2>&1 || fail 'PostgreSQL container not found'
q() { docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -Atqc "$1"; }
qd() { docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" -Atqc "$1"; }
read -r migration_commit migration_catalog_sha < <(python3 - "$migration_receipt" <<'PY'
import json, pathlib, stat, sys
p = pathlib.Path(sys.argv[1]); s = p.stat()
assert s.st_uid == 0 and s.st_gid == 0 and stat.S_IMODE(s.st_mode) == 0o600
b = json.loads(p.read_text(encoding='utf-8'))
assert b.get('schema') == 'metahumotonic/platform-migration@1'
assert b.get('status') == 'PASS' and b.get('database') == 'metahumotonic_platform'
assert len(b.get('commit', '')) == 40 and len(b.get('catalogArtifactSha256', '')) == 64
print(b['commit'], b['catalogArtifactSha256'])
PY
) || fail 'missing or unverified platform migration receipt'
[[ "$migration_commit" =~ ^[0-9a-f]{40}$ && "$migration_catalog_sha" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid migration receipt fields'
[[ "$(q "SELECT count(*) FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname='$database' AND r.rolname='$owner'")" == 1 ]] || fail 'dedicated platform DB ownership check failed'
source_counts="$(qd "SELECT (SELECT count(*) FROM mhb_platform.schema_migrations), (SELECT count(*) FROM mhb_platform.catalog_versions), (SELECT count(*) FROM mhb_platform.asset_versions), (SELECT count(*) FROM mhb_platform.ingest_receipts), (SELECT count(*) FROM mhb_platform.observations)")"
[[ "$source_counts" =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+$ ]] || fail 'platform source tables unavailable'
if [[ "$mode" == status ]]; then
  printf '{"schema":"metahumotonic/platform-post-import-backup@1","mode":"status","database":"metahumotonic_platform","migrationCommit":"%s","catalogArtifactSha256":"%s","sourceCounts":"%s","databaseWrites":false,"wikiTouched":false,"secretMaterialPrinted":false}\n' "$migration_commit" "$migration_catalog_sha" "$source_counts"
  exit 0
fi
[[ "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'capture requires a 32-character hex nonce'
run_dir="$backup_root/${migration_commit}-${nonce}"; receipt="$run_dir/receipt.json"
encrypted="$run_dir/platform.dump.enc"; key_file="$key_root/${migration_commit}-${nonce}.key"
plain="$run_dir/platform.dump"; restored_plain="$run_dir/platform.restore.dump"; restore_container="mhb-platform-restore-$nonce"
test ! -e "$run_dir" && test ! -e "$key_file" || fail 'backup nonce already exists'
install -d -m 700 -o root -g root "$backup_root" "$key_root"
install -d -m 700 -o root -g root "$run_dir"
remove_restore_container() {
  docker container inspect "$restore_container" >/dev/null 2>&1 || return 0
  local owner_nonce
  owner_nonce="$(docker inspect "$restore_container" --format '{{index .Config.Labels "com.metahumotonic.platform-backup-nonce"}}')" || return 1
  [[ "$owner_nonce" == "$nonce" ]] || { printf 'FAIL restore container nonce ownership mismatch\n' >&2; return 1; }
  docker rm -f "$restore_container" >/dev/null
}
cleanup() {
  status=$?; trap - EXIT
  rm -f -- "$plain" "$restored_plain"
  if ! remove_restore_container; then exit 1; fi
  if [[ "$status" != 0 && ! -e "$receipt" ]]; then
    rm -f -- "$encrypted" "$key_file"
    rmdir -- "$run_dir" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT
IFS= read -r backup_key; [[ "$backup_key" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid backup key input'
timeout 300 docker exec "$container" pg_dump -U postgres -Fc "$database" >"$plain"
chown root:root "$plain"; chmod 600 "$plain"
plain_sha="$(sha256sum "$plain" | awk '{print $1}')"; [[ "$plain_sha" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid plaintext backup digest'
printf '%s' "$backup_key" | openssl enc -aes-256-cbc -pbkdf2 -salt -in "$plain" -out "$encrypted" -pass stdin
printf '%s\n' "$backup_key" >"$key_file"
chown root:root "$encrypted" "$key_file"; chmod 600 "$encrypted" "$key_file"
encrypted_sha="$(sha256sum "$encrypted" | awk '{print $1}')"; key_sha="$(sha256sum "$key_file" | awk '{print $1}')"
rm -f -- "$plain"
printf '%s' "$backup_key" | openssl enc -d -aes-256-cbc -pbkdf2 -in "$encrypted" -out "$restored_plain" -pass stdin
chown root:root "$restored_plain"; chmod 600 "$restored_plain"
[[ "$(sha256sum "$restored_plain" | awk '{print $1}')" == "$plain_sha" ]] || fail 'decrypt digest mismatch'
postgres_image="$(docker inspect "$container" --format '{{.Image}}')"; [[ "$postgres_image" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'current PostgreSQL image ID unavailable'
postgres_pgdata="$(docker inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^PGDATA=//p')"
[[ "$postgres_pgdata" =~ ^/var/lib/postgresql/[0-9]+/docker$ ]] || fail 'unsupported current PostgreSQL PGDATA'
timeout 60 docker run -d --name "$restore_container" --label "com.metahumotonic.platform-backup-nonce=$nonce" --network none --read-only --memory 1536m --memory-swap 1536m --cpus 1 --pids-limit 256 --tmpfs /var/lib/postgresql:rw,size=768m --tmpfs /var/run/postgresql:rw,size=16m --tmpfs /tmp:rw,size=128m -e POSTGRES_HOST_AUTH_METHOD=trust "$postgres_image" >/dev/null
for _ in $(seq 1 45); do docker exec "$restore_container" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec "$restore_container" pg_isready -U postgres >/dev/null 2>&1 || fail 'isolated restore PostgreSQL did not become ready'
timeout 300 docker exec -i "$restore_container" pg_restore -U postgres --no-owner --no-privileges -d postgres <"$restored_plain" >/dev/null
restored_counts="$(docker exec "$restore_container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -Atqc "SELECT (SELECT count(*) FROM mhb_platform.schema_migrations), (SELECT count(*) FROM mhb_platform.catalog_versions), (SELECT count(*) FROM mhb_platform.asset_versions), (SELECT count(*) FROM mhb_platform.ingest_receipts), (SELECT count(*) FROM mhb_platform.observations)")"
[[ "$restored_counts" == "$source_counts" ]] || fail 'isolated restore count mismatch'
for path in "$run_dir" "$backup_root" "$key_root"; do test "$(stat -c '%U:%G:%a' "$path")" = root:root:700; done
for path in "$encrypted" "$key_file"; do test "$(stat -c '%U:%G:%a' "$path")" = root:root:600; done
rm -f -- "$restored_plain"
remove_restore_container || fail 'isolated restore container cleanup failed'
python3 - "$receipt" "$migration_commit" "$migration_catalog_sha" "$encrypted" "$key_file" "$plain_sha" "$encrypted_sha" "$key_sha" "$source_counts" "$postgres_image" <<'PY'
import datetime, json, os, pathlib, sys
p=pathlib.Path(sys.argv[1]); t=p.with_suffix('.tmp')
b={"schema":"metahumotonic/platform-post-import-backup@1","status":"VERIFIED","restoreDrill":"PASS","database":"metahumotonic_platform","migrationCommit":sys.argv[2],"catalogArtifactSha256":sys.argv[3],"encryptedBackup":sys.argv[4],"keyFile":sys.argv[5],"plaintextBackupSha256":sys.argv[6],"encryptedBackupSha256":sys.argv[7],"keySha256":sys.argv[8],"sourceCounts":sys.argv[9],"restoreImage":sys.argv[10],"wikiTouched":False,"secretMaterialPrinted":False,"createdAt":datetime.datetime.now(datetime.timezone.utc).isoformat()}
t.write_text(json.dumps(b,sort_keys=True)+'\n',encoding='utf-8'); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
trap - EXIT
printf '{"schema":"metahumotonic/platform-post-import-backup@1","status":"VERIFIED","database":"metahumotonic_platform","migrationCommit":"%s","receiptPath":"%s","encryptedBackupSha256":"%s","plaintextBackupSha256":"%s","restoreDrill":"PASS","wikiTouched":false,"secretMaterialPrinted":false}\n' "$migration_commit" "$receipt" "$encrypted_sha" "$plain_sha"
