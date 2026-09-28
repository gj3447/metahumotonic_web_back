#!/usr/bin/env bash
# Root-only VM100 restore drill for one verified encrypted platform mirror.
set -Eeuo pipefail
umask 077
mode="${1:-status}"; backup_id="${2:-}"; nonce="${3:-}"
mirror_root="/var/lib/metahumotonic-platform/offhost-backups"
key_root="/etc/metahumotonic/platform-offhost-backups"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(status|drill)$ && "$backup_id" =~ ^[0-9a-f]{40}-[0-9a-f]{32}$ ]] || fail 'invalid mode or backup ID'
for command in docker openssl python3 sha256sum stat install timeout; do command -v "$command" >/dev/null || fail "$command is required"; done
run_dir="$mirror_root/$backup_id"; mirror_receipt="$run_dir/receipt.json"; source_receipt="$run_dir/source-receipt.json"; encrypted="$run_dir/platform.dump.enc"; key_file="$key_root/$backup_id.key"
test ! -L "$mirror_root" && test ! -L "$key_root" && test ! -L "$run_dir" && test ! -L "$mirror_receipt" && test ! -L "$source_receipt" || fail 'symlinked mirror path refused'
read -r source_counts image encrypted_sha key_sha < <(python3 - "$mirror_receipt" "$source_receipt" "$encrypted" "$key_file" <<'PY'
import hashlib,json,pathlib,stat,sys
m,s,e,k=map(pathlib.Path,sys.argv[1:])
for p in (m,s,e,k):
    x=p.stat(); assert x.st_uid==0 and x.st_gid==0 and stat.S_IMODE(x.st_mode)==0o600 and not p.is_symlink()
mirror=json.loads(m.read_text()); source=json.loads(s.read_text())
assert mirror.get('schema')=='metahumotonic/platform-offhost-mirror@1' and mirror.get('status')=='VERIFIED' and mirror.get('restoreDrill')=='SOURCE_VERIFIED'
assert source.get('schema')=='metahumotonic/platform-post-import-backup@1' and source.get('status')=='VERIFIED' and source.get('restoreDrill')=='PASS' and source.get('database')=='metahumotonic_platform'
assert hashlib.sha256(e.read_bytes()).hexdigest()==mirror['sourceEncryptedBackupSha256']==source['encryptedBackupSha256']
assert hashlib.sha256(k.read_bytes()).hexdigest()==mirror['sourceKeySha256']==source['keySha256']
counts=source.get('sourceCounts',''); image=source.get('restoreImage','')
assert __import__('re').fullmatch(r'[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+',counts)
assert __import__('re').fullmatch(r'sha256:[0-9a-f]{64}',image)
print(counts,image,source['encryptedBackupSha256'],source['keySha256'])
PY
) || fail 'verified mirror or source receipt validation failed'
if [[ "$mode" == status ]]; then
  printf '{"schema":"metahumotonic/platform-offhost-restore-drill@1","mode":"status","backupId":"%s","sourceCounts":"%s","restoreImage":"%s","databaseWrites":false,"wikiTouched":false,"secretMaterialPrinted":false}\n' "$backup_id" "$source_counts" "$image"; exit 0
fi
[[ "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'drill requires a 32-character hex nonce'
drill_dir="$run_dir/drills/$nonce"; drill_receipt="$drill_dir/receipt.json"; plain="$drill_dir/platform.restore.dump"; restore_container="mhb-platform-offhost-restore-$nonce"
test ! -L "$run_dir/drills" && test ! -e "$drill_dir" && test ! -L "$drill_dir" || fail 'drill nonce already exists'
docker image inspect "$image" >/dev/null 2>&1 || fail 'verified restore image is not staged on VM100'
install -d -m 700 -o root -g root "$run_dir/drills" "$drill_dir"
remove_restore_container() {
  docker container inspect "$restore_container" >/dev/null 2>&1 || return 0
  local owner_nonce
  owner_nonce="$(docker inspect "$restore_container" --format '{{index .Config.Labels "com.metahumotonic.platform-offhost-restore-nonce"}}')" || return 1
  [[ "$owner_nonce" == "$nonce" ]] || { printf 'FAIL restore container nonce ownership mismatch\n' >&2; return 1; }
  docker rm -f "$restore_container" >/dev/null
}
cleanup() { status=$?; trap - EXIT; rm -f -- "$plain"; remove_restore_container || exit 1; exit "$status"; }
trap cleanup EXIT
openssl enc -d -aes-256-cbc -pbkdf2 -in "$encrypted" -out "$plain" -pass file:"$key_file"
chown root:root "$plain"; chmod 600 "$plain"
[[ "$(sha256sum "$plain" | awk '{print $1}')" == "$(python3 - "$source_receipt" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['plaintextBackupSha256'])
PY
)" ]] || fail 'decrypted backup digest mismatch'
pgdata="$(docker image inspect "$image" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^PGDATA=//p')"
[[ "$pgdata" =~ ^/var/lib/postgresql/[0-9]+/docker$ ]] || fail 'unsupported restore PostgreSQL PGDATA'
timeout 60 docker run -d --name "$restore_container" --label "com.metahumotonic.platform-offhost-restore-nonce=$nonce" --network none --read-only --memory 1536m --memory-swap 1536m --cpus 1 --pids-limit 256 --tmpfs /var/lib/postgresql:rw,size=768m --tmpfs /var/run/postgresql:rw,size=16m --tmpfs /tmp:rw,size=128m -e POSTGRES_HOST_AUTH_METHOD=trust "$image" >/dev/null
for _ in $(seq 1 45); do docker exec "$restore_container" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec "$restore_container" pg_isready -U postgres >/dev/null 2>&1 || fail 'isolated restore PostgreSQL did not become ready'
timeout 300 docker exec -i "$restore_container" pg_restore -U postgres --no-owner --no-privileges -d postgres <"$plain" >/dev/null
restored_counts="$(docker exec "$restore_container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -Atqc "SELECT (SELECT count(*) FROM mhb_platform.schema_migrations), (SELECT count(*) FROM mhb_platform.catalog_versions), (SELECT count(*) FROM mhb_platform.asset_versions), (SELECT count(*) FROM mhb_platform.ingest_receipts), (SELECT count(*) FROM mhb_platform.observations)")"
[[ "$restored_counts" == "$source_counts" ]] || fail 'isolated restore count mismatch'
rm -f -- "$plain"
remove_restore_container || fail 'isolated restore container cleanup failed'
python3 - "$drill_receipt" "$backup_id" "$nonce" "$source_counts" "$image" "$encrypted_sha" "$key_sha" <<'PY'
import datetime,json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); b={'schema':'metahumotonic/platform-offhost-restore-drill@1','status':'VERIFIED','restoreDrill':'PASS','backupId':sys.argv[2],'nonce':sys.argv[3],'sourceCounts':sys.argv[4],'restoreImage':sys.argv[5],'encryptedBackupSha256':sys.argv[6],'keySha256':sys.argv[7],'wikiTouched':False,'secretMaterialPrinted':False,'createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}
t=p.with_suffix('.tmp'); t.write_text(json.dumps(b,sort_keys=True)+'\n'); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
test "$(stat -c '%U:%G:%a' "$drill_dir")" = root:root:700 && test "$(stat -c '%U:%G:%a' "$drill_receipt")" = root:root:600
trap - EXIT
printf '{"schema":"metahumotonic/platform-offhost-restore-drill@1","status":"VERIFIED","backupId":"%s","restoreDrill":"PASS","wikiTouched":false,"secretMaterialPrinted":false}\n' "$backup_id"
