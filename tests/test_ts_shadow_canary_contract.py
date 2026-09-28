from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shadow_canary_limits_env_and_forbids_public_or_write_paths() -> None:
    controller = (ROOT / "ops" / "run-ts-shadow-canary-vm100.sh").read_text(encoding="utf-8")
    remote = (ROOT / "ops" / "remote" / "run-ts-shadow-canary.sh").read_text(encoding="utf-8")
    assert "--dry-run" in controller
    assert "git merge-base --is-ancestor" in controller
    assert "--network \"container:${legacy}\"" in remote
    assert "--read-only --cap-drop ALL --security-opt no-new-privileges" in remote
    assert "MHB_SHADOW_READ_ONLY=true" in remote
    assert "MHB_PLATFORM_WRITE_KEY=" in remote and "MHB_KG_WRITE_KEY=" in remote
    assert "MHB_FEEDBACK_ADMIN_KEY=" in remote
    assert "MHB_WIKI_DATABASE_URL" not in remote
    assert "MHB_WIKI_SESSION_SECRET" not in remote
    assert "MHB_ONTOLOGY_MCP_URL" not in remote and "MHB_HSPINE_MCP_URL" not in remote
    assert "publicIngressChanged\":false" in remote
    assert "databaseWrites\":\"not-proven-by-canary\"" in remote
    assert "NEO4J_PASSWORD" in remote and "substr($0,at+1)" in remote
    assert "archive ownership or digest mismatch" in remote
    assert "POST', headers" in remote  # verifies the process itself gets a blocked mutation probe


def test_shadow_mode_blocks_http_mutation_and_feedback_initialization() -> None:
    boundary = (ROOT / "ts" / "src" / "server" / "PlatformBoundary.ts").read_text(encoding="utf-8")
    feedback = (ROOT / "ts" / "src" / "ports" / "FeedbackStoreMongo.ts").read_text(encoding="utf-8")
    assert 'cfg.shadowReadOnly && !["GET", "HEAD", "OPTIONS"].includes(request.method)' in boundary
    assert 'reason: "shadow_read_only"' in boundary
    assert 'Config.boolean("MHB_SHADOW_READ_ONLY")' in feedback
    assert "if (shadowReadOnly)" in feedback
    assert "ensureIndexes: Effect.void" in feedback


def test_shadow_controller_dry_run_has_no_remote_side_effect(tmp_path: Path) -> None:
    commit = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=ROOT, text=True).strip()
    result = subprocess.run(
        ["bash", str(ROOT / "ops" / "run-ts-shadow-canary-vm100.sh"), "--dry-run", commit],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS shadow dry-run" in result.stdout
