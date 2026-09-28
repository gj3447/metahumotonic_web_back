#!/usr/bin/env bash
# Private-only controller. It consumes a receipt-owned, already-restored Wiki canary DB.
set -Eeuo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_host="${MHB_RUNTIME_HOST:-metahumotonic27@192.168.0.24}"; data_host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
commit="${MHB_WIKI_STATEFUL_COMMIT:-}"; nonce="${MHB_WIKI_STATEFUL_NONCE:-}"
python_image="${MHB_WIKI_STATEFUL_PYTHON_IMAGE:-}"; gateway_image="${MHB_WIKI_STATEFUL_GATEWAY_IMAGE:-}"
database="${MHB_WIKI_STATEFUL_DATABASE:-}"; env_file="${MHB_WIKI_STATEFUL_ENV_FILE:-/etc/metahumotonic/web-back.env}"
backup_receipt="${MHB_WIKI_STATEFUL_BACKUP_RECEIPT:-}"
mode="${1:-dry-run}"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(dry-run|run|cleanup)$ ]] || fail 'usage: dry-run|run|cleanup'
[[ "$runtime_host" =~ ^[A-Za-z0-9._@-]+$ && "$data_host" =~ ^[A-Za-z0-9._@-]+$ && "$commit" =~ ^[0-9a-f]{40}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'invalid target, commit, or nonce'
[[ "$python_image" =~ ^metahumotonic-web-back:[A-Za-z0-9._-]+-x86$ && "$gateway_image" =~ ^metahumotonic-web-back-ts:[A-Za-z0-9._-]+-x86$ ]] || fail 'exact Python and TS image tags are required'
[[ "$database" == "metahumotonic_wiki_canary_${commit:0:12}_${nonce:0:12}" && "$env_file" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'database must be exact disposable canary name and env path must be absolute'
[[ "$backup_receipt" =~ ^/var/lib/metahumotonic-wiki/releases/[0-9a-f]{40}-[0-9a-f]{32}/current-backup-receipt\.json$ ]] || fail 'exact VERIFIED current-production backup receipt is required'
redis_image="$(tr -d '\n' <"$repo_root/ops/redis-canary-image.txt")"; [[ "$redis_image" =~ ^redis:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]] || fail 'invalid pinned Redis canary image'
if [[ "$mode" == dry-run ]]; then printf '{"schema":"metahumotonic/wiki-stateful-delegation-controller@1","mode":"dry-run","commit":"%s","database":"%s","publicIngressChanged":false,"productionDatabaseMutated":false,"prerequisites":["exact images already built on VM100","data-01 root-only VERIFIED current-production backup receipt"]}\n' "$commit" "$database"; exit 0; fi
for command in ssh scp mktemp; do command -v "$command" >/dev/null || fail "$command is required"; done
remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$runtime_host" "$@"; }; data_remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$data_host" "$@"; }
runtime_stage=""; data_stage=""
release_created=false
valid_stage() { [[ "$1" =~ ^/var/tmp/mhb-wiki-stateful\.[A-Za-z0-9]{6}$ ]]; }
remove_stage() { local host="$1" stage="$2"; [[ -z "$stage" ]] && return 0; valid_stage "$stage" || return 1; ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" "test ! -L '$stage' && test \"\$(stat -c '%u:%g:%a' '$stage')\" = \"\$(id -u):\$(id -g):700\" && rm -f -- '$stage'/'canary.sh' '$stage'/'runtime.sh' '$stage'/'data.sh' '$stage'/'lock.sh' && rmdir -- '$stage'" >/dev/null 2>&1; }
cleanup_local() { local failed=false; remove_stage "$runtime_host" "$runtime_stage" || failed=true; remove_stage "$data_host" "$data_stage" || failed=true; [[ "$failed" == false ]]; }
runtime_stage="$(remote 'umask 077; mktemp -d /var/tmp/mhb-wiki-stateful.XXXXXX')"; valid_stage "$runtime_stage" || fail 'unsafe runtime staging path'
trap cleanup_local EXIT
data_stage="$(data_remote 'umask 077; mktemp -d /var/tmp/mhb-wiki-stateful.XXXXXX')"; valid_stage "$data_stage" || fail 'unsafe data staging path'
for pair in "$runtime_host:$runtime_stage" "$data_host:$data_stage"; do host="${pair%%:*}"; stage="${pair#*:}"; ssh -o BatchMode=yes "$host" "test ! -L '$stage' && test \"\$(stat -c '%u:%g:%a' '$stage')\" = \"\$(id -u):\$(id -g):700\""; done
canary="$runtime_stage/canary.sh"; runtime="$runtime_stage/runtime.sh"; runtime_lock="$runtime_stage/lock.sh"; data_helper="$data_stage/data.sh"; data_lock="$data_stage/lock.sh"
scp -q -o BatchMode=yes "$repo_root/ops/remote/run-ts-wiki-stateful-canary.sh" "$runtime_host:$canary"
scp -q -o BatchMode=yes "$repo_root/ops/remote/manage-wiki-canary-runtime.sh" "$runtime_host:$runtime"
scp -q -o BatchMode=yes "$repo_root/ops/remote/manage-wiki-operation-lock.sh" "$runtime_host:$runtime_lock"
scp -q -o BatchMode=yes "$repo_root/ops/remote/manage-wiki-canary-database.sh" "$data_host:$data_helper"
scp -q -o BatchMode=yes "$repo_root/ops/remote/manage-wiki-operation-lock.sh" "$data_host:$data_lock"
remote "test -f '$canary' && test -f '$runtime' && test -f '$runtime_lock' && test ! -L '$canary' && test ! -L '$runtime' && test ! -L '$runtime_lock'"
data_remote "test -f '$data_helper' && test -f '$data_lock' && test ! -L '$data_helper' && test ! -L '$data_lock'"
gateway_name="mhb-wiki-canary-gateway-${commit:0:12}-${nonce:0:12}"
cleanup_gateway() { remote "if sudo -n docker container inspect '$gateway_name' >/dev/null 2>&1; then test \"\$(sudo -n docker inspect '$gateway_name' --format '{{index .Config.Labels \"com.metahumotonic.wiki-canary.commit\"}}')\" = '$commit' && test \"\$(sudo -n docker inspect '$gateway_name' --format '{{index .Config.Labels \"com.metahumotonic.wiki-canary.nonce\"}}')\" = '$nonce' && sudo -n docker rm -f '$gateway_name' >/dev/null; fi"; }
receipt="/var/lib/metahumotonic-web-back/private-wiki-stateful-receipts/${commit}-${nonce}.json"
database_owned=false; database_create_started=false; data_locked=false; runtime_locked=false
gateway_needs_cleanup() { remote "sudo -n docker container inspect '$gateway_name' >/dev/null 2>&1"; }
runtime_needs_cleanup() {
  local short="${commit:0:12}-${nonce:0:12}"
  remote "sudo -n test -e '/var/lib/metahumotonic-web-back/runtime-canaries/${commit}-${nonce}.json' || sudo -n docker container inspect 'mhb-wiki-canary-app-$short' >/dev/null 2>&1 || sudo -n docker container inspect 'mhb-wiki-canary-redis-$short' >/dev/null 2>&1 || sudo -n docker network inspect 'mhb-wiki-canary-$short' >/dev/null 2>&1 || sudo -n test -e '/var/lib/metahumotonic-web-back/releases/$commit/.canary-$short'"
}
cleanup_release_dir() {
  [[ "$release_created" == true ]] || return 0
  remote "sudo -n bash -s -- '$commit' <<'REMOTE'
set -Eeuo pipefail
directory=\"/var/lib/metahumotonic-web-back/releases/\$1\"
test ! -L \"\$directory\" && test \"\$(stat -c '%U:%G:%a' \"\$directory\")\" = root:root:700 && rmdir -- \"\$directory\"
REMOTE"
}
cleanup_all() {
  status=$?; trap - EXIT; local runtime_failed=false database_failed=false release_failed=false locks_failed=false staging_failed=false
  if gateway_needs_cleanup; then cleanup_gateway || runtime_failed=true; fi
  if runtime_needs_cleanup; then remote "sudo -n bash '$runtime' cleanup '$commit' '$nonce'" || runtime_failed=true; fi
  if [[ "$database_owned" == true ]]; then
    data_remote "sudo -n bash '$data_helper' drop postgresql '$database' mhb_wiki unused unused '$commit' '$nonce' unused" || database_failed=true
  elif [[ "$database_create_started" == true ]]; then
    if data_remote "sudo -n test -f '/var/lib/metahumotonic-wiki/canaries/${commit}-${nonce}.json'"; then
      data_remote "sudo -n bash '$data_helper' drop postgresql '$database' mhb_wiki unused unused '$commit' '$nonce' unused" || database_failed=true
    else
      data_remote "test \"\$(sudo -n docker exec postgresql psql -U postgres -Atqc \"SELECT count(*) FROM pg_database WHERE datname='$database'\")\" = 0" || database_failed=true
    fi
  fi
  cleanup_release_dir || release_failed=true
  if [[ "$runtime_locked" == true ]]; then remote "sudo -n bash '$runtime_lock' release mhb-wiki-runtime-operation '$nonce'" || locks_failed=true; fi
  if [[ "$data_locked" == true ]]; then data_remote "sudo -n bash '$data_lock' release mhb-wiki-data-operation '$nonce'" || locks_failed=true; fi
  cleanup_local || staging_failed=true
  [[ "$runtime_failed" == false ]] || { printf 'FAIL stateful drill runtime cleanup failed; exact runtime receipt requires operator recovery\n' >&2; exit 1; }
  [[ "$database_failed" == false ]] || { printf 'FAIL stateful drill database cleanup failed; inspect the exact data receipt\n' >&2; exit 1; }
  [[ "$release_failed" == false ]] || { printf 'FAIL stateful drill created release directory was not empty or owned\n' >&2; exit 1; }
  [[ "$locks_failed" == false ]] || { printf 'FAIL stateful drill operation lock release failed; exact lock requires operator recovery\n' >&2; exit 1; }
  [[ "$staging_failed" == false ]] || { printf 'FAIL stateful drill helper staging cleanup failed; exact user-owned staging paths retained\n' >&2; exit 1; }
  exit "$status"
}
trap cleanup_all EXIT
data_remote "sudo -n bash '$data_lock' acquire mhb-wiki-data-operation '$nonce'"; data_locked=true
remote "sudo -n bash '$runtime_lock' acquire mhb-wiki-runtime-operation '$nonce'"; runtime_locked=true
if [[ "$mode" == cleanup ]]; then
  if data_remote "sudo -n bash '$data_helper' status postgresql '$database' mhb_wiki unused unused '$commit' '$nonce' unused" >/dev/null; then database_owned=true; else data_remote "test \"\$(sudo -n docker exec postgresql psql -U postgres -Atqc \"SELECT count(*) FROM pg_database WHERE datname='$database'\")\" = 0" || fail 'foreign exact-name database has no owned receipt'; fi
  exit 0
fi
read -r encrypted_dump key_file key_sha < <(data_remote "sudo -n python3 - '$backup_receipt' <<'PY'
import hashlib,json,pathlib,stat,sys
p=pathlib.Path(sys.argv[1]); s=p.stat(); assert s.st_uid==0 and s.st_gid==0 and stat.S_IMODE(s.st_mode)==0o600 and not p.is_symlink()
b=json.loads(p.read_text()); run=p.parent; assert b.get('schema')=='metahumotonic/wiki-release-backup@1' and b.get('status')=='VERIFIED' and b.get('snapshot')=='current-production' and b.get('restore_drill')=='PASS'
e=pathlib.Path(b['encrypted_backup']); k=pathlib.Path(b['key_file']); assert e==run/'current.dump.enc' and k.parent==pathlib.Path('/etc/metahumotonic/wiki-release-backups')
for directory in (run, run.parent, k.parent):
 d=directory.stat(); assert d.st_uid==0 and d.st_gid==0 and stat.S_IMODE(d.st_mode)==0o700 and not directory.is_symlink()
def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as source:
  for chunk in iter(lambda: source.read(1024*1024), b''): h.update(chunk)
 return h.hexdigest()
for a, field in ((e,'backup_sha256'),(k,'key_sha256')):
 m=a.stat(); assert m.st_uid==0 and m.st_gid==0 and stat.S_IMODE(m.st_mode)==0o600 and not a.is_symlink(); assert digest(a)==b[field]
print(e,k,b['key_sha256'])
PY") || fail 'data-01 backup receipt or artifact validation failed'
[[ "$encrypted_dump" =~ ^/[A-Za-z0-9._/-]+$ && "$key_file" =~ ^/[A-Za-z0-9._/-]+$ && "$key_sha" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid verified backup manifest'
data_remote "sudo -n test ! -e '/var/lib/metahumotonic-wiki/canaries/${commit}-${nonce}.json' && test \"\$(sudo -n docker exec postgresql psql -U postgres -Atqc \"SELECT count(*) FROM pg_database WHERE datname='$database'\")\" = 0" || fail 'exact canary database or receipt already exists'
database_create_started=true
data_remote "sudo -n bash '$data_helper' create postgresql '$database' mhb_wiki '$encrypted_dump' '$key_file' '$commit' '$nonce' '$key_sha'" >/dev/null
database_owned=true
release_state="$(remote "sudo -n bash -s -- '$commit' <<'REMOTE'
set -Eeuo pipefail
directory=\"/var/lib/metahumotonic-web-back/releases/\$1\"
if test -e \"\$directory\"; then test ! -L \"\$directory\" && test \"\$(stat -c '%U:%G:%a' \"\$directory\")\" = root:root:700; printf PRESENT; else install -d -m 700 -o root -g root \"\$directory\"; printf CREATED; fi
REMOTE")"
[[ "$release_state" == PRESENT || "$release_state" == CREATED ]] || fail 'invalid release directory state'
[[ "$release_state" == CREATED ]] && release_created=true
remote "sudo -n install -d -m 700 -o root -g root /var/lib/metahumotonic-web-back/private-wiki-stateful-receipts"
remote "sudo -n bash '$canary' '$python_image' '$commit' '$env_file' '$database' '$receipt' '$nonce' '$redis_image' '$runtime' '$gateway_image'"
