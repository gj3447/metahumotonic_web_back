#!/usr/bin/env bash
# Read-only, fail-closed verification of the actual VM100 backend path.
set -euo pipefail

if [[ -n "${PYTHONOPTIMIZE:-}" ]]; then
  printf 'FAIL PYTHONOPTIMIZE must be unset because checker safety gates require Python assertions\n' >&2
  exit 1
fi

PROXMOX_HOST="${MHB_PROXMOX_HOST:-metahumo}"
VM_ID="${MHB_VM_ID:-100}"
EXPECTED_NODE="${MHB_EXPECTED_NODE:-cpu-edge-01}"
EXPECTED_IP="${MHB_EXPECTED_IP:-192.168.0.24}"
PUBLIC_ORIGIN="${MHB_PUBLIC_ORIGIN:-https://metahumotonic.com}"
DATA_HOST="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPECTED_COMMIT=""
RECEIPT_MODE=final
while (($#)); do
  case "$1" in
    --expected-commit) EXPECTED_COMMIT="${2:-}"; shift 2 ;;
    --candidate) RECEIPT_MODE=candidate; shift ;;
    *) printf 'FAIL usage: %s --expected-commit EXACT_40_HEX [--candidate]\n' "$0" >&2; exit 1 ;;
  esac
done

fail() {
  printf 'FAIL %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'PASS %s\n' "$*"
}

for required_command in ssh curl python3 git; do
  command -v "$required_command" >/dev/null || fail "missing command: ${required_command}"
done
python3 -c 'import sys; raise SystemExit(0 if sys.flags.optimize == 0 else 1)' \
  || fail "python3 optimization must be disabled because checker safety gates require assertions"

[[ "$PROXMOX_HOST" =~ ^[A-Za-z0-9._-]+$ ]] || fail "unsafe Proxmox host alias"
[[ "$VM_ID" =~ ^[0-9]+$ ]] || fail "VM id must be numeric"
[[ "$EXPECTED_NODE" =~ ^[A-Za-z0-9._-]+$ ]] || fail "unsafe expected node"
[[ "$EXPECTED_IP" =~ ^[0-9.]+$ ]] || fail "unsafe expected IP"
[[ "$PUBLIC_ORIGIN" =~ ^https?://[^/]+/?$ ]] || fail "public origin must be an HTTP(S) origin"
[[ "$DATA_HOST" =~ ^[A-Za-z0-9._@-]+$ ]] || fail "unsafe data host"
[[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "--expected-commit must be exact 40-hex commit"
PUBLIC_ORIGIN="${PUBLIC_ORIGIN%/}"

PROJECT_VERSION="$(python3 -c '
import pathlib, re, sys
text = sys.stdin.read()
project = re.search(r"(?ms)^\[project\]\s*$.*?(?=^\[|\Z)", text)
version = re.search(r"(?m)^version\s*=\s*\"([^\"]+)\"\s*$", project.group(0) if project else "")
if not version:
    raise SystemExit("project.version not found")
print(version.group(1))
' <<<"$(git -C "$REPO_ROOT" show "$EXPECTED_COMMIT:pyproject.toml")")"
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
routes = document.get("spec", {}).get("routes", [])
assert routes, routes
route = routes[0]
services = [(service.get("name"), service.get("port")) for service in route.get("services", [])]
assert ("web-back", 8000) in services, services
match = route.get("match", "")
assert match.count("PathPrefix(`/api/wiki`)") == 1, match
' || fail "IngressRoute ${ingress_name} does not target web-back:8000"
done
pass "HTTP and HTTPS IngressRoutes target web-back:8000 with exact /api/wiki prefix"

containers_json="$(guest_exec docker inspect web-back-pve-1 web-back-pve-2)"
container_image_id="$(printf '%s' "$containers_json" | python3 -c '
import json, sys

containers, expected_image, expected_commit = json.load(sys.stdin), sys.argv[1], sys.argv[2]
expected_ports = {"web-back-pve-1": "18210", "web-back-pve-2": "18211"}
assert {item["Name"].lstrip("/") for item in containers} == set(expected_ports)
image_ids = {item.get("Image") for item in containers}
assert len(image_ids) == 1 and None not in image_ids, image_ids
image_id = next(iter(image_ids))
assert image_id.startswith("sha256:"), image_id
for item in containers:
    name = item["Name"].lstrip("/")
    assert item["Config"]["Image"] == expected_image, (name, item["Config"]["Image"])
    labels=item["Config"].get("Labels") or {}
    assert labels.get("org.opencontainers.image.revision") == expected_commit, (name, labels)
    assert len(labels.get("com.metahumotonic.source-archive-sha256", "")) == 64, (name, labels)
    assert len(labels.get("com.metahumotonic.wiki-migrations-sha256", "")) == 64, (name, labels)
    assert item["State"]["Health"]["Status"] == "healthy", (name, item["State"])
    assert item["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped", name
    bindings = item["HostConfig"]["PortBindings"]["8000/tcp"]
    assert len(bindings) == 1, bindings
    assert bindings[0]["HostPort"] == expected_ports[name], bindings
    assert bindings[0]["HostIp"] == "0.0.0.0", bindings
print(image_id)
' "$EXPECTED_IMAGE" "$EXPECTED_COMMIT" || fail "Docker replica commit/image, health, restart policy, or port drift"
)"

image_json="$(guest_exec docker image inspect "$EXPECTED_IMAGE")"
IFS='|' read -r verified_image_id image_archive_sha image_migrations_sha < <(printf '%s' "$image_json" | python3 -c '
import json, sys

images, expected_id, expected_commit = json.load(sys.stdin), sys.argv[1], sys.argv[2]
assert len(images) == 1, len(images)
image = images[0]
assert image.get("Id") == expected_id, (image.get("Id"), expected_id)
labels=image.get("Config",{}).get("Labels") or {}
assert labels.get("org.opencontainers.image.revision") == expected_commit
archive_sha=labels.get("com.metahumotonic.source-archive-sha256", "")
migrations_sha=labels.get("com.metahumotonic.wiki-migrations-sha256", "")
assert len(archive_sha)==64 and all(c in "0123456789abcdef" for c in archive_sha)
assert len(migrations_sha)==64 and all(c in "0123456789abcdef" for c in migrations_sha)
print("|".join((image["Id"],archive_sha,migrations_sha)))
' "$container_image_id" "$EXPECTED_COMMIT") || fail "local image tag, revision label, archive digest, migrations digest, or running container IDs disagree"
[[ "$verified_image_id" == "$container_image_id" ]] || fail "verified image ID readback mismatch"

if [[ "$RECEIPT_MODE" == candidate ]]; then
  rollout_receipt="$(guest_exec cat /var/lib/metahumotonic-web-back/releases/active-rollout.env)"
  expected_status=AWAITING_PUBLIC_READBACK
else
  active_rollout="$(guest_exec cat /var/lib/metahumotonic-web-back/releases/active-rollout.env)"
  test "$(guest_exec stat -c '%U:%G:%a' "/var/lib/metahumotonic-web-back/releases/${EXPECTED_COMMIT}/deployment-current.env")" = root:root:600 \
    || fail "deployment current pointer permissions drift"
  deployment_pointer="$(guest_exec cat "/var/lib/metahumotonic-web-back/releases/${EXPECTED_COMMIT}/deployment-current.env")"
  deployment_receipt_path="$(printf '%s\n' "$deployment_pointer" | python3 -c '
import sys
b=dict(line.split("=",1) for line in sys.stdin.read().splitlines() if "=" in line)
active=dict(line.split("=",1) for line in sys.argv[2].splitlines() if "=" in line)
assert b.get("COMMIT")==sys.argv[1]
nonce=b.get("ROLLOUT_NONCE", ""); assert len(nonce)==32 and all(c in "0123456789abcdef" for c in nonce)
path=b.get("RECEIPT"); assert path==f"/var/lib/metahumotonic-web-back/releases/{sys.argv[1]}/deployment-receipt-{nonce}.env"
assert active.get("STATUS")=="DONE" and active.get("COMMIT")==sys.argv[1] and active.get("ROLLOUT_NONCE")==nonce
print(path)
' "$EXPECTED_COMMIT" "$active_rollout")" || fail "deployment current pointer is not exact commit/nonce bound"
  test "$(guest_exec stat -c '%U:%G:%a' "$deployment_receipt_path")" = root:root:600 \
    || fail "deployment receipt permissions drift"
  rollout_receipt="$(guest_exec cat "$deployment_receipt_path")"
  expected_status=DONE
fi
printf '%s\n' "$rollout_receipt" | python3 -c '
import sys
body=dict(line.split("=",1) for line in sys.stdin.read().splitlines() if "=" in line)
active=dict(line.split("=",1) for line in sys.argv[6].splitlines() if "=" in line)
assert body.get("STATUS") == sys.argv[5]
assert body.get("COMMIT") == sys.argv[1]
assert body.get("IMAGE_ID") == sys.argv[2]
assert body.get("ARCHIVE_SHA256") == sys.argv[3]
assert body.get("MIGRATIONS_SHA256") == sys.argv[4]
rollout_nonce=body.get("ROLLOUT_NONCE", "")
assert len(rollout_nonce) == 32 and all(c in "0123456789abcdef" for c in rollout_nonce)
assert body.get("SCHEMA_GATE_RECEIPT") == f"/var/lib/metahumotonic-web-back/releases/{sys.argv[1]}/schema-gate-receipt-{rollout_nonce}.json"
assert len(body.get("SCHEMA_GATE_RECEIPT_SHA256", "")) == 64
assert len(body.get("CURRENT_BACKUP_RECEIPT_SHA256", "")) == 64
assert body.get("CURRENT_BACKUP_RECEIPT") == f"/var/lib/metahumotonic-wiki/releases/{sys.argv[1]}-{rollout_nonce}/current-backup-receipt.json"
assert body.get("PRIOR_IMAGE_ID", "").startswith("sha256:") and len(body["PRIOR_IMAGE_ID"]) == 71
assert body.get("PRIOR_RESTART_POLICY") == "unless-stopped"
assert body.get("PRIOR_HEALTH") == "healthy"
assert body.get("PRIOR_ONE_PORT") == "18210" and body.get("PRIOR_TWO_PORT") == "18211"
assert body.get("PRIOR_HOST_IP") in {"DOCKER_DEFAULT_ALL", "0.0.0.0"}
if sys.argv[5] == "DONE":
    assert active.get("STATUS") == "DONE"
    assert all(body.get(k)==v for k,v in active.items() if k != "STATUS")
' "$EXPECTED_COMMIT" "$container_image_id" "$image_archive_sha" "$image_migrations_sha" "$expected_status" "${active_rollout:-$rollout_receipt}" || fail "rollout receipt is not bound to expected commit/image/archive/migrations"
  read -r schema_receipt schema_receipt_sha < <(printf '%s\n' "$rollout_receipt" | awk -F= '$1=="SCHEMA_GATE_RECEIPT"{p=$2} $1=="SCHEMA_GATE_RECEIPT_SHA256"{s=$2} END{print p,s}')
  schema_body="$(guest_exec cat "$schema_receipt")"
  test "$(guest_exec sha256sum "$schema_receipt" | awk '{print $1}')" = "$schema_receipt_sha" || fail "schema gate receipt digest mismatch"
  printf '%s' "$schema_body" | python3 -c 'import json,sys; b=json.load(sys.stdin); assert b["commit"]==sys.argv[1] and b["migrations_sha256"]==sys.argv[2] and b["schema_check"]=="PASS"' "$EXPECTED_COMMIT" "$image_migrations_sha" || fail "schema gate receipt content mismatch"

  read -r data_receipt data_receipt_sha rollout_nonce < <(printf '%s\n' "$rollout_receipt" | awk -F= '$1=="CURRENT_BACKUP_RECEIPT"{p=$2} $1=="CURRENT_BACKUP_RECEIPT_SHA256"{s=$2} $1=="ROLLOUT_NONCE"{n=$2} END{print p,s,n}')
  [[ "$data_receipt" =~ ^/[A-Za-z0-9._/-]+$ && "$data_receipt_sha" =~ ^[0-9a-f]{64}$ && "$rollout_nonce" =~ ^[0-9a-f]{32}$ ]] || fail "unsafe data receipt binding"
  data_body="$(ssh -o BatchMode=yes "$DATA_HOST" sudo -n cat "$data_receipt")"
  test "$(ssh -o BatchMode=yes "$DATA_HOST" sudo -n sha256sum "$data_receipt" | awk '{print $1}')" = "$data_receipt_sha" || fail "data-01 backup receipt digest mismatch"
  read -r encrypted_dump key_file dump_sha key_sha < <(printf '%s' "$data_body" | python3 -c '
import json,sys
b=json.load(sys.stdin); assert b["status"]=="VERIFIED" and b["restore_drill"]=="PASS"; assert b["commit"]==sys.argv[1] and b["rollout_nonce"]==sys.argv[2]; print(b["encrypted_backup"],b["key_file"],b["backup_sha256"],b["key_sha256"])
' "$EXPECTED_COMMIT" "$rollout_nonce")
  [[ "$encrypted_dump" =~ ^/[A-Za-z0-9._/-]+$ && "$key_file" =~ ^/[A-Za-z0-9._/-]+$ && "$dump_sha" =~ ^[0-9a-f]{64}$ && "$key_sha" =~ ^[0-9a-f]{64}$ ]] || fail "unsafe backup artifact binding"
  read -r actual_dump_sha actual_key_sha < <(ssh -o BatchMode=yes "$DATA_HOST" sudo -n sh -s -- "$data_receipt" "$encrypted_dump" "$key_file" <<'REMOTE'
set -eu
for file in "$1" "$2" "$3"; do test "$(stat -c '%U:%G:%a' "$file")" = root:root:600; done
for dir in "$(dirname "$1")" "$(dirname "$3")"; do test "$(stat -c '%U:%G:%a' "$dir")" = root:root:700; done
printf '%s %s\n' "$(sha256sum "$2" | awk '{print $1}')" "$(sha256sum "$3" | awk '{print $1}')"
REMOTE
)
  [[ "$actual_dump_sha" == "$dump_sha" ]] || fail "data-01 encrypted dump digest mismatch"
  [[ "$actual_key_sha" == "$key_sha" ]] || fail "data-01 backup key digest mismatch"
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
assert body.get("wiki_required") is True, body
assert body.get("wiki_live") is True, body
assert body.get("degraded") is False, body
' || fail "direct readiness contract failed on port ${port}"

  wiki_json="$(guest_exec curl -fsS --max-time 10 "http://127.0.0.1:${port}/api/wiki/v1/pages?limit=1")"
  printf '%s' "$wiki_json" | python3 -c '
import json, sys
body = json.load(sys.stdin)
assert isinstance(body.get("items"), list), body
assert body.get("limit") == 1, body
assert body.get("offset") == 0, body
' || fail "direct wiki API contract failed on port ${port}"
done
pass "both direct health/readiness and wiki API surfaces are live"

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

wiki_body="$tmpdir/wiki-pages.json"
wiki_meta="$(curl -sS --max-time 15 -o "$wiki_body" \
  -w '%{http_code}|%{content_type}' "$PUBLIC_ORIGIN/api/wiki/v1/pages?limit=1")"
[[ "${wiki_meta%%|*}" == "200" ]] || fail "public wiki API returned ${wiki_meta%%|*}"
[[ "${wiki_meta#*|}" == application/json* ]] || fail "public wiki API content type is ${wiki_meta#*|}"
python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    body = json.load(stream)
assert isinstance(body.get("items"), list), body
assert body.get("limit") == 1, body
assert body.get("offset") == 0, body
' "$wiki_body" || fail "public /api/wiki/v1 page-list contract failed"
pass "public /api/wiki/v1 page-list is 200 JSON"

for private_path in health ready internal/wiki/moderation/reports; do
  status="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' \
    "$PUBLIC_ORIGIN/$private_path")"
  [[ "$status" == "404" ]] || fail "public /${private_path} returned ${status}, expected private-only 404"
done
pass "public /health, /ready, and internal moderation path remain private-only (404)"

pass "VM100 backend topology and live surface verified"
