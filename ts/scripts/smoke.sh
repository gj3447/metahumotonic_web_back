#!/usr/bin/env bash
#
# Start the REAL compiled server and check every route answers.
#
# This exists because the unit tests build their own composition root and
# therefore cannot catch a mis-wired production Layer. On 2026-08-10 a
# misplaced `Layer.provide` made `/health` answer 200 while every prefixed
# route 404'd; 117 tests stayed green and only starting `main.ts` revealed it.
#
# No infrastructure required: the service degrades to its snapshot fallback.
set -euo pipefail
# Preserve the shared runtime fix, and explicitly target loopback below.
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

PORT="${SMOKE_PORT:-9101}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[[ -f dist/src/main.js ]] || { echo "smoke: dist missing — run npm run build" >&2; exit 1; }

[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || { echo "smoke: invalid port" >&2; exit 1; }
WORK="$(mktemp -d)"
LOG="$WORK/server.log"
RUN_MARKER="smoke-$$-$(date +%s)"
# Test only our loopback process; the package proxy is not the tested server.
probe() { command curl --noproxy 127.0.0.1 "$@"; }
# Never inherit production DB connections, credentials or feature opt-ins.
env -i PATH="$PATH" MHB_HOST=127.0.0.1 MHB_PORT="$PORT" MHB_VERSION="$RUN_MARKER" \
  MHB_LOG_JSON=false MHB_FEEDBACK_ADMIN_KEY="smoke-key-at-least-32-bytes-long!!!!" \
  node dist/src/main.js > "$LOG" 2>&1 &
SRV=$!
cleanup() {
  kill "$SRV" 2>/dev/null || true
  wait "$SRV" 2>/dev/null || true
  rm -rf -- "$WORK"
}
trap cleanup EXIT
ready=0
for _ in $(seq 1 60); do
  kill -0 "$SRV" 2>/dev/null || break
  if probe -sf --max-time 2 "http://127.0.0.1:$PORT/health" > "$WORK/health.json" &&
    python3 -c 'import json,sys; b=json.load(open(sys.argv[1])); sys.exit(0 if b.get("status")=="ok" and b.get("version")==sys.argv[2] else 1)' "$WORK/health.json" "$RUN_MARKER" 2>/dev/null; then
    ready=1; break
  fi
  sleep 0.2
done
if [[ "$ready" != 1 ]]; then
  echo "smoke: own server did not become ready" >&2; tail -40 "$LOG" >&2; exit 1
fi

fail=0
check() { # check <expected> <path> [curl args...]
  local want="$1" path="$2"; shift 2
  local got
  got="$(probe -s --max-time 10 -o /dev/null -w '%{http_code}' "$@" "http://127.0.0.1:$PORT$path")"
  if [[ "$got" == "$want" ]]; then
    printf '  ok    %-46s %s\n' "$path" "$got"
  else
    printf '  FAIL  %-46s got %s want %s\n' "$path" "$got" "$want"; fail=1
  fi
}

echo "smoke: routes"
check 200 /health
check 200 /ready
check 200 /
check 200 /docs
check 200 /api/stats
check 200 /api/domains
check 200 /api/skills
for s in summary findings lessons papers consensus recent agent; do check 200 "/api/research/$s"; done
check 200 "/api/research/neighbors?name=anything"
check 200 "/api/agent/walk?seed=anything"
check 200 "/api/agent/plan?seed=anything"
check 200 "/api/agent/explore?seed=anything"

echo "smoke: contract"
check 422 "/api/research/findings?limit=99999"
check 422 "/api/research/findings?limit=0"
check 422 "/api/research/findings?offset=10001"
check 200 "/api/research/findings?limit=100&offset=10000"
check 422 "/api/research/neighbors"
check 401 /internal/feedback
check 200 /internal/feedback -H "X-API-Key: smoke-key-at-least-32-bytes-long!!!!"
check 503 /api/kg/read -X POST -H 'Content-Type: application/json' -d '{"query":"RETURN 1"}'

echo "smoke: /ready shape (ops/check-web-back-live.sh reads these)"
probe -s --max-time 10 "http://127.0.0.1:$PORT/ready" | python3 -c '
import json, sys
body = json.load(sys.stdin)
required = {"status", "kg_live", "wiki_required", "wiki_live", "wiki_store_live",
            "wiki_rate_limit_live", "degraded"}
missing = required - set(body)
assert not missing, f"missing /ready keys: {sorted(missing)}"
assert body["status"] in ("ready", "not_ready"), body
print("  ok    /ready carries all 7 keys")
' || fail=1

echo "smoke: / endpoints array"
probe -s --max-time 10 "http://127.0.0.1:$PORT/" | python3 -c '
import json, sys
body = json.load(sys.stdin)
eps = body.get("endpoints") or []
assert len(eps) >= 7, body
print("  ok    / lists " + str(len(eps)) + " endpoints")
' || fail=1

echo "smoke: CORS on the real compiled socket"
for origin in https://metahumotonic.com https://untrusted.example; do
  probe -s --max-time 5 -D "$WORK/headers" -o /dev/null \
    -X OPTIONS -H "Origin: $origin" -H "Access-Control-Request-Method: POST" \
    -H "Access-Control-Request-Headers: content-type,x-api-key" \
    "http://127.0.0.1:$PORT/api/kg/read"
  python3 - "$WORK/headers" "$origin" <<'PY_CORS' || fail=1
import sys
lines=open(sys.argv[1]).read().splitlines()
headers=dict(line.split(':',1) for line in lines if ':' in line)
headers={k.strip().lower():v.strip() for k,v in headers.items()}
origin=sys.argv[2]
if origin=='https://metahumotonic.com':
    assert headers.get('access-control-allow-origin')==origin
    assert 'x-api-key' in headers.get('access-control-allow-headers','').lower()
else:
    assert 'access-control-allow-origin' not in headers
print('  ok    origin-specific CORS',origin)
PY_CORS
done

if [[ "$fail" != 0 ]]; then
  echo "smoke: FAILED — server log follows" >&2
  tail -40 "$LOG" >&2
  exit 1
fi
echo "smoke: all checks passed"
