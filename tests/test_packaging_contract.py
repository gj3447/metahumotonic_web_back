from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_container_installs_only_hash_locked_runtime_dependencies() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
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
