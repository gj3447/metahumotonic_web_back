#!/usr/bin/env bash
# Unpublished, read-only TS/Effect shadow canary beside the VM100 Python owner.
# It never changes ingress, EndpointSlices, existing containers, or databases.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_host="${MHB_RUNTIME_HOST:-metahumotonic27@192.168.0.24}"
legacy_container="${MHB_SHADOW_LEGACY_CONTAINER:-web-back-pve-1}"
runtime_env_file="${MHB_SHADOW_ENV_FILE:-/etc/metahumotonic/ts-shadow-readonly.env}"
mode=run
if [[ "${1:-}" == --dry-run || "${1:-}" == --status || "${1:-}" == --cleanup ]]; then mode="${1#--}"; shift; fi
commit="${1:-}"
nonce="${2:-}"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$runtime_host" =~ ^[A-Za-z0-9._@-]+$ ]] || fail 'unsafe VM100 SSH target'
[[ "$legacy_container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || fail 'unsafe Python container name'
[[ "$runtime_env_file" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'unsafe root-owned environment path'
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail 'usage: [--dry-run] COMMIT40 | --status/--cleanup COMMIT40 NONCE32'
if [[ "$mode" == status || "$mode" == cleanup ]]; then [[ "$nonce" =~ ^[0-9a-f]{32}$ ]] || fail 'status/cleanup requires nonce32'; fi
for command in git ssh scp openssl; do command -v "$command" >/dev/null || fail "$command is required"; done

cd "$repo_root"
git cat-file -e "${commit}^{commit}"
git merge-base --is-ancestor "$commit" origin/main || fail 'shadow commit is not reachable from origin/main'
if [[ "$mode" == dry-run ]]; then
  printf 'PASS shadow dry-run: exact source, no ingress, no write credentials, read-only HTTP boundary\n'
  exit 0
fi

remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$runtime_host" "$@"; }
helper="/var/tmp/mhb-ts-shadow-helper-${$}.sh"
cleanup_local() { remote "rm -f -- '$helper'" >/dev/null 2>&1 || true; }
trap cleanup_local EXIT
scp -q -o BatchMode=yes "$repo_root/ops/remote/run-ts-shadow-canary.sh" "$runtime_host:$helper"
if [[ "$mode" != run ]]; then
  remote "sudo -n bash '$helper' '$mode' '$commit' '$nonce' '' '$legacy_container' '$runtime_env_file'"
  exit 0
fi

nonce="$(openssl rand -hex 16)"
remote_work="/var/tmp/mhb-ts-shadow-${commit:0:12}-${nonce:0:12}"
archive="$remote_work/source.tar"
local_archive="$(mktemp)"
cleanup_archive() { rm -f -- "$local_archive"; }
trap 'cleanup_archive; cleanup_local' EXIT
git archive --format=tar "$commit" >"$local_archive"
archive_sha="$(sha256sum "$local_archive" | awk '{print $1}')"
remote "install -d -m 700 '$remote_work'"
scp -q -o BatchMode=yes "$local_archive" "$runtime_host:$archive"
remote "sudo -n chown root:root '$archive'; sudo -n chmod 600 '$archive'; sudo -n bash '$helper' run '$commit' '$nonce' '$archive' '$legacy_container' '$runtime_env_file' '$archive_sha'"
