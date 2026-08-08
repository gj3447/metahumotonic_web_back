#!/usr/bin/env bash
# Encrypted, commit/nonce-bound snapshot of the current production wiki DB.
set -Eeuo pipefail
mode="${1:-}"; container="${2:-postgresql}"; database="${3:-metahumotonic_wiki}"
commit="${4:-}"; nonce="${5:-}"; release_root="${6:-/var/lib/metahumotonic-wiki/releases}"
key_root="${7:-/etc/metahumotonic/wiki-release-backups}"
[[ "$mode" == capture || "$mode" == finalize ]]
[[ "$container" =~ ^[A-Za-z0-9._-]+$ ]]
[[ "$database" =~ ^[a-z][a-z0-9_]{2,62}$ ]]
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]; [[ "$nonce" =~ ^[0-9a-f]{32}$ ]]
run_dir="$release_root/${commit}-${nonce}"; receipt="$run_dir/current-backup-receipt.json"
encrypted="$run_dir/current.dump.enc"; key_file="$key_root/${commit}-${nonce}.key"
if [[ "$mode" == finalize ]]; then
  python3 - "$receipt" "$commit" "$nonce" <<'PY'
import hashlib,json,os,pathlib,stat,sys
p=pathlib.Path(sys.argv[1]); b=json.loads(p.read_text()); assert b["status"] in {"CAPTURED","VERIFIED"} and b["commit"]==sys.argv[2] and b["rollout_nonce"]==sys.argv[3]
for field,digest_field in (("encrypted_backup","backup_sha256"),("key_file","key_sha256")):
    artifact=pathlib.Path(b[field]); metadata=artifact.stat()
    assert metadata.st_uid==0 and metadata.st_gid==0 and stat.S_IMODE(metadata.st_mode)==0o600
    assert hashlib.sha256(artifact.read_bytes()).hexdigest()==b[digest_field]
if b["status"]=="CAPTURED":
    b["status"]="VERIFIED"; b["restore_drill"]="PASS"
    t=p.with_suffix('.tmp'); t.write_text(json.dumps(b,sort_keys=True)+"\n"); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
else:
    assert b["restore_drill"]=="PASS"
print(json.dumps(b,sort_keys=True))
PY
  exit 0
fi
IFS= read -r backup_key; [[ "$backup_key" =~ ^[0-9a-f]{64}$ ]]
test ! -e "$run_dir"
test ! -e "$key_file"
install -d -m 700 -o root -g root "$release_root" "$run_dir" "$key_root"
plain="$run_dir/current.dump"
cleanup() { status=$?; trap - ERR; rm -f -- "$plain"; exit "$status"; }
trap cleanup ERR
docker exec "$container" pg_dump -U postgres -Fc "$database" >"$plain"
printf '%s' "$backup_key" | openssl enc -aes-256-cbc -pbkdf2 -salt -in "$plain" -out "$encrypted" -pass stdin
rm -f -- "$plain"; printf '%s\n' "$backup_key" >"$key_file"
chown root:root "$encrypted" "$key_file"; chmod 600 "$encrypted" "$key_file"
sha="$(sha256sum "$encrypted" | awk '{print $1}')"
key_sha="$(sha256sum "$key_file" | awk '{print $1}')"
python3 - "$receipt" "$commit" "$nonce" "$database" "$encrypted" "$key_file" "$sha" "$key_sha" <<'PY'
import json,os,pathlib,sys,datetime
p=pathlib.Path(sys.argv[1]); t=p.with_suffix('.tmp')
b={"schema":"metahumotonic/wiki-release-backup@1","commit":sys.argv[2],"rollout_nonce":sys.argv[3],"source_database":sys.argv[4],"encrypted_backup":sys.argv[5],"key_file":sys.argv[6],"backup_sha256":sys.argv[7],"key_sha256":sys.argv[8],"snapshot":"current-production","restore_drill":"PENDING","status":"CAPTURED","created_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}
t.write_text(json.dumps(b,sort_keys=True)+"\n"); os.chmod(t,0o600); t.replace(p); print(json.dumps(b,sort_keys=True))
PY
for directory in "$release_root" "$run_dir" "$key_root"; do test "$(stat -c '%U:%G:%a' "$directory")" = root:root:700; done
for file in "$receipt" "$encrypted" "$key_file"; do test "$(stat -c '%U:%G:%a' "$file")" = root:root:600; done
trap - ERR
