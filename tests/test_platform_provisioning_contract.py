from pathlib import Path

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

def test_platform_runtime_role_cannot_receive_ddl_update_or_delete() -> None:
    remote = (ROOT / "ops/remote/provision-platform-database.sh").read_text()
    grants = remote[remote.index('GRANT USAGE ON SCHEMA'):remote.index('emit grant-runtime')]
    assert 'GRANT SELECT ON ALL TABLES' in grants and 'GRANT INSERT ON mhb_platform.observations,mhb_platform.ingest_receipts' in grants
    assert 'UPDATE' not in grants and 'DELETE' not in grants and 'CREATE' not in grants
    assert 'qd "SELECT count(*) FROM information_schema.tables' in remote
