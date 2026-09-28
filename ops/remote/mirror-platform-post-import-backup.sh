#!/usr/bin/env bash
# Root-only source/destination helper for one verified platform backup mirror.
set -Eeuo pipefail
umask 077
mode="${1:-}"; receipt="${2:-}"
source_root="/var/lib/metahumotonic-platform/post-import-backups"
source_key_root="/etc/metahumotonic/platform-post-import-backups"
destination_root="/var/lib/metahumotonic-platform/offhost-backups"
destination_key_root="/etc/metahumotonic/platform-offhost-backups"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
for command in python3 sha256sum stat install mv; do command -v "$command" >/dev/null || fail "$command is required"; done
validate_source() {
  [[ "$receipt" =~ ^/var/lib/metahumotonic-platform/post-import-backups/[0-9a-f]{40}-[0-9a-f]{32}/receipt\.json$ ]] || fail 'invalid source receipt path'
  read -r backup_id encrypted key_file encrypted_sha key_sha receipt_sha < <(python3 - "$receipt" "$source_root" "$source_key_root" <<'PY'
import hashlib,json,pathlib,stat,sys
p=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); keyroot=pathlib.Path(sys.argv[3]); s=p.stat()
assert s.st_uid==0 and s.st_gid==0 and stat.S_IMODE(s.st_mode)==0o600 and not p.is_symlink()
b=json.loads(p.read_text(encoding='utf-8')); commit=b.get('migrationCommit','')
run=p.parent; expected_id=run.name
assert b.get('schema')=='metahumotonic/platform-post-import-backup@1' and b.get('status')=='VERIFIED' and b.get('restoreDrill')=='PASS' and b.get('database')=='metahumotonic_platform'
assert len(commit)==40 and expected_id.startswith(commit+'-') and len(expected_id)==73
encrypted=pathlib.Path(b['encryptedBackup']); key=pathlib.Path(b['keyFile'])
assert run.parent==root and encrypted==run/'platform.dump.enc' and key==keyroot/(expected_id+'.key')
for artifact,field in ((encrypted,'encryptedBackupSha256'),(key,'keySha256')):
    a=artifact.stat(); assert a.st_uid==0 and a.st_gid==0 and stat.S_IMODE(a.st_mode)==0o600 and not artifact.is_symlink()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest()==b[field]
assert len(b['encryptedBackupSha256'])==64 and len(b['keySha256'])==64
print(expected_id,encrypted,key,b['encryptedBackupSha256'],b['keySha256'],hashlib.sha256(p.read_bytes()).hexdigest())
PY
  ) || fail 'source backup receipt or artifacts are not verified'
  [[ "$backup_id" =~ ^[0-9a-f]{40}-[0-9a-f]{32}$ && "$encrypted_sha" =~ ^[0-9a-f]{64}$ && "$key_sha" =~ ^[0-9a-f]{64}$ && "$receipt_sha" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid verified source manifest'
}
case "$mode" in
  source-manifest)
    validate_source
    printf '%s %s %s %s %s %s\n' "$backup_id" "$encrypted" "$key_file" "$encrypted_sha" "$key_sha" "$receipt_sha"
    ;;
  source-stream)
    artifact="${3:-}"; validate_source
    case "$artifact" in encrypted) cat -- "$encrypted";; key) cat -- "$key_file";; receipt) cat -- "$receipt";; *) fail 'invalid source artifact';; esac
    ;;
  destination-prepare)
    backup_id="$receipt"; encrypted_sha="${3:-}"; key_sha="${4:-}"; receipt_sha="${5:-}"
    [[ "$backup_id" =~ ^[0-9a-f]{40}-[0-9a-f]{32}$ && "$encrypted_sha" =~ ^[0-9a-f]{64}$ && "$key_sha" =~ ^[0-9a-f]{64}$ && "$receipt_sha" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid destination manifest'
    run_dir="$destination_root/$backup_id"; key_file="$destination_key_root/$backup_id.key"
    for path in "$destination_root" "$destination_key_root"; do test ! -L "$path"; done
    test ! -e "$run_dir" && test ! -L "$run_dir" && test ! -e "$key_file" && test ! -L "$key_file" || fail 'destination backup already exists'
    install -d -m 700 -o root -g root "$destination_root" "$destination_key_root" "$run_dir"
    python3 - "$run_dir/receipt.json" "$backup_id" "$encrypted_sha" "$key_sha" "$receipt_sha" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); b={'schema':'metahumotonic/platform-offhost-mirror@1','status':'RESERVED','backupId':sys.argv[2],'sourceEncryptedBackupSha256':sys.argv[3],'sourceKeySha256':sys.argv[4],'sourceReceiptSha256':sys.argv[5],'secretMaterialPrinted':False,'wikiTouched':False}
t=p.with_suffix('.tmp'); t.write_text(json.dumps(b,sort_keys=True)+'\n'); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
    ;;
  destination-receive)
    backup_id="$receipt"; artifact="${3:-}"; expected_sha="${4:-}"
    [[ "$backup_id" =~ ^[0-9a-f]{40}-[0-9a-f]{32}$ && "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid destination receive inputs'
    run_dir="$destination_root/$backup_id"; key_file="$destination_key_root/$backup_id.key"; receipt_file="$run_dir/receipt.json"
    test ! -L "$run_dir" && test ! -L "$receipt_file" && test -f "$receipt_file" || fail 'destination reservation unavailable'
    case "$artifact" in encrypted) target="$run_dir/platform.dump.enc";; key) target="$key_file";; receipt) target="$run_dir/source-receipt.json";; *) fail 'invalid destination artifact';; esac
    test ! -e "$target" && test ! -L "$target" || fail 'destination artifact already exists'
    stage="$target.stage"; test ! -e "$stage" && test ! -L "$stage" || fail 'destination stage already exists'
    cat >"$stage"; chown root:root "$stage"; chmod 600 "$stage"
    [[ "$(sha256sum "$stage" | awk '{print $1}')" == "$expected_sha" ]] || { rm -f -- "$stage"; fail 'destination artifact digest mismatch'; }
    mv -T "$stage" "$target"
    ;;
  destination-finalize)
    backup_id="$receipt"; run_dir="$destination_root/$backup_id"; key_file="$destination_key_root/$backup_id.key"; receipt_file="$run_dir/receipt.json"
    [[ "$backup_id" =~ ^[0-9a-f]{40}-[0-9a-f]{32}$ ]] || fail 'invalid destination backup ID'
    python3 - "$receipt_file" "$run_dir/platform.dump.enc" "$key_file" "$run_dir/source-receipt.json" <<'PY'
import hashlib,json,os,pathlib,stat,sys
p,e,k,s=map(pathlib.Path,sys.argv[1:]); b=json.loads(p.read_text()); assert b.get('status')=='RESERVED'
for a,field in ((e,'sourceEncryptedBackupSha256'),(k,'sourceKeySha256'),(s,'sourceReceiptSha256')):
    m=a.stat(); assert m.st_uid==0 and m.st_gid==0 and stat.S_IMODE(m.st_mode)==0o600 and not a.is_symlink(); assert hashlib.sha256(a.read_bytes()).hexdigest()==b[field]
source=json.loads(s.read_text()); assert source.get('schema')=='metahumotonic/platform-post-import-backup@1' and source.get('status')=='VERIFIED' and source.get('restoreDrill')=='PASS' and source.get('database')=='metahumotonic_platform'
assert source['encryptedBackupSha256']==b['sourceEncryptedBackupSha256'] and source['keySha256']==b['sourceKeySha256']
b['status']='VERIFIED'; b['restoreDrill']='SOURCE_VERIFIED'; t=p.with_suffix('.tmp'); t.write_text(json.dumps(b,sort_keys=True)+'\n'); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
    for path in "$destination_root" "$destination_key_root" "$run_dir"; do test "$(stat -c '%U:%G:%a' "$path")" = root:root:700; done
    for path in "$receipt_file" "$run_dir/platform.dump.enc" "$run_dir/source-receipt.json" "$key_file"; do test "$(stat -c '%U:%G:%a' "$path")" = root:root:600; done
    printf '{"schema":"metahumotonic/platform-offhost-mirror@1","status":"VERIFIED","backupId":"%s","secretMaterialPrinted":false,"wikiTouched":false}\n' "$backup_id"
    ;;
  *) fail 'usage: source-manifest|source-stream|destination-prepare|destination-receive|destination-finalize';;
esac
