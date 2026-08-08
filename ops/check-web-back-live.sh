#!/usr/bin/env bash
# Read-only, fail-closed verification of the actual VM100 backend path.
set -euo pipefail

PROXMOX_HOST="${MHB_PROXMOX_HOST:-metahumo}"
VM_ID="${MHB_VM_ID:-100}"
EXPECTED_NODE="${MHB_EXPECTED_NODE:-cpu-edge-01}"
EXPECTED_IP="${MHB_EXPECTED_IP:-192.168.0.24}"
PUBLIC_ORIGIN="${MHB_PUBLIC_ORIGIN:-https://metahumotonic.com}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'PASS %s\n' "$*"
}

for required_command in ssh curl python3; do
  command -v "$required_command" >/dev/null || fail "missing command: ${required_command}"
done

[[ "$PROXMOX_HOST" =~ ^[A-Za-z0-9._-]+$ ]] || fail "unsafe Proxmox host alias"
[[ "$VM_ID" =~ ^[0-9]+$ ]] || fail "VM id must be numeric"
[[ "$EXPECTED_NODE" =~ ^[A-Za-z0-9._-]+$ ]] || fail "unsafe expected node"
[[ "$EXPECTED_IP" =~ ^[0-9.]+$ ]] || fail "unsafe expected IP"
[[ "$PUBLIC_ORIGIN" =~ ^https?://[^/]+/?$ ]] || fail "public origin must be an HTTP(S) origin"
PUBLIC_ORIGIN="${PUBLIC_ORIGIN%/}"

PROJECT_VERSION="$(python3 -c '
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
project = re.search(r"(?ms)^\[project\]\s*$.*?(?=^\[|\Z)", text)
version = re.search(r"(?m)^version\s*=\s*\"([^\"]+)\"\s*$", project.group(0) if project else "")
if not version:
    raise SystemExit("project.version not found")
print(version.group(1))
' "$REPO_ROOT/pyproject.toml")"
EXPECTED_IMAGE="metahumotonic-web-back:${PROJECT_VERSION}-x86"

tmpdir="$(mktemp -d)"
trap 'rm -rf -- "$tmpdir"' EXIT

guest_exec() {
  local raw parsed
  if ! raw="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$PROXMOX_HOST" \
      qm guest exec "$VM_ID" -- "$@")"; then
    fail "cannot reach Proxmox guest ${VM_ID} through ${PROXMOX_HOST}"
  fi

  if ! parsed="$(printf '%s' "$raw" | python3 -c '
import json, sys

try:
    payload = json.load(sys.stdin)
except Exception as exc:
    print(f"invalid qm guest-exec JSON: {exc}", file=sys.stderr)
    raise SystemExit(70)

if payload.get("exited") not in (1, True):
    print("guest command has not exited", file=sys.stderr)
    raise SystemExit(70)

exitcode = payload.get("exitcode")
if exitcode != 0:
    detail = payload.get("err-data") or payload.get("out-data") or ""
    if detail:
        print(detail, file=sys.stderr, end="" if detail.endswith("\\n") else "\\n")
    raise SystemExit(exitcode if isinstance(exitcode, int) and 1 <= exitcode <= 125 else 70)

sys.stdout.write(payload.get("out-data") or "")
')"; then
    fail "guest command failed: $*"
  fi
  printf '%s' "$parsed"
}

context="$(guest_exec kubectl config current-context)"
[[ "$context" == "default" ]] || fail "VM100 kubectl context is ${context:-unset}, expected default"
pass "kubectl context default inside VM${VM_ID}"

node_json="$(guest_exec kubectl get node "$EXPECTED_NODE" -o json)"
printf '%s' "$node_json" | python3 -c '
import json, sys

node, expected_name, expected_ip = json.load(sys.stdin), sys.argv[1], sys.argv[2]
assert node["metadata"]["name"] == expected_name
ready = {item["type"]: item["status"] for item in node["status"]["conditions"]}
assert ready.get("Ready") == "True", ready
addresses = {item["address"] for item in node["status"]["addresses"]}
assert expected_ip in addresses, addresses
' "$EXPECTED_NODE" "$EXPECTED_IP" || fail "unexpected or unready k3s control-plane node"
pass "node ${EXPECTED_NODE} is Ready at ${EXPECTED_IP}"

service_json="$(guest_exec kubectl -n infra get service web-back -o json)"
printf '%s' "$service_json" | python3 -c '
import json, sys

service = json.load(sys.stdin)
assert not service["spec"].get("selector"), service["spec"].get("selector")
ports = service["spec"].get("ports", [])
assert any(item.get("port") == 8000 for item in ports), ports
' || fail "infra/web-back is not the expected selectorless service"
pass "selectorless service infra/web-back"

slices_json="$(guest_exec kubectl -n infra get endpointslice \
  -l kubernetes.io/service-name=web-back -o json)"
printf '%s' "$slices_json" | python3 -c '
import json, sys

document, expected_ip = json.load(sys.stdin), sys.argv[1]
actual = set()
for item in document.get("items", []):
    name = item["metadata"]["name"]
    for endpoint in item.get("endpoints", []):
        ready = endpoint.get("conditions", {}).get("ready") is True
        for address in endpoint.get("addresses", []):
            for port in item.get("ports", []):
                actual.add((name, address, port.get("port"), ready))
expected = {
    ("web-back-pve-1", expected_ip, 18210, True),
    ("web-back-pve-2", expected_ip, 18211, True),
}
assert actual == expected, {"expected": sorted(expected), "actual": sorted(actual)}
' "$EXPECTED_IP" || fail "EndpointSlice topology drift"
pass "EndpointSlices point to ${EXPECTED_IP}:18210 and :18211"

for ingress_name in web-back-api web-back-api-tls; do
  ingress_json="$(guest_exec kubectl -n infra get ingressroute "$ingress_name" -o json)"
  printf '%s' "$ingress_json" | python3 -c '
import json, sys

document = json.load(sys.stdin)
services = [
    (service.get("name"), service.get("port"))
    for route in document.get("spec", {}).get("routes", [])
    for service in route.get("services", [])
]
assert ("web-back", 8000) in services, services
' || fail "IngressRoute ${ingress_name} does not target web-back:8000"
done
pass "HTTP and HTTPS IngressRoutes target web-back:8000"

containers_json="$(guest_exec docker inspect web-back-pve-1 web-back-pve-2)"
container_image_id="$(printf '%s' "$containers_json" | python3 -c '
import json, sys

containers, expected_image = json.load(sys.stdin), sys.argv[1]
expected_ports = {"web-back-pve-1": "18210", "web-back-pve-2": "18211"}
assert {item["Name"].lstrip("/") for item in containers} == set(expected_ports)
image_ids = {item.get("Image") for item in containers}
assert len(image_ids) == 1 and None not in image_ids, image_ids
image_id = next(iter(image_ids))
assert image_id.startswith("sha256:"), image_id
for item in containers:
    name = item["Name"].lstrip("/")
    assert item["Config"]["Image"] == expected_image, (name, item["Config"]["Image"])
    assert item["State"]["Health"]["Status"] == "healthy", (name, item["State"])
    assert item["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped", name
    bindings = item["HostConfig"]["PortBindings"]["8000/tcp"]
    assert any(binding["HostPort"] == expected_ports[name] for binding in bindings), bindings
print(image_id)
' "$EXPECTED_IMAGE" || fail "Docker replica image, health, restart policy, or port drift"
)"

image_json="$(guest_exec docker image inspect "$EXPECTED_IMAGE")"
printf '%s' "$image_json" | python3 -c '
import json, sys

images, expected_id = json.load(sys.stdin), sys.argv[1]
assert len(images) == 1, len(images)
image = images[0]
assert image.get("Id") == expected_id, (image.get("Id"), expected_id)
' "$container_image_id" || fail "local image tag and running container IDs disagree"
pass "both Docker replicas are healthy on ${EXPECTED_IMAGE} at ${container_image_id}"

for port in 18210 18211; do
  health_json="$(guest_exec curl -fsS --max-time 10 "http://127.0.0.1:${port}/health")"
  printf '%s' "$health_json" | python3 -c '
import json, sys
body, expected = json.load(sys.stdin), sys.argv[1]
assert body.get("status") == "ok", body
assert body.get("version") == expected, body
' "$PROJECT_VERSION" || fail "direct health contract failed on port ${port}"

  ready_json="$(guest_exec curl -fsS --max-time 10 "http://127.0.0.1:${port}/ready")"
  printf '%s' "$ready_json" | python3 -c '
import json, sys
body = json.load(sys.stdin)
assert body.get("status") == "ready", body
assert body.get("kg_live") is True, body
assert body.get("degraded") is False, body
' || fail "direct readiness contract failed on port ${port}"
done
pass "both direct health/readiness surfaces are live"

summary_body="$tmpdir/research-summary.json"
summary_meta="$(curl -sS --max-time 15 -o "$summary_body" \
  -w '%{http_code}|%{content_type}' "$PUBLIC_ORIGIN/api/research/summary")"
[[ "${summary_meta%%|*}" == "200" ]] || fail "public research summary returned ${summary_meta%%|*}"
[[ "${summary_meta#*|}" == application/json* ]] || fail "public research summary content type is ${summary_meta#*|}"
python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    body = json.load(stream)
assert body.get("source") == "live", body.get("source")
' "$summary_body" || fail "public research summary is not backed by live KG data"
pass "public research summary is 200 JSON with source=live"

for private_path in health ready; do
  status="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' \
    "$PUBLIC_ORIGIN/$private_path")"
  [[ "$status" == "404" ]] || fail "public /${private_path} returned ${status}, expected private-only 404"
done
pass "public /health and /ready remain private-only (404)"

pass "VM100 backend topology and live surface verified"
