from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "ops/remote/run-wiki-release-canary.sh"
RELEASE = ROOT / "ops/release-web-back-vm100.sh"

def test_stateful_delegation_is_exact_image_bound_and_disposable_only():
    source = CANARY.read_text()
    subprocess.run(["bash", "-n", str(CANARY)], check=True)
    for expected in [
        'gateway_image="${9:-}"', 'gateway_name="mhb-wiki-canary-gateway-$short"',
        'MHB_LEGACY_ORIGIN=http://${app_name}:8000', 'MHB_MONGO_URI=',
        'MHB_NEO4J_LIVE=false', 'MHB_REDIS_URL=', '--read-only', '--cap-drop ALL',
        'cleanup_gateway', 'ts_delegated_browser_session_csrf',
        'ts_delegated_agent_idempotency_cas_direct_readback', 'MHB_GATEWAY_URL',
    ]:
        assert expected in source
    assert '"production_database_mutated":False' in source

def test_release_builds_and_removes_commit_labeled_ts_gateway_image():
    source = RELEASE.read_text()
    assert 'gateway_image="metahumotonic-web-back-ts:${version}-x86"' in source
    assert 'docker build --file "$release_dir/Dockerfile"' in source
    assert '"org.opencontainers.image.revision=$commit"' in source
    assert 'cleanup_gateway' in CANARY.read_text()
    assert 'docker image rm "$gateway_image"' in CANARY.read_text()
