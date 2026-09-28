from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def text(path: str) -> str:
    return (ROOT / path).read_text()

def test_read_canary_never_publishes_or_configures_other_datastores():
    script = text("ops/remote/run-ts-platform-read-canary.sh")
    assert "--network \"container:$container\"" in script
    assert "--publish" not in script and "-p " not in script
    for expected in ["MHB_MONGO_URI=", "MHB_REDIS_URL=", "MHB_NEO4J_LIVE=false", "MHB_LEGACY_REQUIRED=false", "MHB_SHADOW_READ_ONLY=true", "--read-only", "--cap-drop ALL", "no-new-privileges"]:
        assert expected in script

def test_reader_contract_is_root_secret_and_checks_denied_privileges():
    script = text("ops/remote/provision-platform-read-canary-role.sh")
    for expected in ["mhb_platform_shadow_reader", "NOBYPASSRLS", "NOINHERIT", "has_database_privilege", "has_schema_privilege", "has_table_privilege", "privilegeDenialVerified", "root -g root", "REVOKE ALL PRIVILEGES ON DATABASE", "drop_owned_reader", "datdba"]:
        assert expected in script
    assert "GRANT INSERT" not in script

def test_scripts_parse_and_controller_requires_exact_id_commit():
    for path in ["ops/provision-platform-read-canary-role.sh", "ops/remote/provision-platform-read-canary-role.sh", "ops/run-ts-platform-read-canary-data01.sh", "ops/remote/run-ts-platform-read-canary.sh"]:
        subprocess.run(["bash", "-n", str(ROOT / path)], check=True)
    controller = text("ops/run-ts-platform-read-canary-data01.sh")
    assert "^sha256:[0-9a-f]{64}$" in controller
    assert "git -C \"$repo_root\" archive" in controller
    assert "git -C \"$repo_root\" show \"$commit:ops/remote/run-ts-platform-read-canary.sh\"" in controller
    assert "MHB_PLATFORM_READ_CANARY_NONCE" in controller
    remote = text("ops/remote/run-ts-platform-read-canary.sh")
    assert "container_is_owned" in remote and "docker logs" not in remote
