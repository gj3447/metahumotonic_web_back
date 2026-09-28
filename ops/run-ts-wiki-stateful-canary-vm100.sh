#!/usr/bin/env bash
# Private-only controller. The caller supplies an already-provisioned disposable Wiki DB.
set -Eeuo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_host="${MHB_RUNTIME_HOST:-metahumotonic27@192.168.0.24}"
commit="${MHB_WIKI_STATEFUL_COMMIT:-}"; nonce="${MHB_WIKI_STATEFUL_NONCE:-}"
python_image="${MHB_WIKI_STATEFUL_PYTHON_IMAGE:-}"; gateway_image="${MHB_WIKI_STATEFUL_GATEWAY_IMAGE:-}"
database="${MHB_WIKI_STATEFUL_DATABASE:-}"; env_file="${MHB_WIKI_STATEFUL_ENV_FILE:-/etc/metahumotonic/web-back.env}"
mode="${1:-dry-run}"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$mode" =~ ^(dry-run|run|cleanup)$ ]] || fail 'usage: dry-run|run|cleanup'
[[ "$runtime_host" =~ ^[A-Za-z0-9._@-]+$ && "$commit" =~ ^[0-9a-f]{40}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'invalid runtime target, commit, or nonce'
[[ "$python_image" =~ ^metahumotonic-web-back:[A-Za-z0-9._-]+-x86$ && "$gateway_image" =~ ^metahumotonic-web-back-ts:[A-Za-z0-9._-]+-x86$ ]] || fail 'exact Python and TS image tags are required'
[[ "$database" == "metahumotonic_wiki_canary_${commit:0:12}_${nonce:0:12}" && "$env_file" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'database must be the exact disposable canary name and env path must be absolute'
redis_image="$(tr -d '\n' <"$repo_root/ops/redis-canary-image.txt")"; [[ "$redis_image" =~ ^redis:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]] || fail 'invalid pinned Redis canary image'
if [[ "$mode" == dry-run ]]; then
  printf '{"schema":"metahumotonic/wiki-stateful-delegation-controller@1","mode":"dry-run","commit":"%s","database":"%s","publicIngressChanged":false,"productionDatabaseMutated":false,"prerequisites":["exact images already built on VM100","disposable database already created by data-01 helper"]}\n' "$commit" "$database"
  exit 0
fi
for command in ssh scp openssl; do command -v "$command" >/dev/null || fail "$command is required"; done
remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$runtime_host" "$@"; }
stage="/var/tmp/mhb-wiki-stateful-helper-${nonce:0:12}"; canary="$stage/canary.sh"; runtime="$stage/runtime.sh"
cleanup_local() { remote "sudo -n rm -rf -- '$stage'" >/dev/null 2>&1 || true; }; trap cleanup_local EXIT
remote "install -d -m 700 '$stage'"
scp -q -o BatchMode=yes "$repo_root/ops/remote/run-ts-wiki-stateful-canary.sh" "$runtime_host:$canary"
scp -q -o BatchMode=yes "$repo_root/ops/remote/manage-wiki-canary-runtime.sh" "$runtime_host:$runtime"
remote "test \"\$(stat -c '%U:%G:%a' '$stage')\" = '$(id -un):$(id -gn):700' && test -f '$canary' && test -f '$runtime' && test ! -L '$canary' && test ! -L '$runtime'"
receipt="/var/lib/metahumotonic-web-back/private-wiki-stateful-receipts/${commit}-${nonce}.json"
if [[ "$mode" == cleanup ]]; then
  remote "sudo -n bash '$runtime' cleanup '$commit' '$nonce'"
  exit 0
fi
remote "sudo -n install -d -m 700 -o root -g root /var/lib/metahumotonic-web-back/private-wiki-stateful-receipts"
remote "sudo -n bash '$canary' '$python_image' '$commit' '$env_file' '$database' '$receipt' '$nonce' '$redis_image' '$runtime' '$gateway_image'"
