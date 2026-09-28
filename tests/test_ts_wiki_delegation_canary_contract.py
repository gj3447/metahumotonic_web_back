from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/remote/run-ts-backend-canary.sh"
DOC = ROOT / "docs/WIKI_DELEGATION_PARITY.md"

def test_private_canary_has_bounded_fixed_read_delegation_parity_contract():
    source = SCRIPT.read_text()
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    for path in ["/api/wiki/v1", "/api/wiki/v1/pages?limit=1", "/api/wiki/v1/pages/mhb-read-parity-missing", "/api/v1/ontology/schema"]:
        assert path in source
    assert "readOnlyParity(path, 'GET')" in source
    assert "readOnlyParity(path, 'HEAD')" in source
    assert "delegation body mismatch" in source
    assert "delegated response exceeds parity bound" in source
    assert "x-mhb-service" in source
    assert '"wiki-ontology-read-parity"' in source

def test_document_states_stateful_gaps_and_no_public_cutover_claim():
    text = DOC.read_text()
    for phrase in ["session issuance", "CSRF", "idempotency replay", "Redis outage", "disposable Wiki database", "Python remains"]:
        assert phrase in text
