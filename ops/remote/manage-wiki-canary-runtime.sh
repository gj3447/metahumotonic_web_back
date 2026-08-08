#!/usr/bin/env bash
# Durable receipt and exact-label cleanup for one runtime canary transaction.
set -Eeuo pipefail
umask 077
mode="${1:-}"; commit="${2:-}"; nonce="${3:-}"
root="${4:-/var/lib/metahumotonic-web-back/runtime-canaries}"
[[ "$mode" == reserve || "$mode" == status || "$mode" == cleanup ]]
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]; [[ "$nonce" =~ ^[0-9a-f]{32}$ ]]
short="${commit:0:12}-${nonce:0:12}"
app="mhb-wiki-canary-app-$short"; redis="mhb-wiki-canary-redis-$short"; network="mhb-wiki-canary-$short"
workdir="/var/lib/metahumotonic-web-back/releases/$commit/.canary-$short"
workdir_marker="$workdir/.mhb-wiki-canary-owner.json"
workdir_stage="${workdir}.reserve"
workdir_stage_marker="$workdir_stage/.mhb-wiki-canary-owner.json"
receipt="$root/${commit}-${nonce}.json"
atomic_receipt() {
  status="$1"; identity="${2:-}"; install -d -m 700 -o root -g root "$root"
  if [[ -z "$identity" && -f "$receipt" ]]; then
    identity="$(python3 - "$receipt" <<'PY'
import json,pathlib,sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text()).get("workdir_identity", ""))
PY
)"
  fi
  python3 - "$receipt" "$commit" "$nonce" "$app" "$redis" "$network" "$workdir" "$status" "$identity" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); t=p.with_name(p.name+f'.tmp.{os.getpid()}')
b={"schema":"metahumotonic/wiki-runtime-canary@1","commit":sys.argv[2],"rollout_nonce":sys.argv[3],"app":sys.argv[4],"redis":sys.argv[5],"network":sys.argv[6],"workdir":sys.argv[7],"status":sys.argv[8],"workdir_identity":sys.argv[9]}
t.write_text(json.dumps(b,sort_keys=True)+"\n"); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
}
validate() { test "$(stat -c '%U:%G:%a' "$receipt")" = root:root:600; python3 - "$receipt" "$commit" "$nonce" "$app" "$redis" "$network" "$workdir" <<'PY'
import json,pathlib,sys
b=json.loads(pathlib.Path(sys.argv[1]).read_text()); keys=("commit","rollout_nonce","app","redis","network","workdir")
assert all(b[k]==v for k,v in zip(keys,sys.argv[2:])); assert b["status"] in {"RESERVED","STAGED","CLEANUP_REQUIRED","CLEANED","REFUSED_WORKDIR_CONFLICT","REFUSED_STAGE_CONFLICT","REFUSED_STAGE_CREATE"}
identity=b.get("workdir_identity", ""); assert not identity or (":" in identity and all(p.isdigit() for p in identity.split(":")))
PY
}
owned_container() { test "$(docker inspect "$1" --format '{{index .Config.Labels "com.metahumotonic.wiki-canary.commit"}}')" = "$commit" && test "$(docker inspect "$1" --format '{{index .Config.Labels "com.metahumotonic.wiki-canary.nonce"}}')" = "$nonce"; }
owned_network() { test "$(docker network inspect "$1" --format '{{index .Labels "com.metahumotonic.wiki-canary.commit"}}')" = "$commit" && test "$(docker network inspect "$1" --format '{{index .Labels "com.metahumotonic.wiki-canary.nonce"}}')" = "$nonce"; }
owned_workdir_at() {
  local directory="$1" marker="$1/.mhb-wiki-canary-owner.json"
  test "$(stat -c '%U:%G:%a' "$directory")" = root:root:700
  test "$(stat -c '%U:%G:%a' "$marker")" = root:root:600
  local identity receipt_identity
  identity="$(stat -c '%d:%i' "$directory")"
  receipt_identity="$(python3 - "$receipt" <<'PY'
import json,pathlib,sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text())["workdir_identity"])
PY
)"
  test "$identity" = "$receipt_identity"
  python3 - "$marker" "$commit" "$nonce" "$workdir" "$identity" <<'PY'
import json,pathlib,sys
b=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert b=={"schema":"metahumotonic/wiki-runtime-canary-workdir@1","commit":sys.argv[2],"rollout_nonce":sys.argv[3],"workdir":sys.argv[4],"workdir_identity":sys.argv[5]}
PY
}
owned_workdir() { owned_workdir_at "$workdir"; }
cleanup_stage() {
  test -e "$workdir_stage" || return 0
  test -e "$workdir_stage_marker"
  owned_workdir_at "$workdir_stage"
  rm -rf -- "$workdir_stage"
}
if [[ "$mode" == reserve ]]; then
  test ! -e "$receipt"
  if test -e "$workdir"; then atomic_receipt REFUSED_WORKDIR_CONFLICT; exit 1; fi
  if test -e "$workdir_stage"; then atomic_receipt REFUSED_STAGE_CONFLICT; exit 1; fi
  atomic_receipt RESERVED
  if ! mkdir -m 700 -- "$workdir_stage"; then atomic_receipt REFUSED_STAGE_CREATE; exit 1; fi
  chown root:root "$workdir_stage"; chmod 700 "$workdir_stage"
  workdir_identity="$(stat -c '%d:%i' "$workdir_stage")"
  python3 - "$workdir_stage_marker" "$commit" "$nonce" "$workdir" "$workdir_identity" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); t=p.with_suffix('.tmp')
b={"schema":"metahumotonic/wiki-runtime-canary-workdir@1","commit":sys.argv[2],"rollout_nonce":sys.argv[3],"workdir":sys.argv[4],"workdir_identity":sys.argv[5]}
t.write_text(json.dumps(b,sort_keys=True)+"\n"); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
  atomic_receipt STAGED "$workdir_identity"
  owned_workdir_at "$workdir_stage"
  mv -T -- "$workdir_stage" "$workdir"
  owned_workdir; cat "$receipt"; exit 0
fi
validate
if [[ "$mode" == status ]]; then cat "$receipt"; exit 0; fi
receipt_status="$(python3 - "$receipt" <<'PY'
import json,pathlib,sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text())["status"])
PY
)"
case "$receipt_status" in
  REFUSED_*) printf 'FAIL runtime canary reservation was refused; foreign paths preserved\n' >&2; exit 1 ;;
  RESERVED)
    printf 'FAIL runtime canary reservation interrupted before ownership binding; stage preserved for inspection\n' >&2
    exit 1
    ;;
esac
atomic_receipt CLEANUP_REQUIRED
for name in "$app" "$redis"; do
  if docker container inspect "$name" >/dev/null 2>&1; then owned_container "$name" || { printf 'FAIL foreign runtime canary container %s\n' "$name" >&2; exit 1; }; docker rm -f "$name" >/dev/null; fi
done
if docker network inspect "$network" >/dev/null 2>&1; then owned_network "$network" || { printf 'FAIL foreign runtime canary network %s\n' "$network" >&2; exit 1; }; docker network rm "$network" >/dev/null; fi
[[ "$workdir" == "/var/lib/metahumotonic-web-back/releases/$commit/.canary-$short" ]]
if test -e "$workdir"; then owned_workdir || { printf 'FAIL foreign runtime canary workdir %s\n' "$workdir" >&2; exit 1; }; rm -rf -- "$workdir"; fi
cleanup_stage || { printf 'FAIL foreign runtime canary staging workdir %s\n' "$workdir_stage" >&2; exit 1; }
test ! -e "$workdir"; test ! -e "$workdir_stage"; ! docker container inspect "$app" >/dev/null 2>&1; ! docker container inspect "$redis" >/dev/null 2>&1; ! docker network inspect "$network" >/dev/null 2>&1
atomic_receipt CLEANED
printf 'PASS exact runtime canary cleanup %s %s\n' "$commit" "$nonce"
