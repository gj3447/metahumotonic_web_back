#!/usr/bin/env bash
# Run a temporary, unpublished TypeScript gateway canary beside VM100 Python.
#
# Usage:
#   ops/run-ts-backend-canary-vm100.sh COMMIT40
#   ops/run-ts-backend-canary-vm100.sh --status COMMIT40 NONCE32
#   ops/run-ts-backend-canary-vm100.sh --cleanup COMMIT40 NONCE32
#
# `run` accepts only a commit already reachable from origin/main, archives that
# exact tree, and automatically removes its owned container/image. It does not
# change ingress, Docker networks, existing Python containers or any database.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_host="${MHB_RUNTIME_HOST:-metahumotonic27@192.168.0.24}"
legacy_container="${MHB_CANARY_LEGACY_CONTAINER:-web-back-pve-1}"
mode=run
if [[ "${1:-}" == --status || "${1:-}" == --cleanup ]]; then mode="${1#--}"; shift; fi
commit="${1:-}"
nonce="${2:-}"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$runtime_host" =~ ^[A-Za-z0-9._@-]+$ ]] || fail 'unsafe VM100 SSH target'
[[ "$legacy_container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || fail 'unsafe legacy container name'
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail 'usage: run COMMIT40 | --status/--cleanup COMMIT40 NONCE32'
if [[ "$mode" != run ]]; then [[ "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'status/cleanup requires nonce32'; fi
for command in git ssh scp openssl; do command -v "$command" >/dev/null || fail "$command is required"; done

remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$runtime_host" "$@"; }
helper="/var/tmp/mhb-ts-canary-helper-${$}.sh"
cleanup_local() { remote "rm -f -- '$helper'" >/dev/null 2>&1 || true; }
trap cleanup_local EXIT
scp -q -o BatchMode=yes "$repo_root/ops/remote/run-ts-backend-canary.sh" "$runtime_host:$helper"

if [[ "$mode" != run ]]; then
  remote "sudo -n bash '$helper' '$mode' '$commit' '$nonce' '' '$legacy_container'"
  exit 0
fi

cd "$repo_root"
git fetch --prune origin main
git cat-file -e "${commit}^{commit}"
git merge-base --is-ancestor "$commit" origin/main || fail 'canary commit is not reachable from origin/main'
nonce="$(openssl rand -hex 16)"
remote_work="/var/tmp/mhb-ts-canary-${commit:0:12}-${nonce:0:12}"
archive="$remote_work/source.tar"
local_archive="$(mktemp)"
cleanup_archive() { rm -f -- "$local_archive"; }
trap 'cleanup_archive; cleanup_local' EXIT
git archive --format=tar "$commit" >"$local_archive"
remote "install -d -m 700 '$remote_work'"
scp -q -o BatchMode=yes "$local_archive" "$runtime_host:$archive"
remote "sudo -n bash '$helper' run '$commit' '$nonce' '$archive' '$legacy_container'"
