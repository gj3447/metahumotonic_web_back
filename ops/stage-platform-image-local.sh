#!/usr/bin/env bash
# Builds an exact-commit TS image on VM100 and streams it to data-01. No DB use.
set -Eeuo pipefail
mode="${1:-dry-run}"; root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vm="${MHB_VM100_HOST:-metahumotonic27@192.168.0.24}"; data="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"; vm_repo="${MHB_VM100_REPO:-/home/metahumotonic/metahumotonic_web_back}"
commit="${MHB_PLATFORM_COMMIT:-$(git -C "$root" rev-parse HEAD)}"; name="mhb-platform-stage:${commit:0:12}"
fail() { echo "FAIL $*" >&2; exit 1; }
[[ "$mode" =~ ^(dry-run|stage)$ && "$commit" =~ ^[0-9a-f]{40}$ && "$vm" =~ ^[A-Za-z0-9._@-]+$ && "$data" =~ ^[A-Za-z0-9._@-]+$ && "$vm_repo" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail 'invalid input'
git -C "$root" cat-file -e "$commit^{commit}" || fail 'commit unavailable'
git -C "$root" merge-base --is-ancestor "$commit" origin/main || fail 'commit must be reachable from origin/main'
source_sha="$(git -C "$root" archive --format=tar "$commit" | sha256sum | awk '{print $1}')"
if [[ "$mode" == dry-run ]]; then printf '{"schema":"metahumotonic/platform-image-stage@1","mode":"dry-run","commit":"%s","sourceArchiveSha256":"%s","databaseWrites":false,"publicIngressChanged":false}\n' "$commit" "$source_sha"; exit 0; fi
ssh -o BatchMode=yes "$vm" "set -eu; test -d '$vm_repo/.git'; git -C '$vm_repo' cat-file -e '$commit^{commit}'; test \"\$(git -C '$vm_repo' archive --format=tar '$commit' | sha256sum | awk '{print \$1}')\" = '$source_sha'; tmp=\$(mktemp -d /var/tmp/mhb-platform-build.XXXXXX); trap 'git -C '$vm_repo' worktree remove --force \"\$tmp\" >/dev/null 2>&1 || rm -rf \"\$tmp\"' EXIT; git -C '$vm_repo' worktree add --detach \"\$tmp\" '$commit' >/dev/null; docker build --pull=false --label org.opencontainers.image.revision='$commit' --label com.metahumotonic.source-archive-sha256='$source_sha' -t '$name' \"\$tmp\" >/dev/null; test \"\$(docker image inspect '$name' --format '{{index .Config.Labels \"org.opencontainers.image.revision\"}}')\" = '$commit'" \
  || fail 'VM100 exact image build verification failed'
vm_id="$(ssh -o BatchMode=yes "$vm" "docker image inspect '$name' --format '{{.Id}}'")"
ssh -o BatchMode=yes "$vm" "docker save '$name'" | ssh -o BatchMode=yes "$data" "docker load >/dev/null"
data_id="$(ssh -o BatchMode=yes "$data" "docker image inspect '$vm_id' --format '{{.Id}}' 2>/dev/null")"
[[ "$data_id" == "$vm_id" ]] || fail 'data-01 image ID mismatch'
ssh -o BatchMode=yes "$data" "test \"\$(docker image inspect '$data_id' --format '{{index .Config.Labels \"org.opencontainers.image.revision\"}}')\" = '$commit'; test \"\$(docker image inspect '$data_id' --format '{{index .Config.Labels \"com.metahumotonic.source-archive-sha256\"}}')\" = '$source_sha'"
printf '{"schema":"metahumotonic/platform-image-stage@1","mode":"stage","commit":"%s","image":"%s","sourceArchiveSha256":"%s","databaseWrites":false,"publicIngressChanged":false}\n' "$commit" "$data_id" "$source_sha"
