#!/usr/bin/env bash
# Root-only lock and non-secret failure evidence for the backup workflow.
set -Eeuo pipefail
mode="${1:-}"; token="${2:-}"; stage="${3:-}"
root="/var/lib/metahumotonic-platform"; lock="$root/backup-workflow-lock"; evidence_root="$root/backup-workflow-evidence"
[[ "$mode" =~ ^(acquire|release|failure)$ && "$token" =~ ^[0-9a-f]{32}$ ]] || { echo 'FAIL invalid workflow lock input' >&2; exit 1; }
case "$mode" in
  acquire)
    test ! -L "$root" && test ! -e "$lock" && test ! -L "$lock" || { echo 'FAIL workflow lock already held or unsafe' >&2; exit 1; }
    install -d -m 700 -o root -g root "$root"; mkdir -m 700 "$lock"
    printf '%s\n' "$token" >"$lock/owner"; chown root:root "$lock/owner"; chmod 600 "$lock/owner"
    ;;
  release)
    test ! -L "$lock" && test "$(cat "$lock/owner")" = "$token" || { echo 'FAIL workflow lock ownership mismatch' >&2; exit 1; }
    rm -f -- "$lock/owner"; rmdir -- "$lock"
    ;;
  failure)
    [[ "$stage" =~ ^[a-z0-9_-]{1,64}$ ]] || { echo 'FAIL invalid failure stage' >&2; exit 1; }
    install -d -m 700 -o root -g root "$root" "$evidence_root"; evidence="$evidence_root/$token.json"
    test ! -e "$evidence" && test ! -L "$evidence" || { echo 'FAIL failure evidence already exists' >&2; exit 1; }
    python3 - "$evidence" "$token" "$stage" <<'PY'
import datetime,json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); b={'schema':'metahumotonic/platform-backup-workflow@1','status':'FAILED','token':sys.argv[2],'stage':sys.argv[3],'secretMaterialPrinted':False,'wikiTouched':False,'createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}
t=p.with_suffix('.tmp'); t.write_text(json.dumps(b,sort_keys=True)+'\n'); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
    logger -p user.err -t mhb-platform-backup-workflow "failed stage=$stage token=$token"
    ;;
esac
