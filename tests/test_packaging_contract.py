from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_default_image_runs_typescript_and_compose_keeps_domains_private() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'CMD ["node", "dist/src/main.js"]' in dockerfile
    assert "npm ci" in dockerfile
    assert "USER node" in dockerfile
    assert "dist/config" in dockerfile
    assert "http://legacy-domains:8000" in compose
    domains = compose.split("  legacy-domains:\n", 1)[1]
    assert "Dockerfile.legacy" in domains
    assert "    ports:" not in domains


def test_container_installs_only_hash_locked_runtime_dependencies() -> None:
    dockerfile = (ROOT / "Dockerfile.legacy").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.lock").read_text(encoding="utf-8")

    assert "COPY requirements.lock" in dockerfile
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "pip install --no-cache-dir ." not in dockerfile
    assert "pip install --no-cache-dir --no-deps ." not in dockerfile

    pinned_lines = [
        line
        for line in requirements.splitlines()
        if line and not line.startswith(("#", " ", "\\"))
    ]
    assert pinned_lines
    assert all("==" in line for line in pinned_lines)
    assert "--hash=sha256:" in requirements
