#!/usr/bin/env bash
#
# Build on dev-01, run on runtime-01.
#
# The split: dev-01 (LXC 307) is where code is written and built; runtime-01
# (LXC 308) only runs an artifact someone else produced. It has no repo and no
# build step — if it cannot run the tarball, the tarball is wrong.
#
# No docker, on purpose. The LXC *is* the container; a second container runtime
# inside it would buy nothing. The artifact is a content-addressed tarball and
# systemd is the supervisor.
#
# Least privilege: this talks to runtime-01 as the "deploy" user over ssh. It
# does NOT touch the Proxmox host. Deploying should not require hypervisor root.
#
#   ops/deploy-runtime-01.sh              build, ship, activate, verify
#   ops/deploy-runtime-01.sh --rollback   re-point at the previous release
#   ops/deploy-runtime-01.sh --status
#
# NOT the production path. Production is VM100 and this script never touches
# it — see ops/release-web-back-vm100.sh.
set -euo pipefail

TARGET="${MHB_RUNTIME_TARGET:-deploy@192.168.0.33}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

rt()  { ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "$@"; }
say() { printf '  %s\n' "$*"; }

probe() { rt "curl -s --max-time 8 http://127.0.0.1:8000$1" 2>/dev/null || true; }

case "${1:-deploy}" in
  --status)
    say "active:   $(rt 'readlink -f /srv/mhb/current 2>/dev/null || echo none')"
    say "previous: $(rt 'readlink -f /srv/mhb/previous 2>/dev/null || echo none')"
    say "unit:     $(rt 'sudo -n systemctl is-active mhb-ts.service' 2>/dev/null || echo inactive)"
    say "releases: $(rt 'ls -1 /srv/mhb/releases 2>/dev/null | wc -l') on disk"
    say "health:   $(probe /health)"
    exit 0
    ;;
  --rollback)
    # A true undo, and repeatable: `current` and `previous` swap.
    #
    # The first version picked "second newest by mtime", which is NOT an undo.
    # Running it twice stayed put instead of rolling forward, and after a fresh
    # deploy it would have jumped to whatever happened to be second on disk
    # rather than to what was actually running. Found by testing rollback
    # before needing it, which is the only time it is cheap to find.
    cur="$(rt 'readlink -f /srv/mhb/current 2>/dev/null || true')"
    prev="$(rt 'readlink -f /srv/mhb/previous 2>/dev/null || true')"
    [[ -n "$prev" ]] || { echo "FAIL no recorded previous release — nothing to swap to" >&2; exit 1; }
    [[ "$prev" != "$cur" ]] || { echo "FAIL previous == current; refusing a no-op" >&2; exit 1; }
    say "swapping: $(basename "$cur") -> $(basename "$prev")"
    rt "ln -sfn '$cur' /srv/mhb/previous.new && ln -sfn '$prev' /srv/mhb/current && mv -T /srv/mhb/previous.new /srv/mhb/previous"
    rt "sudo -n systemctl restart mhb-ts.service"
    ;;
  deploy)
    # 1. The gates decide whether this ships, not the operator's mood.
    say "verifying (harness)…"
    ./verify >/tmp/deploy-verify.log 2>&1 \
      || { echo "FAIL harness reported RED — not deploying"; tail -20 /tmp/deploy-verify.log; exit 1; }
    say "harness: no RED gates"

    # 2. Build here. runtime-01 has no compiler.
    say "building…"
    ( cd ts && npm ci --silent --no-fund --no-audit && npm run build --silent )

    # 3. Content-addressed: the release name IS the hash of its contents, so
    #    "which build is running" is answerable without trusting a label.
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
        -cf "$tmp/app.tar" ts/dist ts/package.json ts/package-lock.json
    sha="$(sha256sum "$tmp/app.tar" | cut -c1-16)"
    rel="$(git rev-parse --short HEAD)-${sha}"
    say "artifact: $rel  ($(du -h "$tmp/app.tar" | cut -f1))"

    # 4. Ship beside the others; nothing existing is overwritten.
    rt "install -d /srv/mhb/releases/$rel"
    scp -q -o BatchMode=yes "$tmp/app.tar" "$TARGET:/tmp/app-$rel.tar"
    rt "tar -xf /tmp/app-$rel.tar -C /srv/mhb/releases/$rel && rm -f /tmp/app-$rel.tar"

    say "installing runtime deps…"
    rt "cd /srv/mhb/releases/$rel/ts && npm ci --omit=dev --silent --no-fund --no-audit >/dev/null 2>&1"

    # 5. Activate. `previous` records what was running so --rollback is a real
    #    undo rather than a guess based on file timestamps.
    rt "if [ -e /srv/mhb/current ]; then ln -sfn \"\$(readlink -f /srv/mhb/current)\" /srv/mhb/previous.new && mv -T /srv/mhb/previous.new /srv/mhb/previous; fi"
    rt "ln -sfn /srv/mhb/releases/$rel /srv/mhb/current"
    rt "sudo -n systemctl enable mhb-ts.service >/dev/null 2>&1 || true; sudo -n systemctl restart mhb-ts.service"
    ;;
  *) echo "usage: $0 [--status|--rollback]" >&2; exit 2 ;;
esac

# 6. Read back from outside. A deploy nobody verified did not happen.
say "waiting for readiness…"
for _ in $(seq 1 40); do
  rt 'curl -sf --max-time 2 -o /dev/null http://127.0.0.1:8000/health' >/dev/null 2>&1 && break
  sleep 0.5
done
health="$(probe /health)"; ready="$(probe /ready)"
say "health: ${health:-<no answer>}"
say "ready:  ${ready:-<no answer>}"

grep -q '"status":"ok"'    <<<"$health" || { echo "FAIL /health did not report ok" >&2; exit 1; }
grep -q '"status":"ready"' <<<"$ready"  || { echo "FAIL /ready did not report ready" >&2; exit 1; }
say "active: $(rt 'readlink -f /srv/mhb/current')"
say "OK"
