from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "ops/remote/run-ts-wiki-stateful-canary.sh"
CONTROLLER = ROOT / "ops/run-ts-wiki-stateful-canary-vm100.sh"
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
        'org.opencontainers.image.revision',
    ]:
        assert expected in source
    assert '"production_database_mutated":False' in source
    assert 'work_dir="/var/lib/metahumotonic-web-back/releases/$commit/.canary-$short"' in source
    assert "f'MHB_WIKI_SESSION_SECRET={secrets.token_hex(32)}'" in source
    assert "f'MHB_WIKI_MODERATION_ADMIN_KEY={secrets.token_hex(32)}'" in source
    assert 'docker image rm "$gateway_image"' not in source
    assert "gateway_image_id" in source
    assert "PASS existing commit/image-bound synthetic canary receipt" in source

def test_standalone_controller_never_calls_public_release_or_builds_images():
    source = CONTROLLER.read_text()
    subprocess.run(["bash", "-n", str(CONTROLLER)], check=True)
    assert "release-web-back-vm100.sh" not in source
    assert "docker build" not in source
    assert "docker image rm" not in source
    assert "manage-wiki-canary-runtime.sh" in source
    assert 'mode="${1:-dry-run}"' in source
    assert "private-wiki-stateful-receipts" in source
    assert "metahumotonic_wiki_canary_${commit:0:12}_${nonce:0:12}" in source
    assert "bash '$runtime' cleanup" in source
    assert "mktemp -d /var/tmp/mhb-wiki-stateful.XXXXXX" in source
    assert "verify-restored" in source
    assert "releases/$commit" in source
    assert "cleanup_gateway" in source
    assert "sudo -n docker rm -f '$gateway_name'" in source
    assert "sudo -n rm -rf" not in source
    assert "Dockerfile.legacy" not in RELEASE.read_text().split('run-wiki-release-canary.sh')[0]
