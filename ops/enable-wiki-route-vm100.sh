#!/usr/bin/env bash
# Safely inspect, enable, or disable the explicit community-wiki prefix.
set -Eeuo pipefail

RUNTIME_HOST="${MHB_RUNTIME_HOST:-metahumotonic27@192.168.0.24}"
NAMESPACE="${MHB_KUBE_NAMESPACE:-infra}"
WIKI_MATCH='PathPrefix(`/api/wiki`)'
ACTION="${1:---enable}"
ROUTE_NONCE="${MHB_WIKI_ROUTE_NONCE:-}"
ROUTE_RECEIPT_ROOT="${MHB_WIKI_ROUTE_RECEIPT_ROOT:-/var/lib/metahumotonic-web-back/route-transactions}"

fail() {
  printf 'FAIL %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'PASS %s\n' "$*"
}

[[ "$RUNTIME_HOST" =~ ^[A-Za-z0-9._@-]+$ ]] || fail "unsafe runtime host"
[[ "$NAMESPACE" =~ ^[A-Za-z0-9._-]+$ ]] || fail "unsafe namespace"
[[ "$ACTION" == --enable || "$ACTION" == --disable || "$ACTION" == --status ]] \
  || fail "usage: $0 [--status|--enable|--disable]"
if [[ "$ACTION" != --status ]]; then [[ "$ROUTE_NONCE" =~ ^[0-9a-f]{32}$ ]] || fail "MHB_WIKI_ROUTE_NONCE must be exact 32-hex for mutation"; fi
[[ "$ROUTE_RECEIPT_ROOT" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "unsafe route receipt root"
for required_command in ssh python3; do
  command -v "$required_command" >/dev/null || fail "missing command: ${required_command}"
done

tmpdir="$(mktemp -d)"
trap 'rm -rf -- "$tmpdir"' EXIT
ingress_names=(web-back-api web-back-api-tls)
changed_names=()
route_receipt="$ROUTE_RECEIPT_ROOT/${ROUTE_NONCE}.json"

kube() {
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$RUNTIME_HOST" \
    sudo -n kubectl -n "$NAMESPACE" "$@"
}

kube_patch() {
  local name="$1" patch_file="$2"
  # Patch JSON contains Traefik backticks. Send it over stdin so no part of the
  # document is re-parsed by the remote login shell.
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$RUNTIME_HOST" \
    sudo -n kubectl -n "$NAMESPACE" patch ingressroute "$name" \
      --type=json --patch-file=/dev/stdin <"$patch_file"
}

rollback() {
  local status=$? name rollback_ok=true
  trap - ERR
  set +e
  for ((index=${#changed_names[@]}-1; index>=0; index--)); do
    name="${changed_names[index]}"
    kube_patch "$name" "$tmpdir/${name}.rollback.json" >/dev/null \
      || rollback_ok=false
  done
  if [[ "$rollback_ok" == true ]]; then
    if declare -F route_receipt_status >/dev/null && ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n test -f "$route_receipt"; then
      route_receipt_status ROLLED_BACK || rollback_ok=false
    fi
    printf 'FAIL route activation failed; exact-match rollback completed\n' >&2
  else
    printf 'FAIL route activation and exact-match rollback both failed; operator action required\n' >&2
  fi
  exit "$status"
}
trap rollback ERR

# Fetch and validate both resources before mutating either one. JSON Patch's
# `test` operation then rejects any concurrent match-string change.
for name in "${ingress_names[@]}"; do
  kube get ingressroute "$name" -o json >"$tmpdir/${name}.json"
  python3 - "$tmpdir/${name}.json" "$tmpdir/${name}.patch.json" \
    "$tmpdir/${name}.rollback.json" "$WIKI_MATCH" "$ACTION" <<'PY'
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
patch_path = pathlib.Path(sys.argv[2])
rollback_path = pathlib.Path(sys.argv[3])
wiki_match = sys.argv[4]
action = sys.argv[5]
document = json.loads(source.read_text(encoding="utf-8"))
routes = document.get("spec", {}).get("routes", [])
if not routes:
    raise SystemExit("IngressRoute has no spec.routes[0]")
route = routes[0]
services = {(item.get("name"), item.get("port")) for item in route.get("services", [])}
if ("web-back", 8000) not in services:
    raise SystemExit("spec.routes[0] does not target web-back:8000")
match = route.get("match")
if not isinstance(match, str):
    raise SystemExit("spec.routes[0].match is not a string")
wiki_count = match.count(wiki_match)
if wiki_count > 1:
    raise SystemExit("wiki PathPrefix is duplicated")

mcp_match = 'PathPrefix(`/api/mcp`)'
if match.count(mcp_match) != 1:
    raise SystemExit("expected exactly one MCP anchor in spec.routes[0].match")
if action == "--enable":
    new_match = match if wiki_count else match.replace(
        mcp_match, f"{mcp_match} || {wiki_match}", 1
    )
elif action == "--disable":
    new_match = match.replace(f" || {wiki_match}", "", 1) if wiki_count else match
else:
    new_match = match

patch = []
rollback = []
if new_match != match:
    patch = [
        {"op": "test", "path": "/spec/routes/0/match", "value": match},
        {"op": "replace", "path": "/spec/routes/0/match", "value": new_match},
    ]
    rollback = [
        {"op": "test", "path": "/spec/routes/0/match", "value": new_match},
        {"op": "replace", "path": "/spec/routes/0/match", "value": match},
    ]
patch_path.write_text(json.dumps(patch, separators=(",", ":")), encoding="utf-8")
rollback_path.write_text(json.dumps(rollback, separators=(",", ":")), encoding="utf-8")
PY
done

route_receipt_status() {
  local phase="$1"
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$RUNTIME_HOST" sudo -n python3 - \
    "$route_receipt" "$ROUTE_NONCE" "$phase" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); b=json.loads(p.read_text()); assert b["rollout_nonce"]==sys.argv[2]
b["phase"]=sys.argv[3]; t=p.with_name(p.name+f'.tmp.{os.getpid()}'); t.write_text(json.dumps(b,sort_keys=True)+"\n"); os.chmod(t,0o600); os.chown(t,0,0); t.replace(p)
PY
}

if [[ "$ACTION" == --status ]]; then
  states=()
  for name in "${ingress_names[@]}"; do
    state="$(python3 - "$tmpdir/${name}.json" "$WIKI_MATCH" <<'PY'
import json, pathlib, sys
body=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
match=body["spec"]["routes"][0]["match"]
print("enabled" if match.count(sys.argv[2]) == 1 else "disabled")
PY
)"
    states+=("$state")
  done
  [[ "${states[0]}" == "${states[1]}" ]] || fail "wiki route state is mixed across HTTP and HTTPS"
  trap - ERR
  printf '%s\n' "${states[0]}"
  exit 0
fi

# Publish the exact two-resource transaction before the first public mutation.
python3 - "$tmpdir/web-back-api.json" "$tmpdir/web-back-api-tls.json" "$tmpdir/route-receipt.json" "$ROUTE_NONCE" "$ACTION" "$WIKI_MATCH" <<'PY'
import hashlib,json,pathlib,sys
resources={}
for raw in sys.argv[1:3]:
 b=json.loads(pathlib.Path(raw).read_text()); name=b["metadata"]["name"]; match=b["spec"]["routes"][0]["match"]
 if sys.argv[5]=="--enable": target=match if sys.argv[6] in match else match.replace('PathPrefix(`/api/mcp`)',f'PathPrefix(`/api/mcp`) || {sys.argv[6]}',1)
 else: target=match.replace(f' || {sys.argv[6]}','',1)
 resources[name]={"resource_version":b["metadata"]["resourceVersion"],"prior_match":match,"prior_sha256":hashlib.sha256(match.encode()).hexdigest(),"target_match":target,"target_sha256":hashlib.sha256(target.encode()).hexdigest()}
out={"schema":"metahumotonic/wiki-route-transaction@1","rollout_nonce":sys.argv[4],"action":sys.argv[5],"phase":"RESERVED","resources":resources}
pathlib.Path(sys.argv[3]).write_text(json.dumps(out,sort_keys=True)+"\n")
PY
if ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n test -f "$route_receipt"; then
  ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n cat "$route_receipt" >"$tmpdir/existing-route-receipt.json"
  python3 - "$tmpdir/existing-route-receipt.json" "$tmpdir/web-back-api.json" "$tmpdir/web-back-api-tls.json" "$ROUTE_NONCE" "$ACTION" <<'PY'
import json,pathlib,sys
r=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert r["schema"]=="metahumotonic/wiki-route-transaction@1" and r["rollout_nonce"]==sys.argv[4] and r["action"]==sys.argv[5]
for raw in sys.argv[2:4]:
 b=json.loads(pathlib.Path(raw).read_text()); name=b["metadata"]["name"]; match=b["spec"]["routes"][0]["match"]; x=r["resources"][name]
 assert match in {x["prior_match"],x["target_match"]}
PY
else
  ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n install -d -m 700 -o root -g root "$ROUTE_RECEIPT_ROOT"
  ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n tee "$route_receipt" <"$tmpdir/route-receipt.json" >/dev/null
  ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n chown root:root "$route_receipt"
  ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n chmod 600 "$route_receipt"
fi

for name in "${ingress_names[@]}"; do
  patch_json="$(<"$tmpdir/${name}.patch.json")"
  if [[ "$patch_json" == '[]' ]]; then
    if [[ "$ACTION" == --enable ]]; then
      pass "${NAMESPACE}/${name} already contains exact wiki prefix"
    else
      pass "${NAMESPACE}/${name} already in requested disable state"
    fi
    continue
  fi
  kube_patch "$name" "$tmpdir/${name}.patch.json" >/dev/null
  changed_names+=("$name")
  route_receipt_status "PATCHED_${name}"
  pass "${NAMESPACE}/${name} patched"
done

for name in "${ingress_names[@]}"; do
  kube get ingressroute "$name" -o json >"$tmpdir/${name}.readback.json"
  python3 - "$tmpdir/${name}.readback.json" "$WIKI_MATCH" "$ACTION" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
wiki_match = sys.argv[2]
route = document.get("spec", {}).get("routes", [None])[0]
if not isinstance(route, dict):
    raise SystemExit("missing route[0]")
match = route.get("match", "")
expected = sys.argv[3]
if (match.count(wiki_match) == 1) != (expected == "--enable"):
    raise SystemExit("wiki PathPrefix exact-readback failed")
services = {(item.get("name"), item.get("port")) for item in route.get("services", [])}
if ("web-back", 8000) not in services:
    raise SystemExit("web-back target exact-readback failed")
PY
done

trap - ERR
route_receipt_status DONE
pass "HTTP and HTTPS IngressRoutes are in requested ${ACTION#--} state"
