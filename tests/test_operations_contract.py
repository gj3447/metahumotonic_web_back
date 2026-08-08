from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_OPERATIONS_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "DEV_STACK.md",
    ROOT / "docs" / "OMD_PARALLEL.md",
    ROOT / "docs" / "KG_PROXY_CONNECT.md",
    ROOT / "docs" / "OPERATIONS_VM100.md",
    ROOT / "deploy" / "k8s" / "README.md",
)


def test_retired_dgx_manifest_cannot_be_applied_from_old_path():
    assert not (ROOT / "deploy" / "k8s" / "web-back.yaml").exists()
    assert not (ROOT / "deploy" / "k8s" / "web-back-secret.example.yaml").exists()

    legacy = ROOT / "deploy" / "legacy" / "web-back-dgx-retired.yaml"
    assert legacy.is_file()
    assert legacy.read_text(encoding="utf-8").startswith(
        "# RETIRED 2026-08-08 — HISTORICAL EVIDENCE ONLY. DO NOT APPLY."
    )


def test_active_runbooks_do_not_reintroduce_retired_commands():
    joined = "\n".join(
        path.read_text(encoding="utf-8") for path in ACTIVE_OPERATIONS_DOCS
    )
    forbidden = (
        "mcp__omd__",
        "ssh dgx",
        "kubectl apply -f deploy/k8s/web-back.yaml",
        "kubectl rollout restart deploy/web-back",
        "kubectl -n infra patch secret web-back-secrets",
    )
    for command in forbidden:
        assert command not in joined

    assert "ops/check-web-back-live.sh" in joined
    assert "docs/OPERATIONS_VM100.md" in (ROOT / "README.md").read_text(
        encoding="utf-8"
    )


def test_omd_driver_fails_closed_and_archive_is_exact():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "omd" / "driver.py")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 78
    assert "OMD is retired" in result.stderr

    archive = ROOT / "docs" / "archive" / "omd" / "driver_20260713.py.txt"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert digest == "ab47f84e157b0e3bd7bc20b07cbb2ce3cec1c538a5049500db4eb8c6c4d34ca7"


def _ingress(name: str) -> dict:
    host = "Host(`metahumotonic.com`)"
    paths = "PathPrefix(`/api/mcp`)"
    return {
        "metadata": {"name": name, "resourceVersion": "100"},
        "spec": {
            "routes": [
                {
                    "match": f"{host} && ({paths})",
                    "services": [{"name": "web-back", "port": 8000}],
                }
            ]
        },
    }


def test_wiki_route_activation_is_exact_and_idempotent(tmp_path):
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    fake_bin.mkdir()
    state.mkdir()
    for name in ("web-back-api", "web-back-api-tls"):
        (state / f"{name}.json").write_text(
            json.dumps(_ingress(name)), encoding="utf-8"
        )

    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
if "kubectl" not in args:
    command = args[args.index("sudo") + 2:]
    if command[:2] == ["test", "-f"]:
        raise SystemExit(0 if pathlib.Path(command[2]).is_file() else 1)
    if command[0] == "cat":
        sys.stdout.write(pathlib.Path(command[1]).read_text()); raise SystemExit(0)
    if command[:2] == ["install", "-d"]:
        pathlib.Path(command[-1]).mkdir(parents=True, exist_ok=True); raise SystemExit(0)
    if command[0] == "tee":
        pathlib.Path(command[1]).write_text(sys.stdin.read()); raise SystemExit(0)
    if command[0] in {"chown", "chmod"}: raise SystemExit(0)
    if command[:2] == ["python3", "-"]:
        source=sys.stdin.read().replace("os.chown(t,0,0); ", "")
        result=subprocess.run([sys.executable,"-",*command[2:]],input=source,text=True)
        raise SystemExit(result.returncode)
    raise SystemExit(f"unexpected remote command: {command}")
index = args.index("kubectl")
command = args[index + 3:]
state = pathlib.Path(os.environ["FAKE_KUBE_DIR"])
verb, resource, name = command[:3]
path = state / f"{name}.json"
document = json.loads(path.read_text(encoding="utf-8"))
if verb == "get":
    print(json.dumps(document))
elif verb == "patch":
    fail_name = os.environ.get("FAKE_FAIL_PATCH_ONCE")
    fail_marker = state / ".failed-once"
    if fail_name == name and not fail_marker.exists():
        fail_marker.write_text(name, encoding="utf-8")
        raise SystemExit(55)
    assert "--patch-file=/dev/stdin" in command
    patch = json.load(sys.stdin)
    if (
        os.environ.get("FAKE_FAIL_ROLLBACK") == name
        and patch
        and "PathPrefix(`/api/wiki`)" in patch[0]["value"]
    ):
        raise SystemExit(56)
    current = document["spec"]["routes"][0]["match"]
    assert patch[0] == {"op": "test", "path": "/spec/routes/0/match", "value": current}
    document["spec"]["routes"][0]["match"] = patch[1]["value"]
    path.write_text(json.dumps(document), encoding="utf-8")
else:
    raise SystemExit(f"unexpected command: {command}")
""",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_KUBE_DIR": str(state),
        "MHB_RUNTIME_HOST": "test@example.invalid",
        "MHB_WIKI_ROUTE_NONCE": "a" * 32,
        "MHB_WIKI_ROUTE_RECEIPT_ROOT": str(tmp_path / "route-receipts"),
    }
    script = ROOT / "ops" / "enable-wiki-route-vm100.sh"

    first = subprocess.run(
        ["bash", str(script)], env=env, text=True, capture_output=True, check=False
    )
    assert first.returncode == 0, first.stderr
    assert first.stdout.count("patched") == 2
    for name in ("web-back-api", "web-back-api-tls"):
        match = json.loads((state / f"{name}.json").read_text(encoding="utf-8"))[
            "spec"
        ]["routes"][0]["match"]
        assert match.count("PathPrefix(`/api/wiki`)") == 1
        assert (
            match
            == "Host(`metahumotonic.com`) && (PathPrefix(`/api/mcp`) || PathPrefix(`/api/wiki`))"
        )

    second = subprocess.run(
        ["bash", str(script)], env=env, text=True, capture_output=True, check=False
    )
    assert second.returncode == 0, second.stderr
    assert second.stdout.count("already contains exact wiki prefix") == 2

    status = subprocess.run(
        ["bash", str(script), "--status"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    assert status.stdout.strip() == "enabled"

    disabled = subprocess.run(
        ["bash", str(script), "--disable"],
        env={**env, "MHB_WIKI_ROUTE_NONCE": "b" * 32},
        text=True,
        capture_output=True,
        check=False,
    )
    assert disabled.returncode == 0, disabled.stderr
    for name in ("web-back-api", "web-back-api-tls"):
        assert json.loads((state / f"{name}.json").read_text(encoding="utf-8")) == _ingress(name)

    # Disable is also transactional: failure on the second patch restores the
    # first route to its exact enabled match.
    enabled = subprocess.run(
        ["bash", str(script), "--enable"],
        env={**env, "MHB_WIKI_ROUTE_NONCE": "c" * 32},
        text=True,
        capture_output=True,
        check=False,
    )
    assert enabled.returncode == 0, enabled.stderr
    (state / ".failed-once").unlink(missing_ok=True)
    disable_failed = subprocess.run(
        ["bash", str(script), "--disable"],
        env={**env, "MHB_WIKI_ROUTE_NONCE": "d" * 32, "FAKE_FAIL_PATCH_ONCE": "web-back-api-tls"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert disable_failed.returncode != 0
    for name in ("web-back-api", "web-back-api-tls"):
        match = json.loads((state / f"{name}.json").read_text(encoding="utf-8"))[
            "spec"
        ]["routes"][0]["match"]
        assert match.count("PathPrefix(`/api/wiki`)") == 1

    # A failure while patching the second route restores the exact first match.
    for name in ("web-back-api", "web-back-api-tls"):
        (state / f"{name}.json").write_text(
            json.dumps(_ingress(name)), encoding="utf-8"
        )
    (state / ".failed-once").unlink(missing_ok=True)
    failed = subprocess.run(
        ["bash", str(script)],
        env={**env, "MHB_WIKI_ROUTE_NONCE": "e" * 32, "FAKE_FAIL_PATCH_ONCE": "web-back-api-tls"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed.returncode != 0
    assert "exact-match rollback completed" in failed.stderr
    for name in ("web-back-api", "web-back-api-tls"):
        document = json.loads((state / f"{name}.json").read_text(encoding="utf-8"))
        assert document == _ingress(name)

    (state / ".failed-once").unlink(missing_ok=True)
    rollback_failed = subprocess.run(
        ["bash", str(script)],
        env={
            **env,
            "MHB_WIKI_ROUTE_NONCE": "f" * 32,
            "FAKE_FAIL_PATCH_ONCE": "web-back-api-tls",
            "FAKE_FAIL_ROLLBACK": "web-back-api",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert rollback_failed.returncode != 0
    assert "rollback both failed; operator action required" in rollback_failed.stderr


def test_live_checker_requires_wiki_route_readiness_and_public_probe():
    checker = (ROOT / "ops" / "check-web-back-live.sh").read_text(encoding="utf-8")
    assert 'match.count("PathPrefix(`/api/wiki`)") == 1' in checker
    assert 'body.get("wiki_required") is True' in checker
    assert 'body.get("wiki_live") is True' in checker
    assert "/api/wiki/v1/pages?limit=1" in checker
    assert "internal/wiki/moderation/reports" in checker
    assert 'expected private-only 404' in checker


def test_wiki_release_is_two_phase_and_recovery_gated():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text(
        encoding="utf-8"
    )
    release_path = release[release.index('archive="$release_tmp/source.tar"') :]
    ordered = (
        "run-wiki-release-canary.sh",
        "replace_one web-back-pve-1",
        "replace_one web-back-pve-2",
        "preflight verified DB backup",
        "--enable",
        '"$REPO_ROOT/ops/check-web-back-live.sh"',
        "finalize",
    )
    # Preflight occurs before archive creation; the remaining sequence is
    # canary -> private replicas -> public route/readback -> finalization.
    assert release.index("preflight verified DB backup") < release.index(
        'archive="$release_tmp/source.tar"'
    )
    rollout = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text()
    assert release_path.index("run-wiki-release-canary.sh") < release_path.index('"$remote_rollout_helper" deploy')
    assert rollout.index("replace_one web-back-pve-1") < rollout.index("replace_one web-back-pve-2")
    candidate = release_path.rindex('--candidate')
    finalize = release_path.rindex('finalize "$commit"')
    final_check = release_path.rindex('check-web-back-live.sh" --expected-commit "$commit"')
    assert candidate < finalize < final_check
    assert "restore_drill\") == \"PASS\"" in release
    assert "actual_backup_sha" in release
    assert "env backup is not transaction-bound" in release
    assert "trap rollback_deploy ERR" in rollout
    assert "route_was" in release and "--disable" in release
    assert "--recover-rollback" in release and "--resume-public" in release
    assert "ROLLBACK_FAILED_REQUIRES_OPERATOR" in rollout
    assert "rollback_replicas || true" not in release
    assert "MHB_WIKI_MODERATION_ADMIN_KEY" in release


def test_release_remote_shell_heredocs_parse():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text(
        encoding="utf-8"
    )
    bodies = []
    lines = release.splitlines()
    for index, line in enumerate(lines):
        if "<<'REMOTE'" not in line:
            continue
        end = lines.index("REMOTE", index + 1)
        bodies.append("\n".join(lines[index + 1 : end]) + "\n")
    assert len(bodies) == 3
    for body in bodies:
        result = subprocess.run(
            ["bash", "-n"], input=body, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr


def test_rollout_finalize_is_receipt_first_and_retryable():
    helper = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text(
        encoding="utf-8"
    )
    assert "DONE_ROLLBACK_RETAINED" in helper
    receipt_done = helper.rindex('BEGIN{print "STATUS=DONE"}')
    state_done = helper.rindex("set_status DONE")
    assert receipt_done < state_done
    assert 'if docker container inspect "$backup"' in helper
    assert "ROLLBACK_FAILED_REQUIRES_OPERATOR" in helper
    assert "rollback incomplete" in helper
    assert "rollback boundary already committed" in helper


def test_release_canary_is_disposable_and_covers_all_adapters():
    canary = (
        ROOT / "ops" / "remote" / "run-wiki-release-canary.sh"
    ).read_text(encoding="utf-8")
    required = (
        "browser_session_csrf",
        "agent_bearer",
        "idempotency_replay_conflict",
        "create_edit_stale_cas",
        "history_diff_recent",
        "submit_report",
        "moderation_quarantine_public_exclusion_release_resolve",
        "/internal/wiki/moderation/pages/canary-agent/quarantine",
        "/internal/wiki/moderation/pages/canary-agent/release",
        "cli_live",
        "mcp_initialize_list_live_call",
        'names == {"wiki_get"',
        'await session.call_tool("wiki_get"',
        "result.is_error is False",
        '"production_database_mutated":False',
        '"database_cleanup":"DATA_HELPER_REQUIRED"',
    )
    for marker in required:
        assert marker in canary
    assert 'DROP DATABASE' not in canary
    assert ".isError" not in canary
    assert '-p "127.0.0.1::8000"' in canary
    assert 'docker port "$app_name"' in canary


def test_failed_database_compensation_retains_failure_receipt(tmp_path):
    transaction = "a" * 32
    backup_root = tmp_path / "bootstrap"
    run_dir = backup_root / transaction
    run_dir.mkdir(parents=True)
    receipt = run_dir / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "metahumotonic/wiki-bootstrap-receipt@1",
                "transaction_id": transaction,
                "ownership_nonce": transaction,
                "database": "metahumotonic_wiki",
                "status": "VERIFIED",
                "backup_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    key = tmp_path / "wiki-backup.key"
    key.write_text("evidence", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if "dropdb" in args or "dropuser" in args:
    raise SystemExit(55)
if "psql" in args:
    print("1")
    raise SystemExit(0)
raise SystemExit(70)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    install = fake_bin / "install"
    install.write_text(
        """#!/usr/bin/env python3
import pathlib, sys
pathlib.Path(sys.argv[-1]).mkdir(parents=True, exist_ok=True)
""",
        encoding="utf-8",
    )
    install.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "ops" / "remote" / "provision-wiki-database.sh"),
            "rollback",
            "postgresql",
            "mhb_wiki",
            "metahumotonic_wiki",
            str(backup_root),
            str(key),
        ],
        input=f"{transaction}\n",
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "PASS" not in result.stdout
    assert "evidence retained" in result.stderr
    assert key.exists()
    body = json.loads(receipt.read_text(encoding="utf-8"))
    assert body["status"] == "FAILED_COMPENSATION_REQUIRES_OPERATOR"
    assert body["backup_sha256"] == "b" * 64


def test_wiki_provisioning_has_transactional_compensation_and_restore_drill():
    provision = (ROOT / "ops" / "provision-wiki-storage.sh").read_text(
        encoding="utf-8"
    )
    database = (ROOT / "ops" / "remote" / "provision-wiki-database.sh").read_text(
        encoding="utf-8"
    )
    runtime = (ROOT / "ops" / "remote" / "install-wiki-runtime-env.sh").read_text(
        encoding="utf-8"
    )
    assert "transaction_id=\"$(openssl rand -hex 16)\"" in provision
    assert provision.count(" rollback ") >= 2
    assert "cleanup_failed_create" in database
    assert "pg_dump" in database and "openssl enc -aes-256-cbc" in database
    assert "pg_restore" in database and '"restore_drill": "PASS"' in database
    assert '"key_sha256": sys.argv[9]' in database
    assert 'key_sha="$(sha256sum "$key_file"' in database
    assert "cleanup_failed_install" in runtime
    assert "env_preexisting" in runtime
    assert "wiki-provision-receipt@1" in runtime
    assert "wiki_moderation_admin_key=\"$(openssl rand -hex 32)\"" in provision
    assert 'if [[ "$db_attempted" == true ]]; then' in provision
    assert 'install -d -m 700 -o root -g root' in database
    assert 'root:root:600' in database and 'root:root:700' in database
    assert 'install -d -m 700 -o root -g root' in runtime
    assert "MHB_WIKI_MODERATION_ADMIN_KEY" in runtime


def test_ops_use_remote_mutex_and_nonce_owned_compensation():
    provision = (ROOT / "ops" / "provision-wiki-storage.sh").read_text()
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    lock = (ROOT / "ops" / "remote" / "manage-wiki-operation-lock.sh").read_text()
    database = (ROOT / "ops" / "remote" / "provision-wiki-database.sh").read_text()
    assert "mkdir -- \"$lock_dir\"" in lock and "owner_file" in lock
    for script in (provision, release):
        assert "mhb-wiki-data-operation" in script
        assert "mhb-wiki-runtime-operation" in script
        assert "release_operation_locks" in script
    assert "ownership_nonce" in database
    assert "shobj_description" in database
    assert "refusing database compensation" in database


def test_release_commit_backup_and_digest_are_receipt_bound():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    checker = (ROOT / "ops" / "check-web-back-live.sh").read_text()
    canary_db = (ROOT / "ops" / "remote" / "manage-wiki-canary-database.sh").read_text()
    redis_ref = (ROOT / "ops" / "redis-canary-image.txt").read_text().strip()
    assert "git fetch --prune origin main" in release
    assert 'git cat-file -e "${EXPECTED_COMMIT}^{commit}"' in release
    assert 'if [[ "$CONTROL_MODE" == release ]]' in release
    assert "snapshot\"]==\"current-production\"" in release
    assert "CURRENT_BACKUP_RECEIPT_SHA256" in release
    assert "MIGRATIONS_SHA256" in release
    assert "migration tree changed; use maintenance migration flow" in release
    assert "--expected-commit" in checker
    assert "org.opencontainers.image.revision" in checker
    assert "com.metahumotonic.source-archive-sha256" in checker
    assert "deployment-receipt-${ROLLOUT_NONCE}.env" in (
        ROOT / "ops" / "remote" / "manage-wiki-rollout.sh"
    ).read_text()
    assert "deployment-receipt-{nonce}.env" in checker
    assert "deployment-current.env" in checker
    assert "CURRENT_BACKUP_RECEIPT_SHA256" in checker
    assert '"key_sha256":sys.argv[8]' in (
        ROOT / "ops" / "remote" / "backup-wiki-release-database.sh"
    ).read_text()
    assert 'hashlib.sha256(artifact.read_bytes()).hexdigest()==b[digest_field]' in (
        ROOT / "ops" / "remote" / "backup-wiki-release-database.sh"
    ).read_text()
    assert 'b["key_sha256"]' in release
    assert '"$backup_key_sha"' in release
    assert 'sha256sum "$key_file"' in canary_db
    assert 'test "$(sha256sum "$key_file"' in canary_db
    assert '"metahumotonic_wiki_canary_${commit:0:12}_${nonce:0:12}"' in canary_db
    assert "@sha256:" in redis_ref and len(redis_ref.rsplit("@sha256:", 1)[1]) == 64


def test_rollout_state_and_migration_gate_are_atomic_and_fail_closed():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    rollout = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text()
    assert 'state_tmp="${state_file}.tmp.${rollout_nonce}"' in release
    assert 'CURRENT_BACKUP_RECEIPT_SHA256=$release_backup_receipt_sha' in release
    assert 'mv "$state_tmp" "$state_file"' in release
    assert 'schema-gate-receipt-${rollout_nonce}.json' in release
    assert 'schema-gate-receipt-${ROLLOUT_NONCE}.json' in rollout
    assert '>> /var/lib/metahumotonic-web-back/releases/active-rollout.env' not in release
    assert "sed -i" not in rollout
    assert "com.metahumotonic.wiki-migrations-sha256" in release
    assert 'cd "$release_dir"' in release
    assert "find migrations/wiki" in release
    assert 'find "$release_dir/migrations/wiki"' not in release
    assert 'or "UNLABELED"' in release
    assert 'test "$current_revision" = UNLABELED' in release
    assert 'test "$current_migration_hash" = UNLABELED' in release
    assert "BOOTSTRAP_0_9_1_ROUTE_DISABLED" in release
    assert "maintenance migration flow" in release
    assert '"$remote_rollout_helper" deploy "$commit"' in release
    assert release.index('"$data_canary_helper" drop') < release.rindex('"$remote_rollout_helper" deploy')


def test_canary_db_cleanup_is_exact_receipt_and_owner_bound():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    helper = (ROOT / "ops" / "remote" / "manage-wiki-canary-database.sh").read_text()
    canary = (ROOT / "ops" / "remote" / "run-wiki-release-canary.sh").read_text()
    assert "atomic_receipt RESERVED" in helper
    assert "COMMENT ON DATABASE" in helper
    assert "pg_get_userbyid" in helper and "shobj_description" in helper
    assert "refusing drop" in helper
    assert "atomic_receipt DROPPED" in helper
    assert "DROP DATABASE" not in canary
    assert '127.0.0.1::8000' in canary
    assert 'stat -c \'%U:%G:%a\' "$work_dir"' in canary
    marker = ': >"$canary_db_cleanup_marker"'
    create = '"$data_canary_helper" create'
    drop = '"$data_canary_helper" drop'
    clear = 'rm -f -- "$canary_db_cleanup_marker"'
    assert '[[ -f "$canary_db_cleanup_marker" ]] || return 0' in release
    assert "cleanup_canary_database || status=1" in release
    assert 'if ! ssh -o BatchMode=yes "$RUNTIME_HOST" sudo -n bash -s --' in release
    assert 'cleanup_canary_database || fail "rollout preparation failed' in release
    cleanup_function = release[
        release.index("cleanup_canary_database()") : release.index("cleanup_local()")
    ]
    assert 'if ! ssh -o BatchMode=yes "$DATA_HOST"' in cleanup_function
    assert "return 1" in cleanup_function
    assert cleanup_function.index(drop) < cleanup_function.index(clear)
    assert release.index(marker) < release.index(create)
    assert release.index(create) < release.rindex(drop) < release.rindex(clear)
    assert "canary_db_created" not in release


def test_canary_db_cleanup_marker_survives_failed_exact_drop(tmp_path):
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    cleanup_function = release[
        release.index("cleanup_canary_database()") : release.index("cleanup_local()")
    ]
    marker = tmp_path / "canary-db-cleanup-required"
    base = f"""set -Eeuo pipefail
DATA_HOST=unused
data_canary_helper=unused
canary_db_cleanup_marker="$MARKER"
canary_database=metahumotonic_wiki_canary_deadbeefdead_deadbeefdead
commit={'d' * 40}
operation_id={'e' * 32}
{cleanup_function}
"""
    marker.write_text("required\n", encoding="utf-8")
    failed = subprocess.run(
        ["bash", "-c", base + "ssh() { return 17; }\ncleanup_canary_database\n"],
        env={**os.environ, "MARKER": str(marker)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed.returncode != 0
    assert marker.is_file()

    succeeded = subprocess.run(
        ["bash", "-c", base + "ssh() { return 0; }\ncleanup_canary_database\n"],
        env={**os.environ, "MARKER": str(marker)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert succeeded.returncode == 0, succeeded.stderr
    assert not marker.exists()


def test_final_checker_reads_data_host_receipt_dump_and_key():
    checker = (ROOT / "ops" / "check-web-back-live.sh").read_text()
    assert 'DATA_HOST="${MHB_DATA_HOST' in checker
    assert 'sudo -n sha256sum "$data_receipt"' in checker
    assert 'b["status"]=="VERIFIED"' in checker
    assert 'b["restore_drill"]=="PASS"' in checker
    assert 'sha256sum "$2"' in checker
    assert 'b["key_sha256"]' in checker
    assert 'sha256sum "$3"' in checker
    assert 'actual_key_sha' in checker
    assert "root:root:600" in checker and "root:root:700" in checker


def test_provision_can_compensate_only_owned_uncommented_createdb_gap():
    database = (ROOT / "ops" / "remote" / "provision-wiki-database.sh").read_text()
    assert "is_reserved_uncommented_database" in database
    assert "OWNERSHIP_RESERVED" in database
    assert "pg_get_userbyid" in database
    assert "is_owned role" in database
    assert "test -z \"$marker\"" in database


def test_same_commit_canary_receipts_are_nonce_unique_and_immutable():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    rollout = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text()
    assert 'wiki-release-canary-${rollout_nonce}.json' in release
    assert 'test ! -e "$canary_receipt"' in release
    assert 'rm -f -- "$canary_receipt"' not in release
    assert 'wiki-release-canary-${ROLLOUT_NONCE}.json' in rollout


def test_candidate_checker_validates_complete_state_and_data_before_finalize():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    checker = (ROOT / "ops" / "check-web-back-live.sh").read_text()
    candidate = release.rindex('check-web-back-live.sh" --expected-commit "$commit" --candidate')
    finalize = release.rindex('"$remote_rollout_helper" finalize "$commit"')
    assert candidate < finalize
    assert "active-rollout.env" in checker
    assert "AWAITING_PUBLIC_READBACK" in checker
    assert 'sudo -n sha256sum "$data_receipt"' in checker
    assert 'sha256sum "$2"' in checker


def test_release_preflight_validates_both_existing_replicas_exactly():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    assert "docker inspect web-back-pve-1 web-back-pve-2" in release
    assert 'ids={x["Image"] for x in items}' in release
    assert 'refs={x["Config"]["Image"] for x in items}' in release
    assert 'x["State"]["Running"] is True' in release
    assert 'x["State"]["Health"]["Status"]=="healthy"' in release
    assert 'RestartPolicy' in release and 'PortBindings' in release
    assert 'len(bindings)==1' in release
    assert 'bindings[0]["HostIp"]=="0.0.0.0"' in release
    assert 'grep -qx "IMAGE_ID=$current_image_id"' in release
    assert 'metahumotonic-web-back:0.9.1-x86' in release


def test_replica_topology_rejects_extra_or_noncanonical_bindings():
    rollout = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text()
    checker = (ROOT / "ops" / "check-web-back-live.sh").read_text()
    assert rollout.count('len(bindings)==1') >= 2
    assert rollout.count('bindings[0]["HostIp"]=="0.0.0.0"') >= 2
    assert '-p "0.0.0.0:$port:8000"' in rollout
    assert 'assert len(bindings) == 1' in checker
    assert 'bindings[0]["HostIp"] == "0.0.0.0"' in checker


def test_runtime_canary_cleanup_is_durable_and_exact_owned():
    helper = (ROOT / "ops" / "remote" / "manage-wiki-canary-runtime.sh").read_text()
    canary = (ROOT / "ops" / "remote" / "run-wiki-release-canary.sh").read_text()
    assert "atomic_receipt RESERVED" in helper
    assert "atomic_receipt CLEANUP_REQUIRED" in helper
    assert "atomic_receipt CLEANED" in helper
    assert "owned_container" in helper and "owned_network" in helper
    assert "owned_workdir" in helper
    assert 'test ! -e "$workdir"' in helper
    assert "wiki-runtime-canary-workdir@1" in helper
    assert "foreign runtime canary workdir" in helper
    assert 'mkdir -m 700 -- "$workdir_stage"' in helper
    assert 'mv -T -- "$workdir_stage" "$workdir"' in helper
    assert "cleanup_stage" in helper
    assert "foreign runtime canary staging workdir" in helper
    assert "foreign runtime canary" in helper
    assert 'bash "$runtime_helper" reserve' in canary
    assert 'bash "$runtime_helper" cleanup' in canary
    assert "com.metahumotonic.wiki-canary.commit" in canary
    assert "com.metahumotonic.wiki-canary.nonce" in canary


def test_resume_public_is_status_aware_and_nonce_receipts_are_current_bound():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    rollout = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text()
    checker = (ROOT / "ops" / "check-web-back-live.sh").read_text()
    resume = release[release.rindex('if [[ "$CONTROL_MODE" == resume-public ]]') :]
    awaiting = resume.index("AWAITING_PUBLIC_READBACK)")
    finalizing = resume.index("FINALIZING|DONE_ROLLBACK_RETAINED)")
    done = resume.index("DONE)", finalizing)
    assert awaiting < finalizing < done
    assert resume.index("--candidate", awaiting, finalizing) >= awaiting
    assert "--candidate" not in resume[finalizing:done]
    assert "--candidate" not in resume[done : resume.index("archive=", done)]
    assert 'deployment-receipt-${ROLLOUT_NONCE}.env' in rollout
    assert 'current_pointer="$release_dir/deployment-current.env"' in rollout
    assert 'ROLLOUT_NONCE=$ROLLOUT_NONCE' in rollout
    assert 'active.get("ROLLOUT_NONCE")==nonce' in checker
    assert "validate_deployment_receipt DONE_ROLLBACK_RETAINED" in rollout
    assert "validate_deployment_receipt DONE" in rollout
    assert "retire_previous_current_pointer" in rollout
    assert 'previous-${ROLLOUT_NONCE}.env' in rollout
    assert 'if test -e "$current_pointer"; then validate_current_pointer; fi' in rollout
    receipt_done = rollout.index("validate_deployment_receipt DONE\n", rollout.index("validate_retained_or_done_receipt"))
    pointer_optional = rollout.index('if test -e "$current_pointer"; then validate_current_pointer; fi', receipt_done)
    assert receipt_done < pointer_optional


def test_finalize_crash_snapshots_accept_only_absent_or_exact_current_pointer():
    rollout = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text()
    status_case = rollout[rollout.index('if [[ "$mode" == status ]]') : rollout.index('if [[ "$mode" == deploy ]]')]
    assert "DONE_ROLLBACK_RETAINED) validate_retained_or_done_receipt" in status_case
    retained = rollout[rollout.index("validate_retained_or_done_receipt()") : rollout.index("validate_prior_container()")]
    # Receipt-DONE / pointer-not-yet-moved and pointer-moved / state-not-yet-DONE
    # are both resumable, but a present pointer must be exact.
    assert 'grep -qx \'STATUS=DONE_ROLLBACK_RETAINED\'' in retained
    assert "validate_deployment_receipt DONE" in retained
    assert 'if test -e "$current_pointer"; then validate_current_pointer; fi' in retained
    finalize = rollout[rollout.index('if [[ "$STATUS" == DONE ]]') :]
    assert finalize.index("set_status DONE_ROLLBACK_RETAINED") < finalize.index("retire_previous_current_pointer")
    assert finalize.index('mv -T "$pointer_next" "$current_pointer"') < finalize.rindex("set_status DONE")


def test_rollback_objects_are_exact_bound_and_crash_points_are_recoverable():
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    rollout = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text()
    for field in (
        "PRIOR_IMAGE_ID",
        "PRIOR_IMAGE_REF",
        "PRIOR_REVISION",
        "PRIOR_MIGRATIONS_SHA256",
        "PRIOR_RESTART_POLICY",
        "PRIOR_HEALTH",
        "PRIOR_ONE_PORT",
        "PRIOR_TWO_PORT",
    ):
        assert f"{field}=" in release
        assert field in rollout
    deploy = rollout[rollout.index('if [[ "$mode" == deploy ]]') : rollout.index('if [[ "$mode" == rollback ]]')]
    assert deploy.index('! docker container inspect "$BACKUP_ONE"') < deploy.index("replace_one()")
    assert deploy.index('! docker container inspect "$BACKUP_TWO"') < deploy.index("replace_one()")
    rollback = rollout[rollout.index('if [[ "$mode" == rollback ]]') :]
    assert 'validate_prior_container web-back-pve-1 web-back-pve-1 "$PRIOR_ONE_PORT" any' in rollback
    assert 'validate_candidate_container web-back-pve-1 "$PRIOR_ONE_PORT" identity-only' in rollback
    assert 'if docker container inspect web-back-pve-1' in rollback
    assert rollback.index('validate_backup "$BACKUP_ONE"') < rollback.index('docker stop "$name"')
    assert "validate_candidate_container" in rollout


def test_canary_stage_conflicts_are_durable_and_never_broad_cleaned():
    helper = (ROOT / "ops" / "remote" / "manage-wiki-canary-runtime.sh").read_text()
    assert "umask 077" in helper
    assert "REFUSED_WORKDIR_CONFLICT" in helper
    assert "REFUSED_STAGE_CONFLICT" in helper
    assert "REFUSED_STAGE_CREATE" in helper
    assert "workdir_identity" in helper
    assert "stat -c '%d:%i'" in helper
    assert 'test -e "$workdir_stage_marker"' in helper
    assert "find \"$workdir_stage\"" not in helper
    assert 'case "$receipt_status" in' in helper
    assert "stage preserved for inspection" in helper


def test_runtime_env_install_is_receipt_first_and_digest_guarded():
    helper = (ROOT / "ops" / "remote" / "install-wiki-runtime-env.sh").read_text()
    for phase in ("RESERVED", "BACKUP_STAGED", "BACKUP_CREATED", "ENV_STAGED", "ENV_INSTALLED", "INSTALLED"):
        assert f"atomic_receipt {phase}" in helper
    assert helper.index("atomic_receipt RESERVED") < helper.index('>"$backup_stage"')
    assert helper.index("atomic_receipt BACKUP_STAGED") < helper.index('mv -T "$backup_stage" "$env_backup"')
    install_env_mv = helper.rindex('mv -T "$env_stage" "$env_file"')
    assert helper.index("atomic_receipt ENV_STAGED") < install_env_mv
    assert "original_env_sha256" in helper and "installed_env_sha256" in helper
    assert "refusing to overwrite foreign runtime env digest" in helper
    assert "ENV_STAGED|FAILED_COMPENSATION_REQUIRES_OPERATOR" in helper
    assert "ENV_INSTALLED|INSTALLED" in helper


def test_route_transaction_is_nonce_bound_and_crash_resumable():
    route = (ROOT / "ops" / "enable-wiki-route-vm100.sh").read_text()
    release = (ROOT / "ops" / "release-web-back-vm100.sh").read_text()
    rollout = (ROOT / "ops" / "remote" / "manage-wiki-rollout.sh").read_text()
    assert "wiki-route-transaction@1" in route
    assert "resource_version" in route and "prior_sha256" in route and "target_sha256" in route
    assert '"phase":"RESERVED"' in route
    assert 'route_receipt_status "PATCHED_${name}"' in route
    assert "route_receipt_status DONE" in route
    assert 'match in {x["prior_match"],x["target_match"]}' in route
    assert "ROUTE_ENABLE_NONCE=$rollout_nonce" in release
    assert "ROUTE_ROLLBACK_NONCE=$route_rollback_nonce" in release
    assert "route_rollback_nonce=" in rollout
    assert 'MHB_WIKI_ROUTE_NONCE="$active_nonce"' in release
    assert 'MHB_WIKI_ROUTE_NONCE="$route_rollback_nonce"' in release
