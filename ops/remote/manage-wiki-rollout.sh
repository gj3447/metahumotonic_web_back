#!/usr/bin/env bash
# Root-only, idempotent recovery/finalization for one VM100 wiki rollout.
set -Eeuo pipefail

mode="${1:-}"
expected_commit="${2:-}"
state_file="${3:-/var/lib/metahumotonic-web-back/releases/active-rollout.env}"
[[ "$mode" == status || "$mode" == deploy || "$mode" == rollback || "$mode" == finalize ]]
[[ -z "$expected_commit" || "$expected_commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$state_file" =~ ^/[A-Za-z0-9._/-]+$ ]]
test -f "$state_file"
test "$(stat -c '%U:%G:%a' "$state_file")" = root:root:600
test "$(stat -c '%U:%G:%a' "$(dirname "$state_file")")" = root:root:700
# shellcheck disable=SC1090
. "$state_file"
[[ "${COMMIT:-}" =~ ^[0-9a-f]{40}$ ]]
[[ -z "$expected_commit" || "$COMMIT" == "$expected_commit" ]]
[[ "${ROLLOUT_NONCE:-}" =~ ^[0-9a-f]{32}$ ]]
[[ "${ROUTE_ENABLE_NONCE:-}" == "$ROLLOUT_NONCE" ]]
[[ "${ROUTE_ROLLBACK_NONCE:-}" =~ ^[0-9a-f]{32}$ ]]
[[ "$ROUTE_ROLLBACK_NONCE" != "$ROLLOUT_NONCE" ]]
[[ "${ROUTE_WAS:-}" == enabled || "${ROUTE_WAS:-}" == disabled ]]
[[ "${CANARY_RECEIPT:-}" == "/var/lib/metahumotonic-web-back/releases/$COMMIT/wiki-release-canary-${ROLLOUT_NONCE}.json" ]]
[[ "${CANARY_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]
[[ "${REDIS_IMAGE:-}" =~ ^redis:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]]
[[ "${CURRENT_BACKUP_RECEIPT:-}" == "/var/lib/metahumotonic-wiki/releases/${COMMIT}-${ROLLOUT_NONCE}/current-backup-receipt.json" ]]
[[ "${CURRENT_BACKUP_RECEIPT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]
[[ "${MIGRATIONS_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]
[[ "${MIGRATION_POLICY:-}" == UNCHANGED || "${MIGRATION_POLICY:-}" == BOOTSTRAP_0_9_1_ROUTE_DISABLED ]]
[[ "${SCHEMA_GATE_RECEIPT:-}" == "/var/lib/metahumotonic-web-back/releases/$COMMIT/schema-gate-receipt-${ROLLOUT_NONCE}.json" ]]
[[ "${SCHEMA_GATE_RECEIPT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]
[[ "${PRIOR_IMAGE_ID:-}" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "${PRIOR_IMAGE_REF:-}" =~ ^[A-Za-z0-9._:/-]+$ ]]
[[ "${PRIOR_REVISION:-}" == UNLABELED || "${PRIOR_REVISION:-}" =~ ^[0-9a-f]{40}$ ]]
[[ "${PRIOR_MIGRATIONS_SHA256:-}" == UNLABELED || "${PRIOR_MIGRATIONS_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]
[[ "${PRIOR_RESTART_POLICY:-}" == unless-stopped ]]
[[ "${PRIOR_HEALTH:-}" == healthy ]]
[[ "${PRIOR_ONE_PORT:-}" == 18210 && "${PRIOR_TWO_PORT:-}" == 18211 ]]
[[ "${PRIOR_HOST_IP:-}" == DOCKER_DEFAULT_ALL || "${PRIOR_HOST_IP:-}" == 0.0.0.0 ]]
test "$(stat -c '%U:%G:%a' "$CANARY_RECEIPT")" = root:root:600
test "$(stat -c '%U:%G:%a' "$(dirname "$CANARY_RECEIPT")")" = root:root:700
test "$(sha256sum "$CANARY_RECEIPT" | awk '{print $1}')" = "$CANARY_SHA256"
for value in "${BACKUP_ONE:-}" "${BACKUP_TWO:-}"; do
  [[ "$value" =~ ^web-back-pve-[12]-rollback-[0-9TZ]+$ ]]
done

release_dir="/var/lib/metahumotonic-web-back/releases/$COMMIT"
receipt="$release_dir/deployment-receipt-${ROLLOUT_NONCE}.env"
current_pointer="$release_dir/deployment-current.env"

set_status() {
  local status="$1" temporary
  temporary="${state_file}.tmp.${ROLLOUT_NONCE}"
  awk -v status="$status" 'BEGIN { print "STATUS=" status } $0 !~ /^STATUS=/' \
    "$state_file" >"$temporary"
  chown root:root "$temporary"
  chmod 600 "$temporary"
  mv -T "$temporary" "$state_file"
  STATUS="$status"
}

validate_deployment_receipt() {
  local required_status="$1"
  test -f "$receipt"
  test "$(stat -c '%U:%G:%a' "$receipt")" = root:root:600
  python3 - "$state_file" "$receipt" "$required_status" <<'PY'
import pathlib, sys
def env(path):
    return dict(line.split("=", 1) for line in pathlib.Path(path).read_text().splitlines() if "=" in line)
state, receipt, required = env(sys.argv[1]), env(sys.argv[2]), sys.argv[3]
assert receipt.get("STATUS") == required
assert receipt.get("FINALIZED_AT")
for key, value in state.items():
    if key != "STATUS":
        assert receipt.get(key) == value, (key, receipt.get(key), value)
assert receipt.get("ROLLOUT_NONCE") == state.get("ROLLOUT_NONCE")
PY
}

validate_current_pointer() {
  test -f "$current_pointer"
  test "$(stat -c '%U:%G:%a' "$current_pointer")" = root:root:600
  grep -qx "COMMIT=$COMMIT" "$current_pointer"
  grep -qx "ROLLOUT_NONCE=$ROLLOUT_NONCE" "$current_pointer"
  grep -qx "RECEIPT=$receipt" "$current_pointer"
}

retire_previous_current_pointer() {
  local previous_pointer pointer_body previous_receipt
  test -e "$current_pointer" || return 0
  if validate_current_pointer >/dev/null 2>&1; then return 0; fi
  test "$(stat -c '%U:%G:%a' "$current_pointer")" = root:root:600
  pointer_body="$(cat "$current_pointer")"
  previous_receipt="$(POINTER_BODY="$pointer_body" python3 - "$COMMIT" "$release_dir" "$ROLLOUT_NONCE" <<'PY'
import os, re, sys
b=dict(line.split("=",1) for line in os.environ["POINTER_BODY"].splitlines() if "=" in line)
assert b.get("COMMIT")==sys.argv[1]
nonce=b.get("ROLLOUT_NONCE", ""); assert re.fullmatch(r"[0-9a-f]{32}", nonce) and nonce != sys.argv[3]
receipt=b.get("RECEIPT"); assert receipt==f"{sys.argv[2]}/deployment-receipt-{nonce}.env"
print(receipt)
PY
)"
  test "$(stat -c '%U:%G:%a' "$previous_receipt")" = root:root:600
  grep -qx 'STATUS=DONE' "$previous_receipt"
  grep -qx "COMMIT=$COMMIT" "$previous_receipt"
  previous_pointer="${current_pointer%.env}.previous-${ROLLOUT_NONCE}.env"
  test ! -e "$previous_pointer"
  mv -T "$current_pointer" "$previous_pointer"
  test "$(stat -c '%U:%G:%a' "$previous_pointer")" = root:root:600
}

validate_retained_or_done_receipt() {
  if grep -qx 'STATUS=DONE_ROLLBACK_RETAINED' "$receipt"; then
    validate_deployment_receipt DONE_ROLLBACK_RETAINED
  else
    validate_deployment_receipt DONE
    if test -e "$current_pointer"; then validate_current_pointer; fi
  fi
}

validate_prior_container() {
  local container="$1" expected_name="$2" expected_port="$3" expected_running="$4"
  local container_json
  container_json="$(docker inspect "$container")"
  CONTAINER_JSON="$container_json" python3 - "$expected_name" "$expected_port" "$expected_running" \
    "$PRIOR_IMAGE_ID" "$PRIOR_IMAGE_REF" "$PRIOR_REVISION" "$PRIOR_MIGRATIONS_SHA256" \
    "$PRIOR_RESTART_POLICY" "$PRIOR_HEALTH" "$PRIOR_HOST_IP" <<'PY'
import json, os, sys
x=json.loads(os.environ["CONTAINER_JSON"])[0]
name, port, running, image_id, image_ref, revision, migrations, restart, health, host_ip=sys.argv[1:]
labels=x["Config"].get("Labels") or {}
actual_revision=labels.get("org.opencontainers.image.revision") or "UNLABELED"
actual_migrations=labels.get("com.metahumotonic.wiki-migrations-sha256") or "UNLABELED"
assert x["Name"].lstrip("/")==name
assert x["Image"]==image_id and x["Config"]["Image"]==image_ref
assert actual_revision==revision and actual_migrations==migrations
assert x["HostConfig"]["RestartPolicy"]["Name"]==restart
bindings=x["HostConfig"]["PortBindings"]["8000/tcp"]
expected_host_ip="" if host_ip=="DOCKER_DEFAULT_ALL" else host_ip
assert len(bindings)==1 and bindings[0]["HostPort"]==port and bindings[0]["HostIp"]==expected_host_ip
if running != "any":
    assert x["State"]["Running"] is (running=="true")
    if running=="true": assert x["State"]["Health"]["Status"]==health
PY
}

validate_backup() {
  local backup="$1" port="$2"
  validate_prior_container "$backup" "$backup" "$port" false
}

validate_candidate_container() {
  local container="$1" expected_port="$2" state_check="${3:-strict}"
  local container_json
  container_json="$(docker inspect "$container")"
  CONTAINER_JSON="$container_json" python3 - "$container" "$expected_port" "$IMAGE_ID" "$IMAGE" "$COMMIT" "$MIGRATIONS_SHA256" "$state_check" <<'PY'
import json, os, sys
x=json.loads(os.environ["CONTAINER_JSON"])[0]
name, port, image_id, image_ref, commit, migrations, state_check=sys.argv[1:]
labels=x["Config"].get("Labels") or {}
assert x["Name"].lstrip("/")==name
assert x["Image"]==image_id and x["Config"]["Image"]==image_ref
assert labels.get("org.opencontainers.image.revision")==commit
assert labels.get("com.metahumotonic.wiki-migrations-sha256")==migrations
assert x["HostConfig"]["RestartPolicy"]["Name"]=="unless-stopped"
bindings=x["HostConfig"]["PortBindings"]["8000/tcp"]
assert len(bindings)==1 and bindings[0]["HostPort"]==port and bindings[0]["HostIp"]=="0.0.0.0"
if state_check=="strict":
    assert x["State"]["Running"] is True and x["State"]["Health"]["Status"]=="healthy"
PY
}

if [[ "$mode" == status ]]; then
  case "${STATUS:-}" in
    FINALIZING)
      if test -e "$receipt"; then validate_deployment_receipt DONE_ROLLBACK_RETAINED; fi
      ;;
    DONE_ROLLBACK_RETAINED) validate_retained_or_done_receipt ;;
    DONE) validate_deployment_receipt DONE; validate_current_pointer ;;
  esac
  printf 'status=%s\ncommit=%s\nroute_was=%s\nrollout_nonce=%s\nroute_rollback_nonce=%s\nreceipt=%s\n' \
    "${STATUS:-UNKNOWN}" "$COMMIT" "$ROUTE_WAS" "$ROLLOUT_NONCE" "$ROUTE_ROLLBACK_NONCE" "$receipt"
  exit 0
fi

if [[ "$mode" == deploy ]]; then
  [[ "${STATUS:-}" == PREPARING ]]
  ! docker container inspect "$BACKUP_ONE" >/dev/null 2>&1
  ! docker container inspect "$BACKUP_TWO" >/dev/null 2>&1
  validate_prior_container web-back-pve-1 web-back-pve-1 "$PRIOR_ONE_PORT" true
  validate_prior_container web-back-pve-2 web-back-pve-2 "$PRIOR_TWO_PORT" true
  schema_pending="${SCHEMA_GATE_RECEIPT}.pending.${ROLLOUT_NONCE}"
  test "$(sha256sum "$schema_pending" | awk '{print $1}')" = "$SCHEMA_GATE_RECEIPT_SHA256"
  if [[ "$MIGRATION_POLICY" == BOOTSTRAP_0_9_1_ROUTE_DISABLED ]]; then
    docker run --rm -i --env-file "$ENV_FILE" --entrypoint python "$IMAGE" - <<'PY'
import asyncio, os
from app.wiki.postgres import PostgresWikiStore
async def main():
    store = await PostgresWikiStore.connect(os.environ["MHB_WIKI_DATABASE_URL"])
    try: await store.ensure_schema()
    finally: await store.close()
asyncio.run(main())
PY
  fi
  mv "$schema_pending" "$SCHEMA_GATE_RECEIPT"
  rollback_deploy() {
    original=$?; trap - ERR
    if ! bash "$0" rollback "$COMMIT" "$state_file"; then
      printf 'FAIL deploy and automatic rollback both failed; operator recovery required\n' >&2
      exit 70
    fi
    exit "$original"
  }
  trap rollback_deploy ERR
  replace_one() {
    name="$1"; port="$2"; backup="$3"
    docker stop "$name" >/dev/null
    docker rename "$name" "$backup"
    validate_backup "$backup" "$port"
    docker run -d --name "$name" --restart unless-stopped --env-file "$ENV_FILE" -p "0.0.0.0:$port:8000" "$IMAGE" >/dev/null
    for _ in $(seq 1 45); do
      if curl -fsS --max-time 3 "http://127.0.0.1:${port}/health" >/dev/null \
          && curl -fsS --max-time 3 "http://127.0.0.1:${port}/ready" >/dev/null \
          && test "$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null)" = true \
          && test "$(docker inspect "$name" --format '{{.State.Health.Status}}' 2>/dev/null)" = healthy; then
        validate_candidate_container "$name" "$port"
        return 0
      fi
      sleep 2
    done
    return 1
  }
  replace_one web-back-pve-1 "$PRIOR_ONE_PORT" "$BACKUP_ONE"
  replace_one web-back-pve-2 "$PRIOR_TWO_PORT" "$BACKUP_TWO"
  validate_candidate_container web-back-pve-1 "$PRIOR_ONE_PORT"
  validate_candidate_container web-back-pve-2 "$PRIOR_TWO_PORT"
  set_status AWAITING_PUBLIC_READBACK
  trap - ERR
  printf 'PASS replicas deployed after complete state and schema gate\n'
  exit 0
fi

if [[ "$mode" == rollback ]]; then
  case "${STATUS:-}" in
    DONE|DONE_ROLLBACK_RETAINED)
      printf 'FAIL rollback boundary already committed; resume idempotent finalization instead\n' >&2
      exit 1
      ;;
    FINALIZING)
      if test -f "$receipt"; then
        validate_deployment_receipt DONE_ROLLBACK_RETAINED
        printf 'FAIL finalization receipt already committed; resume finalization instead\n' >&2
        exit 1
      fi
      ;;
  esac
  # Validate every surviving rollback object before mutating either live replica.
  if docker container inspect "$BACKUP_ONE" >/dev/null 2>&1; then
    validate_backup "$BACKUP_ONE" "$PRIOR_ONE_PORT"
    if docker container inspect web-back-pve-1 >/dev/null 2>&1; then
      validate_candidate_container web-back-pve-1 "$PRIOR_ONE_PORT" identity-only
    fi
  else
    validate_prior_container web-back-pve-1 web-back-pve-1 "$PRIOR_ONE_PORT" any
  fi
  if docker container inspect "$BACKUP_TWO" >/dev/null 2>&1; then
    validate_backup "$BACKUP_TWO" "$PRIOR_TWO_PORT"
    if docker container inspect web-back-pve-2 >/dev/null 2>&1; then
      validate_candidate_container web-back-pve-2 "$PRIOR_TWO_PORT" identity-only
    fi
  else
    validate_prior_container web-back-pve-2 web-back-pve-2 "$PRIOR_TWO_PORT" any
  fi
  restored=true
  set +e
  for spec in "web-back-pve-2:$BACKUP_TWO:$PRIOR_TWO_PORT" "web-back-pve-1:$BACKUP_ONE:$PRIOR_ONE_PORT"; do
    name="${spec%%:*}"; rest="${spec#*:}"; backup="${rest%%:*}"; port="${rest##*:}"
    if docker container inspect "$backup" >/dev/null 2>&1; then
      if docker container inspect "$name" >/dev/null 2>&1; then
        docker stop "$name" >/dev/null 2>&1 || restored=false
        docker rm "$name" >/dev/null 2>&1 || restored=false
      fi
      docker rename "$backup" "$name" >/dev/null 2>&1 || restored=false
    fi
    docker start "$name" >/dev/null 2>&1 || restored=false
    healthy=false
    for _ in $(seq 1 45); do
      if test "$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null)" = true \
          && test "$(docker inspect "$name" --format '{{.State.Health.Status}}' 2>/dev/null)" = healthy; then
        healthy=true; break
      fi
      sleep 2
    done
    [[ "$healthy" == true ]] || restored=false
    validate_prior_container "$name" "$name" "$port" true >/dev/null 2>&1 || restored=false
  done
  set -e
  if [[ "$restored" != true ]]; then
    set_status ROLLBACK_FAILED_REQUIRES_OPERATOR
    printf 'FAIL replica rollback incomplete; state retained at %s\n' "$state_file" >&2
    exit 1
  fi
  set_status ROLLED_BACK_CONTAINERS
  printf 'PASS containers rolled back; route_was=%s\n' "$ROUTE_WAS"
  exit 0
fi

[[ "${STATUS:-}" == AWAITING_PUBLIC_READBACK || "${STATUS:-}" == FINALIZING \
   || "${STATUS:-}" == DONE_ROLLBACK_RETAINED || "${STATUS:-}" == DONE ]]
if [[ "$STATUS" == DONE ]]; then
  validate_deployment_receipt DONE
  validate_current_pointer
  printf 'PASS rollout already finalized with durable receipt %s\n' "$receipt"
  exit 0
fi
if [[ "$STATUS" == AWAITING_PUBLIC_READBACK ]]; then set_status FINALIZING; fi
if [[ "$STATUS" == FINALIZING ]]; then
  if test -e "$receipt"; then
    validate_deployment_receipt DONE_ROLLBACK_RETAINED
  else
    temporary="${receipt}.tmp.${ROLLOUT_NONCE}"
    awk '$0 !~ /^STATUS=/' "$state_file" >"$temporary"
    printf 'STATUS=DONE_ROLLBACK_RETAINED\nFINALIZED_AT=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$temporary"
    chown root:root "$temporary"; chmod 600 "$temporary"; mv -T "$temporary" "$receipt"
    validate_deployment_receipt DONE_ROLLBACK_RETAINED
  fi
  set_status DONE_ROLLBACK_RETAINED
fi
retire_previous_current_pointer

# The nonce-specific durable receipt is committed before rollback containers are removed.
# Validate both backups and both candidates before deleting either name.
for spec in "$BACKUP_ONE:$PRIOR_ONE_PORT" "$BACKUP_TWO:$PRIOR_TWO_PORT"; do
  backup="${spec%%:*}"; port="${spec##*:}"
  if docker container inspect "$backup" >/dev/null 2>&1; then
    validate_backup "$backup" "$port"
  fi
done
validate_candidate_container web-back-pve-1 "$PRIOR_ONE_PORT"
validate_candidate_container web-back-pve-2 "$PRIOR_TWO_PORT"
for backup in "$BACKUP_ONE" "$BACKUP_TWO"; do
  if docker container inspect "$backup" >/dev/null 2>&1; then docker rm "$backup" >/dev/null; fi
done
if grep -qx 'STATUS=DONE_ROLLBACK_RETAINED' "$receipt"; then
  receipt_next="${receipt}.tmp.${ROLLOUT_NONCE}"
  awk 'BEGIN{print "STATUS=DONE"} $0 !~ /^STATUS=/' "$receipt" >"$receipt_next"
  chown root:root "$receipt_next"; chmod 600 "$receipt_next"; mv -T "$receipt_next" "$receipt"
fi
validate_deployment_receipt DONE
pointer_next="${current_pointer}.tmp.${ROLLOUT_NONCE}"
printf 'COMMIT=%s\nROLLOUT_NONCE=%s\nRECEIPT=%s\n' "$COMMIT" "$ROLLOUT_NONCE" "$receipt" >"$pointer_next"
chown root:root "$pointer_next"; chmod 600 "$pointer_next"; mv -T "$pointer_next" "$current_pointer"
validate_current_pointer
set_status DONE
printf 'PASS rollout finalized with durable receipt %s\n' "$receipt"
