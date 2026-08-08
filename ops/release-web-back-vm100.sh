#!/usr/bin/env bash
# Commit-bound, sequential VM100 rollout with automatic replica rollback.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_HOST="${MHB_RUNTIME_HOST:-metahumotonic27@192.168.0.24}"
DATA_HOST="${MHB_DATA_HOST:-metahumotonic27@192.168.0.25}"
RUNTIME_ENV_FILE="${MHB_RUNTIME_ENV_FILE:-/etc/metahumotonic/web-back.env}"
PROVISION_RECEIPT="${MHB_WIKI_PROVISION_RECEIPT:-/etc/metahumotonic/wiki-provision-receipt.json}"
REDIS_IMAGE_FILE="$REPO_ROOT/ops/redis-canary-image.txt"
EXPECTED_COMMIT="${1:-}"
CONTROL_MODE=release
if [[ "$EXPECTED_COMMIT" == --recover-rollback || "$EXPECTED_COMMIT" == --resume-public ]]; then
  CONTROL_MODE="${EXPECTED_COMMIT#--}"
  EXPECTED_COMMIT="${2:-}"
fi

fail() {
  printf 'FAIL %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'PASS %s\n' "$*"
}

[[ "$RUNTIME_HOST" =~ ^[A-Za-z0-9._@-]+$ ]] || fail "unsafe runtime host"
[[ "$DATA_HOST" =~ ^[A-Za-z0-9._@-]+$ ]] || fail "unsafe data host"
[[ "$RUNTIME_ENV_FILE" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "unsafe runtime env path"
[[ "$PROVISION_RECEIPT" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "unsafe provision receipt path"
command -v git >/dev/null || fail "git is required"
command -v ssh >/dev/null || fail "ssh is required"
command -v scp >/dev/null || fail "scp is required"
command -v python3 >/dev/null || fail "python3 is required"
command -v openssl >/dev/null || fail "openssl is required"

operation_id="$(openssl rand -hex 16)"
route_rollback_nonce="$(printf '%s' "${operation_id}:route-prior" | shasum -a 256 | awk '{print substr($1,1,32)}')"
lock_helper="/var/tmp/mhb-wiki-operation-lock-${operation_id}.sh"
data_lock=false
runtime_lock=false

remote_rollout_helper="/var/tmp/mhb-manage-wiki-rollout-${$}.sh"
remote_canary_helper="/var/tmp/mhb-run-wiki-canary-${$}.sh"
runtime_canary_helper="/var/tmp/mhb-manage-wiki-runtime-${$}.sh"
data_canary_helper="/var/tmp/mhb-manage-wiki-canary-db-${$}.sh"
data_backup_helper="/var/tmp/mhb-backup-wiki-release-db-${$}.sh"
install_rollout_helper() {
  scp -q "$REPO_ROOT/ops/remote/manage-wiki-rollout.sh" "$RUNTIME_HOST:$remote_rollout_helper"
}
remove_rollout_helper() {
  ssh -o BatchMode=yes "$RUNTIME_HOST" "rm -f -- '$remote_rollout_helper'" >/dev/null 2>&1 || true
}
remove_canary_helpers() {
  ssh -o BatchMode=yes "$RUNTIME_HOST" "rm -f -- '$remote_canary_helper'" >/dev/null 2>&1 || true
  ssh -o BatchMode=yes "$RUNTIME_HOST" "rm -f -- '$runtime_canary_helper'" >/dev/null 2>&1 || true
  ssh -o BatchMode=yes "$DATA_HOST" "rm -f -- '$data_canary_helper'" >/dev/null 2>&1 || true
  ssh -o BatchMode=yes "$DATA_HOST" "rm -f -- '$data_backup_helper'" >/dev/null 2>&1 || true
}

release_operation_locks() {
  local failed=false
  if [[ "$runtime_lock" == true ]]; then ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$lock_helper" release mhb-wiki-runtime-operation "$operation_id" || failed=true; fi
  if [[ "$data_lock" == true ]]; then ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$lock_helper" release mhb-wiki-data-operation "$operation_id" || failed=true; fi
  ssh -o BatchMode=yes "$RUNTIME_HOST" "rm -f -- '$lock_helper'" >/dev/null 2>&1 || true
  ssh -o BatchMode=yes "$DATA_HOST" "rm -f -- '$lock_helper'" >/dev/null 2>&1 || true
  [[ "$failed" == false ]]
}

scp -q "$REPO_ROOT/ops/remote/manage-wiki-operation-lock.sh" "$DATA_HOST:$lock_helper"
scp -q "$REPO_ROOT/ops/remote/manage-wiki-operation-lock.sh" "$RUNTIME_HOST:$lock_helper"
ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$lock_helper" acquire mhb-wiki-data-operation "$operation_id"
data_lock=true
trap 'release_operation_locks' EXIT
ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$lock_helper" acquire mhb-wiki-runtime-operation "$operation_id"
runtime_lock=true

if [[ "$CONTROL_MODE" == recover-rollback ]]; then
  [[ -z "$EXPECTED_COMMIT" || "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
    || fail "usage: $0 --recover-rollback [exact-commit]"
  install_rollout_helper
  trap 'remove_rollout_helper; release_operation_locks' EXIT
  rollout_status="$(ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash \
    "$remote_rollout_helper" status "$EXPECTED_COMMIT")"
  route_was="$(printf '%s\n' "$rollout_status" | awk -F= '$1 == "route_was" {print $2}')"
  active_nonce="$(printf '%s\n' "$rollout_status" | awk -F= '$1 == "rollout_nonce" {print $2}')"
  active_route_rollback_nonce="$(printf '%s\n' "$rollout_status" | awk -F= '$1 == "route_rollback_nonce" {print $2}')"
  ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$remote_rollout_helper" rollback "$EXPECTED_COMMIT"
  if [[ "$route_was" == enabled ]]; then route_action=--enable; else route_action=--disable; fi
  MHB_WIKI_ROUTE_NONCE="$active_route_rollback_nonce" "$REPO_ROOT/ops/enable-wiki-route-vm100.sh" "$route_action" \
    || fail "containers rolled back but route restoration failed; rerun exact route action ${route_action}"
  pass "incomplete rollout recovered; containers and initial route state restored"
  exit 0
fi

[[ -n "$EXPECTED_COMMIT" ]] || fail "usage: $0 <exact-commit>"
[[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "invalid commit"
cd "$REPO_ROOT"
git fetch --prune origin main
git cat-file -e "${EXPECTED_COMMIT}^{commit}"
commit="$EXPECTED_COMMIT"
if [[ "$CONTROL_MODE" == release ]]; then
  [[ -z "$(git status --porcelain=v1)" ]] || fail "working tree must be clean"
  [[ "$(git branch --show-current)" == "main" ]] || fail "release must run from main"
  [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "local main and fetched origin/main differ"
  [[ "$commit" == "$(git rev-parse HEAD)" ]] || fail "confirmation commit does not match HEAD"
fi

version="$(python3 -c '
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
project = re.search(r"(?ms)^\[project\]\s*$.*?(?=^\[|\Z)", text)
match = re.search(r"(?m)^version\s*=\s*\"([^\"]+)\"\s*$", project.group(0) if project else "")
if not match:
    raise SystemExit("project.version not found")
print(match.group(1))
' <(git show "$commit:pyproject.toml"))"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([._-][A-Za-z0-9.-]+)?$ ]] || fail "unsafe project version"

release_tmp="$(mktemp -d)"
canary_db_created=false
canary_database="metahumotonic_wiki_canary_${commit:0:12}_${operation_id:0:12}"
cleanup_local() {
  local status=$?
  trap - EXIT
  set +e
  if [[ "$canary_db_created" == true ]]; then
    ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$data_canary_helper" drop \
      postgresql "$canary_database" mhb_wiki "" "" "$commit" "$operation_id" || status=1
  fi
  rm -rf -- "$release_tmp"
  remove_rollout_helper
  remove_canary_helpers
  release_operation_locks || status=1
  exit "$status"
}
trap cleanup_local EXIT

# Public writes are never routed until the root-only provision receipt, exact
# pre-wiki env backup, encrypted DB backup, and restore drill have all passed.
ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n cat "$PROVISION_RECEIPT" >"$release_tmp/provision.json"
read -r transaction_id encrypted_backup backup_sha backup_key_file backup_key_sha env_backup < <(python3 - "$release_tmp/provision.json" <<'PY'
import json, pathlib, sys
body=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
db=body.get("database_backup", {})
assert body.get("schema") == "metahumotonic/wiki-provision-receipt@1"
assert body.get("status") == "INSTALLED"
assert db.get("status") == "VERIFIED" and db.get("restore_drill") == "PASS"
assert body.get("transaction_id") == db.get("transaction_id")
print(body["transaction_id"], db["encrypted_backup"], db["backup_sha256"], db["key_file"], db["key_sha256"], body["env_backup"])
PY
)
[[ "$transaction_id" =~ ^[0-9a-f]{32}$ ]] || fail "invalid provision transaction"
[[ "$encrypted_backup" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "unsafe backup path in receipt"
[[ "$backup_key_file" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "unsafe backup key path in receipt"
[[ "$backup_sha" =~ ^[0-9a-f]{64}$ ]] || fail "invalid backup digest in receipt"
[[ "$backup_key_sha" =~ ^[0-9a-f]{64}$ ]] || fail "invalid backup key digest in receipt"
[[ "$env_backup" == "${RUNTIME_ENV_FILE}.pre-wiki-${transaction_id}" ]] || fail "env backup is not transaction-bound"
ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n sh -s -- \
  "$RUNTIME_ENV_FILE" "$PROVISION_RECEIPT" "$env_backup" <<'REMOTE'
set -eu
for path in "$1" "$2" "$3"; do
  test -f "$path"
  test "$(stat -c '%U:%G:%a' "$path")" = root:root:600
done
for directory in "$(dirname "$1")" "$(dirname "$2")"; do
  test "$(stat -c '%U:%G:%a' "$directory")" = root:root:700
done
grep -q '^MHB_WIKI_PUBLIC_WRITES=true$' "$1"
grep -Eq '^MHB_WIKI_SESSION_SECRET=[[:xdigit:]]{64}$' "$1"
grep -Eq '^MHB_WIKI_MODERATION_ADMIN_KEY=[[:xdigit:]]{64}$' "$1"
session_secret="$(sed -n 's/^MHB_WIKI_SESSION_SECRET=//p' "$1")"
moderation_key="$(sed -n 's/^MHB_WIKI_MODERATION_ADMIN_KEY=//p' "$1")"
test "$session_secret" != "$moderation_key"
unset session_secret moderation_key
REMOTE
read -r actual_backup_sha actual_backup_key_sha < <(ssh -o BatchMode=yes "$DATA_HOST" sudo -n sh -s -- "$encrypted_backup" "$backup_key_file" <<'REMOTE'
set -eu
for path in "$1" "$2"; do
  test -f "$path"
  test "$(stat -c '%U:%G:%a' "$path")" = root:root:600
done
test "$(stat -c '%U:%G:%a' "$(dirname "$1")")" = root:root:700
test "$(stat -c '%U:%G:%a' "$(dirname "$2")")" = root:root:700
printf '%s %s\n' "$(sha256sum "$1" | awk '{print $1}')" "$(sha256sum "$2" | awk '{print $1}')"
REMOTE
)
[[ "$actual_backup_sha" == "$backup_sha" ]] || fail "encrypted database backup digest mismatch"
[[ "$actual_backup_key_sha" == "$backup_key_sha" ]] || fail "database backup key digest mismatch"
pass "preflight verified DB backup, disposable restore-drill receipt, and recoverable env"

install_rollout_helper
if ! route_was="$("$REPO_ROOT/ops/enable-wiki-route-vm100.sh" --status)"; then
  if ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n test -f /var/lib/metahumotonic-web-back/releases/active-rollout.env; then
    rollout_status="$(ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$remote_rollout_helper" status "")"
    route_was="$(printf '%s\n' "$rollout_status" | awk -F= '$1=="route_was"{print $2}')"
  else
    fail "mixed wiki route state has no receipt-bound active rollout recovery"
  fi
fi
[[ "$route_was" == enabled || "$route_was" == disabled ]] || fail "unknown wiki route state"

active_rollout_found=false
if ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n test -f \
    /var/lib/metahumotonic-web-back/releases/active-rollout.env; then
  active_rollout_found=true
  rollout_status="$(ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash \
    "$remote_rollout_helper" status "")"
  active_status="$(printf '%s\n' "$rollout_status" | awk -F= '$1 == "status" {print $2}')"
  active_commit="$(printf '%s\n' "$rollout_status" | awk -F= '$1 == "commit" {print $2}')"
  active_route_was="$(printf '%s\n' "$rollout_status" | awk -F= '$1 == "route_was" {print $2}')"
  active_nonce="$(printf '%s\n' "$rollout_status" | awk -F= '$1 == "rollout_nonce" {print $2}')"
  active_route_rollback_nonce="$(printf '%s\n' "$rollout_status" | awk -F= '$1 == "route_rollback_nonce" {print $2}')"
  if [[ "$CONTROL_MODE" == resume-public ]]; then
    [[ "$EXPECTED_COMMIT" == "$active_commit" ]] \
      || fail "resume commit does not match active rollout ${active_commit}"
    [[ "$active_route_was" == enabled || "$active_route_was" == disabled ]] \
      || fail "active rollout has invalid initial route state"
    route_was="$active_route_was"
  fi
  case "$active_status" in
    DONE) ;;
    ROLLED_BACK_CONTAINERS)
      [[ "$CONTROL_MODE" != resume-public ]] \
        || fail "rolled-back rollout cannot be resumed; start a new exact-commit release"
      ;;
    AWAITING_PUBLIC_READBACK|FINALIZING|DONE_ROLLBACK_RETAINED)
      if [[ "$CONTROL_MODE" != resume-public || "$EXPECTED_COMMIT" != "$active_commit" ]]; then
        fail "incomplete rollout ${active_commit} (${active_status}); use --resume-public ${active_commit} or --recover-rollback ${active_commit}"
      fi
      ;;
    *) fail "unsafe active rollout ${active_commit} (${active_status}); inspect state and use explicit recovery" ;;
  esac
fi
if [[ "$CONTROL_MODE" == resume-public && "$active_rollout_found" != true ]]; then
  fail "no active rollout state exists to resume"
fi

if [[ "$CONTROL_MODE" == resume-public ]]; then
  [[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "usage: $0 --resume-public <exact-commit>"
  case "$active_status" in
    AWAITING_PUBLIC_READBACK)
      if ! MHB_WIKI_ROUTE_NONCE="$active_nonce" "$REPO_ROOT/ops/enable-wiki-route-vm100.sh" --enable \
          || ! "$REPO_ROOT/ops/check-web-back-live.sh" --expected-commit "$EXPECTED_COMMIT" --candidate; then
        rollback_ok=true
        ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$remote_rollout_helper" rollback "$EXPECTED_COMMIT" || rollback_ok=false
        if [[ "$route_was" == enabled ]]; then route_action=--enable; else route_action=--disable; fi
        MHB_WIKI_ROUTE_NONCE="$active_route_rollback_nonce" "$REPO_ROOT/ops/enable-wiki-route-vm100.sh" "$route_action" || rollback_ok=false
        [[ "$rollback_ok" == true ]] || fail "resume failed and rollback/route restoration was incomplete; operator action required"
        fail "resume public verification failed; prior containers and route state restored"
      fi
      ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$remote_rollout_helper" finalize "$EXPECTED_COMMIT"
      "$REPO_ROOT/ops/check-web-back-live.sh" --expected-commit "$EXPECTED_COMMIT"
      ;;
    FINALIZING|DONE_ROLLBACK_RETAINED)
      # status already validated this nonce's state and durable receipt (when present).
      ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$remote_rollout_helper" finalize "$EXPECTED_COMMIT"
      "$REPO_ROOT/ops/check-web-back-live.sh" --expected-commit "$EXPECTED_COMMIT"
      ;;
    DONE)
      "$REPO_ROOT/ops/check-web-back-live.sh" --expected-commit "$EXPECTED_COMMIT"
      ;;
    *) fail "rollout status ${active_status} is not resumable" ;;
  esac
  pass "incomplete rollout resumed and finalized for ${EXPECTED_COMMIT}"
  exit 0
fi

archive="$release_tmp/source.tar"
git archive --format=tar "$commit" >"$archive"
archive_sha="$(shasum -a 256 "$archive" | awk '{print $1}')"
remote_archive="/var/tmp/metahumotonic-web-back-${commit}.tar"

scp -q "$archive" "$RUNTIME_HOST:$remote_archive"
pass "uploaded exact source archive ${commit} (${archive_sha})"

scp -q "$REPO_ROOT/ops/remote/run-wiki-release-canary.sh" "$RUNTIME_HOST:$remote_canary_helper"
scp -q "$REPO_ROOT/ops/remote/manage-wiki-canary-runtime.sh" "$RUNTIME_HOST:$runtime_canary_helper"
scp -q "$REPO_ROOT/ops/remote/manage-wiki-canary-database.sh" "$DATA_HOST:$data_canary_helper"
scp -q "$REPO_ROOT/ops/remote/backup-wiki-release-database.sh" "$DATA_HOST:$data_backup_helper"
redis_image="$(tr -d '\n' <"$REDIS_IMAGE_FILE")"
[[ "$redis_image" =~ ^redis:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]] || fail "Redis canary image must be digest pinned"
release_backup_key="$(openssl rand -hex 32)"
release_backup_receipt="$(printf '%s\n' "$release_backup_key" | ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$data_backup_helper" capture postgresql metahumotonic_wiki "$commit" "$operation_id")"
read -r encrypted_backup backup_key_file backup_sha backup_key_sha < <(printf '%s' "$release_backup_receipt" | python3 -c 'import json,sys; b=json.load(sys.stdin); assert b["snapshot"]=="current-production" and b["status"]=="CAPTURED"; print(b["encrypted_backup"],b["key_file"],b["backup_sha256"],b["key_sha256"])')
release_backup_receipt_path="$(printf '%s' "$release_backup_receipt" | python3 -c 'import json,sys,pathlib; b=json.load(sys.stdin); print(str(pathlib.Path(b["encrypted_backup"]).with_name("current-backup-receipt.json")))')"
canary_db_created=true
ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$data_canary_helper" create \
  postgresql "$canary_database" mhb_wiki "$encrypted_backup" "$backup_key_file" "$commit" "$operation_id" \
  "$backup_key_sha"
release_backup_receipt="$(ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$data_backup_helper" finalize postgresql metahumotonic_wiki "$commit" "$operation_id")"
release_backup_receipt_sha="$(ssh -o BatchMode=yes "$DATA_HOST" sudo -n sha256sum "$release_backup_receipt_path" | awk '{print $1}')"
[[ "$release_backup_receipt_sha" =~ ^[0-9a-f]{64}$ ]] || fail "invalid immutable backup receipt digest"

ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash -s -- \
  prepare "$commit" "$version" "$archive_sha" "$remote_archive" "$RUNTIME_ENV_FILE" \
  "$route_was" "$remote_canary_helper" "$canary_database" "$operation_id" "$redis_image" "$release_backup_receipt_path" "$release_backup_receipt_sha" "$runtime_canary_helper" "$route_rollback_nonce" <<'REMOTE'
set -Eeuo pipefail
mode="$1"; commit="$2"; version="$3"; expected_sha="$4"; archive="$5"; env_file="$6"; route_was="$7"; canary_helper="$8"; canary_database="$9"; rollout_nonce="${10}"; redis_image="${11}"; release_backup_receipt="${12}"; release_backup_receipt_sha="${13}"; runtime_canary_helper="${14}"; route_rollback_nonce="${15}"
[[ "$mode" == prepare ]]
release_root="/var/lib/metahumotonic-web-back/releases"
release_dir="$release_root/$commit"
state_file="$release_root/active-rollout.env"
image="metahumotonic-web-back:${version}-x86"

test -f "$archive"
test -f "$env_file"
test "$(stat -c '%U:%G:%a' "$env_file")" = root:root:600
grep -q '^MHB_WIKI_DATABASE_URL=' "$env_file"
grep -Eq '^MHB_WIKI_SESSION_SECRET=[[:xdigit:]]{64}$' "$env_file"
grep -Eq '^MHB_WIKI_MODERATION_ADMIN_KEY=[[:xdigit:]]{64}$' "$env_file"
grep -q '^MHB_WIKI_PUBLIC_WRITES=true$' "$env_file"
grep -q '^MHB_REDIS_URL=' "$env_file"

install -d -m 700 -o root -g root "$release_root" "$release_dir"
test "$(stat -c '%U:%G:%a' "$release_root")" = root:root:700
test "$(stat -c '%U:%G:%a' "$release_dir")" = root:root:700
actual_sha="$(sha256sum "$archive" | awk '{print $1}')"
test "$actual_sha" = "$expected_sha"
mv "$archive" "$release_dir/source.tar"
tar -xf "$release_dir/source.tar" -C "$release_dir"
migration_hash="$(
  cd "$release_dir"
  find migrations/wiki -type f -name '*.sql' -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum \
    | sha256sum \
    | awk '{print $1}'
)"
[[ "$migration_hash" =~ ^[0-9a-f]{64}$ ]]

docker build \
  --label "org.opencontainers.image.revision=$commit" \
  --label "com.metahumotonic.source-archive-sha256=$expected_sha" \
  --label "com.metahumotonic.wiki-migrations-sha256=$migration_hash" \
  -t "$image" "$release_dir"
image_id="$(docker image inspect "$image" --format '{{.Id}}')"
label_commit="$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
label_sha="$(docker image inspect "$image" --format '{{index .Config.Labels "com.metahumotonic.source-archive-sha256"}}')"
test "$label_commit" = "$commit"
test "$label_sha" = "$expected_sha"
test "$(docker image inspect "$image" --format '{{index .Config.Labels "com.metahumotonic.wiki-migrations-sha256"}}')" = "$migration_hash"

canary_receipt="$release_dir/wiki-release-canary-${rollout_nonce}.json"
test ! -e "$canary_receipt"
bash "$canary_helper" "$image" "$commit" "$env_file" "$canary_database" "$canary_receipt" "$rollout_nonce" "$redis_image" "$runtime_canary_helper"
test "$(stat -c '%U:%G:%a' "$canary_receipt")" = root:root:600
canary_sha="$(sha256sum "$canary_receipt" | awk '{print $1}')"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_one="web-back-pve-1-rollback-$stamp"
backup_two="web-back-pve-2-rollback-$stamp"
current_pair="$(docker inspect web-back-pve-1 web-back-pve-2)"
IFS='|' read -r current_image_id current_image_ref current_revision current_migration_hash < <(CURRENT_PAIR_JSON="$current_pair" python3 - <<'PY'
import json,os
items=json.loads(os.environ["CURRENT_PAIR_JSON"]); assert {x["Name"].lstrip("/") for x in items}=={"web-back-pve-1","web-back-pve-2"}
ids={x["Image"] for x in items}; refs={x["Config"]["Image"] for x in items}; assert len(ids)==len(refs)==1
ports={"web-back-pve-1":"18210","web-back-pve-2":"18211"}; revisions=set(); migrations=set()
for x in items:
 n=x["Name"].lstrip("/"); labels=x["Config"].get("Labels") or {}; revisions.add(labels.get("org.opencontainers.image.revision", "")); migrations.add(labels.get("com.metahumotonic.wiki-migrations-sha256", ""))
 assert x["State"]["Running"] is True and x["State"]["Health"]["Status"]=="healthy"; assert x["HostConfig"]["RestartPolicy"]["Name"]=="unless-stopped"
 bindings=x["HostConfig"]["PortBindings"]["8000/tcp"]
 assert len(bindings)==1 and bindings[0]["HostPort"]==ports[n] and bindings[0]["HostIp"]=="0.0.0.0"
assert len(revisions)==len(migrations)==1
revision=next(iter(revisions)) or "UNLABELED"
migration=next(iter(migrations)) or "UNLABELED"
print("|".join((next(iter(ids)),next(iter(refs)),revision,migration)))
PY
)"
if [[ "$current_revision" =~ ^[0-9a-f]{40}$ ]]; then
  test "$current_migration_hash" = "$migration_hash" || { printf 'FAIL migration tree changed; use maintenance migration flow before release\n' >&2; exit 1; }
  current_pointer="$release_root/$current_revision/deployment-current.env"
  test "$(stat -c '%U:%G:%a' "$current_pointer")" = root:root:600
  current_nonce="$(awk -F= '$1=="ROLLOUT_NONCE"{print $2}' "$current_pointer")"
  current_receipt="$(awk -F= '$1=="RECEIPT"{print $2}' "$current_pointer")"
  [[ "$current_nonce" =~ ^[0-9a-f]{32}$ ]]
  test "$current_receipt" = "$release_root/$current_revision/deployment-receipt-${current_nonce}.env"
  grep -qx "COMMIT=$current_revision" "$current_pointer"
  test "$(stat -c '%U:%G:%a' "$current_receipt")" = root:root:600
  grep -qx 'STATUS=DONE' "$current_receipt"
  grep -qx "COMMIT=$current_revision" "$current_receipt"
  grep -qx "ROLLOUT_NONCE=$current_nonce" "$current_receipt"
  grep -qx "IMAGE_ID=$current_image_id" "$current_receipt"
  grep -qx "MIGRATIONS_SHA256=$current_migration_hash" "$current_receipt"
  migration_policy=UNCHANGED
else
  test "$current_revision" = UNLABELED
  test "$route_was" = disabled
  [[ "$current_image_ref" == metahumotonic-web-back:0.9.1-x86 ]] || { printf 'FAIL unlabeled deployment is not the documented 0.9.1 bootstrap\n' >&2; exit 1; }
  test "$current_migration_hash" = UNLABELED
  migration_policy=BOOTSTRAP_0_9_1_ROUTE_DISABLED
fi
schema_receipt="$release_dir/schema-gate-receipt-${rollout_nonce}.json"
schema_receipt_pending="${schema_receipt}.pending.${rollout_nonce}"
python3 - "$schema_receipt_pending" "$commit" "$rollout_nonce" "$migration_hash" "$migration_policy" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); b={"schema":"metahumotonic/wiki-schema-gate@1","commit":sys.argv[2],"rollout_nonce":sys.argv[3],"migrations_sha256":sys.argv[4],"policy":sys.argv[5],"schema_check":"PASS"}
p.write_text(json.dumps(b,sort_keys=True)+"\n"); os.chmod(p,0o600); os.chown(p,0,0)
PY
schema_receipt_sha="$(sha256sum "$schema_receipt_pending" | awk '{print $1}')"
state_tmp="${state_file}.tmp.${rollout_nonce}"
cat >"$state_tmp" <<STATE
STATUS=PREPARING
COMMIT=$commit
VERSION=$version
IMAGE=$image
IMAGE_ID=$image_id
ARCHIVE_SHA256=$expected_sha
MIGRATIONS_SHA256=$migration_hash
MIGRATION_POLICY=$migration_policy
SCHEMA_GATE_RECEIPT=$schema_receipt
SCHEMA_GATE_RECEIPT_SHA256=$schema_receipt_sha
ROLLOUT_NONCE=$rollout_nonce
ROUTE_ENABLE_NONCE=$rollout_nonce
ROUTE_ROLLBACK_NONCE=$route_rollback_nonce
REDIS_IMAGE=$redis_image
CURRENT_BACKUP_RECEIPT=$release_backup_receipt
CURRENT_BACKUP_RECEIPT_SHA256=$release_backup_receipt_sha
ENV_FILE=$env_file
BACKUP_ONE=$backup_one
BACKUP_TWO=$backup_two
PRIOR_IMAGE_ID=$current_image_id
PRIOR_IMAGE_REF=$current_image_ref
PRIOR_REVISION=$current_revision
PRIOR_MIGRATIONS_SHA256=$current_migration_hash
PRIOR_RESTART_POLICY=unless-stopped
PRIOR_HEALTH=healthy
PRIOR_ONE_PORT=18210
PRIOR_TWO_PORT=18211
ROUTE_WAS=$route_was
CANARY_RECEIPT=$canary_receipt
CANARY_SHA256=$canary_sha
STATE
chown root:root "$state_tmp"
chmod 600 "$state_tmp"
mv "$state_tmp" "$state_file"
test "$(stat -c '%U:%G:%a' "$state_file")" = root:root:600

printf 'image=%s\nimage_id=%s\nstate_file=%s\n' "$image" "$image_id" "$state_file"
REMOTE

ssh -o BatchMode=yes "$DATA_HOST" sudo -n bash "$data_canary_helper" drop \
  postgresql "$canary_database" mhb_wiki "" "" "$commit" "$operation_id"
canary_db_created=false

ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$remote_rollout_helper" deploy "$commit"

rollback_replicas() {
  ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$remote_rollout_helper" rollback "$commit"
}

if ! MHB_WIKI_ROUTE_NONCE="$operation_id" "$REPO_ROOT/ops/enable-wiki-route-vm100.sh" --enable; then
  rollback_ok=true
  rollback_replicas || rollback_ok=false
  if [[ "$route_was" == enabled ]]; then route_action=--enable; else route_action=--disable; fi
  MHB_WIKI_ROUTE_NONCE="$route_rollback_nonce" "$REPO_ROOT/ops/enable-wiki-route-vm100.sh" "$route_action" || rollback_ok=false
  [[ "$rollback_ok" == true ]] \
    || fail "route activation failed and rollback/route restoration was incomplete; operator action required"
  fail "route activation failed; replicas and initial route state restored"
fi
if ! "$REPO_ROOT/ops/check-web-back-live.sh" --expected-commit "$commit" --candidate; then
  rollback_ok=true
  rollback_replicas || rollback_ok=false
  if [[ "$route_was" == enabled ]]; then route_action=--enable; else route_action=--disable; fi
  MHB_WIKI_ROUTE_NONCE="$route_rollback_nonce" "$REPO_ROOT/ops/enable-wiki-route-vm100.sh" "$route_action" || rollback_ok=false
  [[ "$rollback_ok" == true ]] \
    || fail "public readback failed and rollback/route restoration was incomplete; operator action required"
  fail "public readback failed; replicas and initial route state restored"
fi

ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash "$remote_rollout_helper" finalize "$commit"
"$REPO_ROOT/ops/check-web-back-live.sh" --expected-commit "$commit"

pass "VM100 sequential rollout complete for ${commit} (${version})"
