from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys


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
    joined = "\n".join(path.read_text(encoding="utf-8") for path in ACTIVE_OPERATIONS_DOCS)
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
    assert "docs/OPERATIONS_VM100.md" in (ROOT / "README.md").read_text(encoding="utf-8")


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
