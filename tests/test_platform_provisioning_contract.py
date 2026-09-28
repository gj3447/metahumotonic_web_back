from pathlib import Path
import os
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]

def test_platform_bootstrap_is_dedicated_and_dry_run_by_default() -> None:
    remote = (ROOT / "ops/remote/provision-platform-database.sh").read_text()
    controller = (ROOT / "ops/provision-platform-storage.sh").read_text()
    assert 'database="metahumotonic_platform"' in remote
    assert 'owner="mhb_platform_owner"' in remote and 'runtime="mhb_platform_runtime"' in remote
    assert 'dry-run|status|apply|grant-runtime|rollback-empty' in controller
    assert 'metahumotonic_wiki' not in remote and 'mhb_wiki' not in remote
    assert 'rollback-empty refuses destructive drop' in remote
    assert 'secretMaterialPrinted' in remote and 'MHB_PLATFORM_DATABASE_URL' not in remote
    assert 'baseline.dump.enc' in remote and 'aes-256-cbc' in remote
    assert 'chmod 600 "$root/baseline.dump.enc"' in remote
    assert 'FAILED_RECOVERY_REQUIRES_OPERATOR' in remote
    assert 'tagged_roles_without_database()' in remote
    assert "role_has_owned_objects()" in remote
    assert 'if empty_owned_database; then' in remote
    assert 'elif tagged_roles_without_database; then' in remote
    assert 'BEGIN;' in remote and 'COMMIT;' in remote

def test_platform_runtime_role_cannot_receive_ddl_update_or_delete() -> None:
    remote = (ROOT / "ops/remote/provision-platform-database.sh").read_text()
    grants = remote[remote.index('GRANT USAGE ON SCHEMA'):remote.index('emit grant-runtime')]
    assert 'GRANT SELECT ON ALL TABLES' in grants and 'GRANT INSERT ON mhb_platform.observations,mhb_platform.ingest_receipts' in grants
    assert 'UPDATE' not in grants and 'DELETE' not in grants and 'CREATE' not in grants
    assert 'qd "SELECT count(*) FROM information_schema.tables' in remote


def test_apply_failure_compensates_tagged_empty_roles_without_database(tmp_path: Path) -> None:
    """A createdb failure must not strand the bootstrap roles or a receipt."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.write_text("owner=0\nruntime=0\n")
    (fake_bin / "docker").write_text(
        """#!/usr/bin/env bash
set -eu
source "$FAKE_DOCKER_STATE"
save() { printf 'owner=%s\\nruntime=%s\\n' "$owner" "$runtime" >"$FAKE_DOCKER_STATE"; }
case "$1" in inspect) exit 0;; esac
args="$*"
if [[ "$args" == *"createdb"* ]]; then exit 1; fi
if [[ "$args" == *"dropuser"* ]]; then
  [[ "$args" == *"mhb_platform_runtime"* ]] && runtime=0 || owner=0
  save; exit 0
fi
if [[ "$args" == *"-Atqc"* ]]; then
  query="${!#}"
  case "$query" in
    *"rolname='mhb_platform_owner'"*) echo "$owner";;
    *"rolname='mhb_platform_runtime'"*) echo "$runtime";;
    *"pg_database WHERE datname"*) echo 0;;
    *"shobj_description"*) echo 'metahumotonic-platform-bootstrap@1';;
    *"pg_shdepend"*) echo 0;;
    *) echo 0;;
  esac
  exit 0
fi
if [[ "$args" == *"psql"* ]]; then cat >/dev/null; owner=1; runtime=1; save; exit 0; fi
exit 0
"""
    )
    (fake_bin / "docker").chmod(0o755)
    root, secrets = tmp_path / "bootstrap", tmp_path / "secrets"
    result = subprocess.run(
        [str(ROOT / "ops/remote/provision-platform-database.sh"), "apply", "postgresql", str(root), str(secrets)],
        input="a" * 64 + "\n", text=True, capture_output=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_DOCKER_STATE": str(state)},
    )
    assert result.returncode == 1  # createdb failed
    assert state.read_text() == "owner=0\nruntime=0\n"
    assert not (root / "receipt.json").exists()


def test_platform_migration_controller_is_exact_commit_and_secret_free() -> None:
    controller = (ROOT / "ops/migrate-platform-storage.sh").read_text()
    remote = (ROOT / "ops/remote/migrate-platform-catalog.sh").read_text()
    assert 'mode="${1:-status}"' in controller
    assert 'status --porcelain --untracked-files=all' in controller
    assert 'merge-base --is-ancestor "$commit" origin/main' in controller
    assert 'git -C "$root" show "$commit:ts/config/platform-catalog.json"' in controller
    assert 'git -C "$root" cat-file -e "$commit^{commit}"' in controller
    assert 'MHB_PLATFORM_IMAGE must be a digest reference or image ID' in controller
    assert 'org.opencontainers.image.revision' in remote
    assert 'stage-image' in remote and 'catalog artifact digest mismatch' in remote
    assert '"$image" != sha256:*' in remote
    assert 'expected == result' in remote
    assert 'databaseWrites":false' in remote
    assert 'remote_dir="/var/tmp/mhb-platform-migrate-$nonce"' in controller
    assert 'test ! -L' in controller and 'stat -c %a' in controller


def test_local_image_stage_is_exact_and_db_free() -> None:
    stage = (ROOT / "ops/stage-platform-image-local.sh").read_text()
    remote = (ROOT / "ops/remote/migrate-platform-catalog.sh").read_text()
    assert 'mode="${1:-dry-run}"' in stage
    assert 'git -C "$root" archive --format=tar' in stage and 'source.tar' in stage
    assert 'docker save' in stage and 'docker load' in stage
    assert 'source-archive-sha256' in stage and 'databaseWrites":false' in stage
    assert 'source.tar' in stage and 'sudo -n docker load' in stage
    assert 'sudo -n docker version' in stage
    assert 'sudo -n docker image inspect' in stage
    assert 'rm -rf -- \'$vm_dir\'' in stage
    assert '--read-only' in remote and '--cap-drop ALL' in remote
    assert 'no-new-privileges' in remote and '--pids-limit 128' in remote
    assert 'VERIFIED_BEFORE_RUNTIME_GRANT' in remote
    assert '"$provision" grant-runtime' in remote
    assert '-d "$database" -c "REASSIGN OWNED BY $manager TO $owner; DROP OWNED BY $manager"' in remote
    assert '-d postgres -c "REVOKE $owner FROM $manager; DROP ROLE $manager"' in remote
    assert remote.index('if ! cleanup_manager; then') < remote.rindex('"$provision" grant-runtime')
    assert remote.rindex('"$provision" grant-runtime') < remote.rindex('"status":"PASS"')
    assert 'MHB_PLATFORM_DATABASE_URL=' in remote
    assert 'secretMaterialPrinted' in remote and 'wikiTouched' in remote


def test_migrator_role_cleanup_real_postgres_contract() -> None:
    """Exercise the role-handoff SQL against PostgreSQL when explicitly enabled.

    CI environments without a local Postgres image keep the normal suite
    hermetic; data-01 staging enables this with MHB_RUN_POSTGRES_CONTRACT=1.
    """
    if os.environ.get("MHB_RUN_POSTGRES_CONTRACT") != "1":
        import pytest
        pytest.skip("set MHB_RUN_POSTGRES_CONTRACT=1 with Docker and postgres:16-alpine staged")
    if subprocess.run(["docker", "image", "inspect", "postgres:16-alpine"], capture_output=True).returncode:
        raise AssertionError("postgres:16-alpine must be staged for the real cleanup contract")
    name = f"mhb-cleanup-{uuid.uuid4().hex[:12]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test", "postgres:16-alpine"], check=True, capture_output=True, text=True)
    try:
        for _ in range(45):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise AssertionError("temporary PostgreSQL did not become ready")
        sql = """
CREATE ROLE mhb_platform_owner NOLOGIN;
CREATE ROLE mhb_platform_migrator LOGIN;
GRANT mhb_platform_owner TO mhb_platform_migrator;
CREATE DATABASE metahumotonic_platform OWNER mhb_platform_owner;
"""
        subprocess.run(["docker", "exec", "-i", name, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres"], input=sql, check=True, text=True, capture_output=True)
        subprocess.run(["docker", "exec", "-i", name, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "mhb_platform_migrator", "-d", "metahumotonic_platform"], input="CREATE TABLE migrator_owned (id integer);", check=True, text=True, capture_output=True)
        cleanup = "REASSIGN OWNED BY mhb_platform_migrator TO mhb_platform_owner; DROP OWNED BY mhb_platform_migrator;"
        subprocess.run(["docker", "exec", "-i", name, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "metahumotonic_platform", "-c", cleanup], check=True, text=True, capture_output=True)
        subprocess.run(["docker", "exec", "-i", name, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", "-c", "REVOKE mhb_platform_owner FROM mhb_platform_migrator; DROP ROLE mhb_platform_migrator"], check=True, text=True, capture_output=True)
        result = subprocess.run(["docker", "exec", name, "psql", "-X", "-At", "-U", "postgres", "-d", "metahumotonic_platform", "-c", "SELECT tableowner FROM pg_tables WHERE tablename='migrator_owned'; SELECT count(*) FROM pg_roles WHERE rolname='mhb_platform_migrator';"], check=True, text=True, capture_output=True)
        assert result.stdout.splitlines() == ["mhb_platform_owner", "0"]
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
