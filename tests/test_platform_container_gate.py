"""Cleanup ownership and missing-runtime behavior, without touching Docker."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("container_gate", ROOT / "scripts/check-platform-containers.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_foreign_container_is_never_removed(monkeypatch):
    docker = gate.Docker("owned-nonce")
    docker.containers.append("foreign-container-id")
    monkeypatch.setattr(docker, "inspect", lambda *_args: {gate.OWNER: "another-run"})
    commands = []
    monkeypatch.setattr(docker, "run", lambda *args, **_kwargs: commands.append(args))
    assert docker.cleanup() == [{"kind": "container", "id": "foreign-container-id"}]
    assert commands == []


def test_partial_cleanup_remains_a_failure_and_continues_other_resources(monkeypatch):
    docker = gate.Docker("owned-nonce")
    docker.containers.extend(["first", "second"])
    monkeypatch.setattr(docker, "inspect", lambda *_args: {gate.OWNER: "owned-nonce"})
    commands = []

    def run(*args, **_kwargs):
        commands.append(args)
        if args[:2] == ("container", "rm") and args[-1] == "second":
            raise RuntimeError("injected removal failure")
        return ""

    monkeypatch.setattr(docker, "run", run)
    assert docker.cleanup() == [{"kind": "container", "id": "second"}]
    assert ("container", "rm", "--force", "--volumes", "first") in commands


def test_docker_success_without_removal_is_not_cleanup_success(monkeypatch):
    docker = gate.Docker("owned-nonce")
    docker.network = "owned-network"
    monkeypatch.setattr(docker, "inspect", lambda *_args: {gate.OWNER: "owned-nonce"})
    monkeypatch.setattr(docker, "run", lambda *args, **_kwargs: "owned-network" if args[1] == "ls" else "")
    assert docker.cleanup() == [{"kind": "network", "id": "owned-network"}]


def test_missing_engine_is_not_run_and_cannot_overwrite_a_receipt(monkeypatch, tmp_path):
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr("sys.argv", ["gate", "--receipt", str(receipt)])
    monkeypatch.setattr(gate.shutil, "which", lambda _name: None)
    assert gate.main() == 2
    before = receipt.read_bytes()
    document = json.loads(before)
    assert document["status"] == "NOT_RUN"
    assert document["checks"] == []
    with pytest.raises(FileExistsError):
        gate.main()
    assert receipt.read_bytes() == before


def test_remote_engine_is_refused_before_build(monkeypatch, tmp_path):
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr("sys.argv", ["gate", "--receipt", str(receipt)])
    monkeypatch.setattr(gate.shutil, "which", lambda _name: "/fixture/docker")
    monkeypatch.setenv("DOCKER_HOST", "ssh://production.invalid")
    commands = []

    def run(_self, *args, **_kwargs):
        commands.append(args)
        return "fixture-context"

    monkeypatch.setattr(gate.Docker, "run", run)
    assert gate.main() == 1
    assert json.loads(receipt.read_text())["status"] == "FAIL"
    assert commands == [("context", "show")]
