#!/usr/bin/env bash
# Private-only controller. It consumes a receipt-owned, already-restored Wiki canary DB.
set -Eeuo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_host="${MHB_RUNTIME_HOST:-metahumotonic27@192.168.0.24}"; data_host="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
commit="${MHB_WIKI_STATEFUL_COMMIT:-}"; nonce="${MHB_WIKI_STATEFUL_NONCE:-}"
python_image="${MHB_WIKI_STATEFUL_PYTHON_IMAGE:-}"; gateway_image="${MHB_WIKI_STATEFUL_GATEWAY_IMAGE:-}"
database="${MHB_WIKI_STATEFUL_DATABASE:-}"; env_file="${MHB_WIKI_STATEFUL_ENV_FILE:-/etc/metahumotonic/web-back.env}"
mode="${1:-dry-run}"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(dry-run|run|cleanup)$ ]] || fail 'usage: dry-run|run|cleanup'
[[ "$runtime_host" =~ ^[A-Za-z0-9._@-]+$ && "$data_host" =~ ^[A-Za-z0-9._@-]+$ && "$commit" =~ ^[0-9a-f]{40}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'invalid target, commit, or nonce'
[[ "$python_image" =~ ^metahumotonic-web-back:[A-Za-z0-9._-]+-x86$ && "$gateway_image" =~ ^metahumotonic-web-back-ts:[A-Za-z0-9._-]+-x86$ ]] || fail 'exact Python and TS image tags are required'
[[ "$database" == "metahumotonic_wiki_canary_${commit:0:12}_${nonce:0:12}" && "$env_file" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'database must be exact disposable canary name and env path must be absolute'
redis_image="$(tr -d '\n' <"$repo_root/ops/redis-canary-image.txt")"; [[ "$redis_image" =~ ^redis:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]] || fail 'invalid pinned Redis canary image'
if [[ "$mode" == dry-run ]]; then printf '{"schema":"metahumotonic/wiki-stateful-delegation-controller@1","mode":"dry-run","commit":"%s","database":"%s","publicIngressChanged":false,"productionDatabaseMutated":false,"prerequisites":["exact images already built on VM100","data-01 verify-restored receipt/owner check"]}\n' "$commit" "$database"; exit 0; fi
for command in ssh scp mktemp; do command -v "$command" >/dev/null || fail "$command is required"; done
remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$runtime_host" "$@"; }; data_remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$data_host" "$@"; }
user_name="$(id -un)"; group_name="$(id -gn)"; runtime_stage=""; data_stage=""
valid_stage() { [[ "$1" =~ ^/var/tmp/mhb-wiki-stateful\.[A-Za-z0-9]{6}$ ]]; }
remove_stage() { local host="$1" stage="$2"; [[ -z "$stage" ]] && return 0; valid_stage "$stage" || return 1; ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" "test ! -L '$stage' && test \"\$(stat -c '%U:%G:%a' '$stage')\" = '$user_name:$group_name:700' && rm -f -- '$stage'/'canary.sh' '$stage'/'runtime.sh' '$stage'/'data.sh' && rmdir -- '$stage'" >/dev/null 2>&1; }
cleanup_local() { remove_stage "$runtime_host" "$runtime_stage" || true; remove_stage "$data_host" "$data_stage" || true; }; trap cleanup_local EXIT
runtime_stage="$(remote 'umask 077; mktemp -d /var/tmp/mhb-wiki-stateful.XXXXXX')"; valid_stage "$runtime_stage" || fail 'unsafe runtime staging path'
data_stage="$(data_remote 'umask 077; mktemp -d /var/tmp/mhb-wiki-stateful.XXXXXX')"; valid_stage "$data_stage" || fail 'unsafe data staging path'
for pair in "$runtime_host:$runtime_stage" "$data_host:$data_stage"; do host="${pair%%:*}"; stage="${pair#*:}"; ssh -o BatchMode=yes "$host" "test ! -L '$stage' && test \"\$(stat -c '%U:%G:%a' '$stage')\" = '$user_name:$group_name:700'"; done
canary="$runtime_stage/canary.sh"; runtime="$runtime_stage/runtime.sh"; data_helper="$data_stage/data.sh"
scp -q -o BatchMode=yes "$repo_root/ops/remote/run-ts-wiki-stateful-canary.sh" "$runtime_host:$canary"
scp -q -o BatchMode=yes "$repo_root/ops/remote/manage-wiki-canary-runtime.sh" "$runtime_host:$runtime"
scp -q -o BatchMode=yes "$repo_root/ops/remote/manage-wiki-canary-database.sh" "$data_host:$data_helper"
remote "test -f '$canary' && test -f '$runtime' && test ! -L '$canary' && test ! -L '$runtime'"
data_remote "test -f '$data_helper' && test ! -L '$data_helper'"
gateway_name="mhb-wiki-canary-gateway-${commit:0:12}-${nonce:0:12}"
cleanup_gateway() { remote "if sudo -n docker container inspect '$gateway_name' >/dev/null 2>&1; then test \"\$(sudo -n docker inspect '$gateway_name' --format '{{index .Config.Labels \"com.metahumotonic.wiki-canary.commit\"}}')\" = '$commit' && test \"\$(sudo -n docker inspect '$gateway_name' --format '{{index .Config.Labels \"com.metahumotonic.wiki-canary.nonce\"}}')\" = '$nonce' && sudo -n docker rm -f '$gateway_name' >/dev/null; fi"; }
receipt="/var/lib/metahumotonic-web-back/private-wiki-stateful-receipts/${commit}-${nonce}.json"
if [[ "$mode" == cleanup ]]; then cleanup_gateway; remote "sudo -n bash '$runtime' cleanup '$commit' '$nonce'"; exit 0; fi
# This is read-only: the helper validates receipt status plus DB owner/comment before runtime starts.
data_remote "sudo -n bash '$data_helper' verify-restored postgresql '$database' mhb_wiki unused unused '$commit' '$nonce' unused" >/dev/null
remote "test \"\$(sudo -n stat -c '%U:%G:%a' '/var/lib/metahumotonic-web-back/releases/$commit')\" = 'root:root:700'"
remote "sudo -n install -d -m 700 -o root -g root /var/lib/metahumotonic-web-back/private-wiki-stateful-receipts"
remote "sudo -n bash '$canary' '$python_image' '$commit' '$env_file' '$database' '$receipt' '$nonce' '$redis_image' '$runtime' '$gateway_image'"
