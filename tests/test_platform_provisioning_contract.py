from pathlib import Path
import os
import subprocess

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
