#!/usr/bin/env python3
"""Build and verify an isolated company backend stack; never use production env.

Requires a working Docker engine on an integration worker. Own containers have
random names, exact owner labels, resource limits and an internal network. Only
temporary loopback ports are published. A receipt is PASS only after cleanup.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
OWNER = "com.metahumotonic.integration"
OPENER = build_opener(ProxyHandler({}))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Docker:
    def __init__(self, nonce):
        self.nonce = nonce
        self.containers = []
        self.network = None
        self.images = []

    def run(self, *args, timeout=60):
        result = subprocess.run(["docker", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        # Raw inspect/build output can contain env values. Only bounded JSON
        # selections, IDs and test assertions belong in the receipt.
        require(result.returncode == 0, "docker operation failed: " + args[0])
        return result.stdout.strip()

    def inspect(self, kind, identity, selection):
        return json.loads(self.run(kind, "inspect", "--format", "{{json " + selection + "}}", identity))

    def assert_owner(self, kind, identity):
        path = ".Labels" if kind == "network" else ".Config.Labels"
        labels = self.inspect(kind, identity, path) or {}
        require(isinstance(labels, dict) and labels.get(OWNER) == self.nonce, "foreign Docker resource; refusing mutation")

    def container(self, role, image, *args):
        name = f"mhb-check-{self.nonce}-{role}"
        identity = self.run("create", "--name", name, "--label", f"{OWNER}={self.nonce}",
                            "--network", self.network, "--network-alias", role,
                            "--memory", "512m", "--cpus", "1", "--pids-limit", "128", *args, image)
        self.containers.append(identity)
        self.run("start", identity)
        return identity

    def cleanup(self):
        failures = []
        for kind, identity in [("container", x) for x in reversed(self.containers)] + ([("network", self.network)] if self.network else []):
            try:
                self.assert_owner(kind, identity)
                self.run(kind, "rm", *(["--force", "--volumes"] if kind == "container" else []), identity)
                remaining = self.run(kind, "ls", "-q", *( ["--all"] if kind == "container" else []), "--filter", f"id={identity}")
                require(not remaining, "resource remains after cleanup")
            except (RuntimeError, subprocess.TimeoutExpired):
                failures.append({"kind": kind, "id": identity})
        for image in self.images:
            try:
                self.assert_owner("image", image)
                self.run("image", "rm", image)
            except (RuntimeError, subprocess.TimeoutExpired):
                failures.append({"kind": "image", "id": image})
        return failures


def http(origin, path, *, body=None, key=None, method=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(origin + path, headers=headers, data=None if body is None else json.dumps(body).encode(), method=method)
    try:
        response = OPENER.open(request, timeout=8)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(1_048_577)
        require(len(raw) <= 1_048_576, "oversized integration response")
        return response.status, json.loads(raw) if raw else None, response.headers


def await_ready(origin, marker):
    for _ in range(90):
        try:
            if http(origin, "/health")[1].get("version") == marker and http(origin, "/ready")[0] == 200:
                return
        except (URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError("container readiness deadline exceeded")


def source_digest():
    files = set()
    for path in ["Dockerfile", "Dockerfile.legacy", ".dockerignore", "requirements.lock", "README.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
                 "ts/package.json", "ts/package-lock.json", "ts/.node-version", "ts/tsconfig.json", "scripts/check-platform-containers.py"]:
        files.add(ROOT / path)
    for directory in ["app", "migrations", "ts/src", "ts/test", "ts/config", "ts/scripts"]:
        files.update(p for p in (ROOT / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return hashlib.sha256(json.dumps({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted(files)}, sort_keys=True).encode()).hexdigest()


def verify_stack(docker, work, receipt):
    nonce = docker.nonce
    for role, dockerfile in [("platform", "Dockerfile"), ("domains", "Dockerfile.legacy")]:
        image = f"mhb-check-{role}:{nonce}"
        docker.run("build", "--label", f"{OWNER}={nonce}", "-f", dockerfile, "-t", image, ".", timeout=900)
        docker.images.append(image)
        receipt["images"][role] = docker.inspect("image", image, ".Id")
    for image in ["mongo:7.0.24", "redis:8.0.2"]:
        docker.run("pull", image, timeout=300)
        receipt["images"][image] = docker.inspect("image", image, ".Id")
    docker.network = docker.run("network", "create", "--internal", "--label", f"{OWNER}={nonce}", f"mhb-check-{nonce}")
    mongo = docker.container("mongo", "mongo:7.0.24")
    redis = docker.container("redis", "redis:8.0.2")
    for _ in range(60):
        try:
            require(docker.run("exec", mongo, "mongosh", "--quiet", "--eval", "db.runCommand({ping:1}).ok") == "1", "mongo not ready")
            require(docker.run("exec", redis, "redis-cli", "ping") == "PONG", "redis not ready")
            break
        except RuntimeError:
            time.sleep(0.5)
    else:
        raise RuntimeError("isolated stores did not start")
    legacy = docker.container("legacy", docker.images[1], "--env", "MHB_NEO4J_LIVE=false")
    require(not docker.inspect("container", legacy, ".HostConfig.PortBindings"), "legacy published a host port")
    key, admin = uuid.uuid4().hex + uuid.uuid4().hex, uuid.uuid4().hex + uuid.uuid4().hex
    env = {"MHB_HOST": "0.0.0.0", "MHB_PORT": "8000", "MHB_VERSION": nonce,
           "MHB_NEO4J_LIVE": "false", "MHB_LEGACY_ORIGIN": "http://legacy:8000", "MHB_LEGACY_REQUIRED": "true",
           "MHB_MONGO_URI": "mongodb://mongo:27017", "MHB_MONGO_DB": "mhb_container_check",
           "MHB_REDIS_URL": "redis://redis:6379", "MHB_FEEDBACK_REQUIRE_DURABLE": "true",
           "MHB_FEEDBACK_MAX_PER_WINDOW": "2", "MHB_PLATFORM_READ_KEY": key, "MHB_FEEDBACK_ADMIN_KEY": admin}
    env_file = work / "synthetic.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    env_file.chmod(0o600)
    origins, replicas = [], []
    for index in range(2):
        identity = docker.container(f"platform-{index}", docker.images[0], "--env-file", str(env_file), "--publish", "127.0.0.1::8000")
        replicas.append(identity)
        bindings = docker.inspect("container", identity, '.NetworkSettings.Ports')["8000/tcp"]
        require(len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1", "non-loopback published port")
        origin = "http://127.0.0.1:" + bindings[0]["HostPort"]
        origins.append(origin)
        await_ready(origin, nonce)
        require(docker.inspect("container", identity, ".Image") == receipt["images"]["platform"], "wrong running image")
        require(docker.run("exec", identity, "id", "-u") != "0", "entrypoint runs as root")
        require(http(origin, "/api/platform/v1/services")[0] == 401, "anonymous company API accepted")
        require(http(origin, "/api/platform/v1/services", key=key)[0] == 200, "company API missing")
        catalog = http(origin, "/api/public/v1/hub")[1]
        ld_status, linked, headers = http(origin, "/api/public/v1/hub/graph.jsonld")
        require(ld_status == 200 and headers.get_content_type() == "application/ld+json", "missing linked-data route")
        require(catalog["sourceDigest"] == linked["mh:sourceDigest"], "different public graph identities")
        usl = http(origin, "/api/public/v1/hub/usl.json")[1]
        require(len(usl["nodes"]) == len(catalog["nodes"]) and len(usl["relations"]) == len(catalog["edges"]), "missing graph projection")
    receipt["checks"].extend(["two-exact-image-replicas", "non-root-entrypoint", "required-domain-ready", "private-legacy-port", "platform-auth", "public-graph-parity"])
    # Read the Python owner from inside its container, independently of the TS proxy.
    for path in ["/api/wiki/v1", "/api/wiki/v1/pages"]:
        direct = json.loads(docker.run("exec", legacy, "python", "-c", "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000" + path + "'))))"))
        proxied = http(origins[0], path)
        require(proxied[0] == 200 and proxied[1] == direct and proxied[2].get("x-mhb-service") == "legacy-domain", "private domain parity failed")
    receipt["checks"].append("wiki-owner-readback")
    ids = []
    for origin in origins:
        status, result, _ = http(origin, "/api/feedback", body={"subject": "Container readback", "body": nonce})
        require(status == 200 and result.get("status") == "stored", "feedback was not durable")
        ids.append(result["id"])
    stored = json.loads(docker.run("exec", mongo, "mongosh", "--quiet", "mhb_container_check", "--eval",
        'JSON.stringify(db.web_feedback.find({}, {_id:1,body:1}).toArray())'))
    require({row["_id"] for row in stored} == set(ids) and all(row["body"] == nonce for row in stored), "owner Mongo readback differs")
    windows = json.loads(docker.run("exec", redis, "redis-cli", "--json", "KEYS", "mhb:rl:*"))
    require(len(windows) == 1 and docker.run("exec", redis, "redis-cli", "ZCARD", windows[0]) == "2", "replicas did not share the rate window")
    require(http(origins[0], "/api/feedback", body={"subject": "Third", "body": nonce})[0] == 429, "cross-replica limit not enforced")
    docker.assert_owner("container", replicas[0])
    docker.run("restart", replicas[0])
    await_ready(origins[0], nonce)
    durable = http(origins[0], "/internal/feedback", key=admin)
    require(durable[0] == 200 and {row["id"] for row in durable[1]["items"]} == set(ids), "records lost after process restart")
    require(http(origins[0], "/api/feedback", body={"subject": "After restart", "body": nonce})[0] == 429, "rate state lost after restart")
    receipt["checks"].extend(["mongo-owner-readback", "redis-owner-readback", "cross-replica-rate-limit", "restart-preserves-data-and-rate-state"])
    for dependency, identity in [("mongo", mongo), ("redis", redis)]:
        docker.assert_owner("container", identity)
        docker.run("stop", "--time", "5", identity)
        for origin in origins:
            status, body, _ = http(origin, "/ready")
            require(status == 503 and body[dependency + "_live"] is False, "unavailable required store reported ready")
            require(http(origin, "/health")[0] == 200, "dependency outage killed the process")
        docker.run("start", identity)
        for origin in origins:
            await_ready(origin, nonce)
        receipt["checks"].append(dependency + "-outage-readiness-and-recovery")
    initialized = http(origins[0], "/mcp", key=key, body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "container-check", "version": "1"}}})
    require(initialized[0] == 200 and initialized[1]["result"]["serverInfo"]["name"] == "metahumotonic-platform", "MCP entrypoint missing")
    receipt["checks"].append("container-mcp-initialize")
    for identity in replicas + [legacy]:
        for _ in range(90):
            state = docker.inspect("container", identity, ".State")
            if state["Running"] and state.get("Health", {}).get("Status") == "healthy":
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Docker healthcheck did not become healthy")
    receipt["checks"].append("docker-healthchecks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = {"schema": "metahumotonic/container-check@1", "observedAt": datetime.now(timezone.utc).isoformat(),
               "scope": "Disposable integration stack; no production services or credentials", "nonce": uuid.uuid4().hex,
               "sourceTreeSha256": source_digest(), "status": "RUNNING", "checks": [], "images": {}}
    with args.receipt.open("x") as stream:
        stream.write(json.dumps(receipt, indent=2) + "\n")
    docker = Docker(receipt["nonce"])
    code = 1
    try:
        if not shutil.which("docker"):
            receipt.update(status="NOT_RUN", reason="Docker executable unavailable")
            code = 2
        else:
            context = docker.run("context", "show")
            endpoint = os.environ.get("DOCKER_HOST") or docker.run("context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}")
            require(endpoint.startswith("unix://"), "requires a local integration engine; remote Docker contexts are refused")
            docker.run("info", "--format", "{{.ServerVersion}}")
            with tempfile.TemporaryDirectory(prefix="mhb-container-check-") as work:
                verify_stack(docker, Path(work), receipt)
            receipt["status"] = "PASS"
            code = 0
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        receipt.update(status="FAIL", failureType=type(error).__name__)
        # Report only our source location, not exception values: Docker and HTTP
        # exceptions may carry credentials or response bodies.
        frame = error.__traceback__
        while frame and frame.tb_next:
            frame = frame.tb_next
        if frame and frame.tb_frame.f_code.co_filename == __file__:
            receipt["failureLocation"] = {"function": frame.tb_frame.f_code.co_name, "line": frame.tb_lineno}
        if isinstance(error, RuntimeError):
            receipt["reason"] = str(error)
    finally:
        failures = docker.cleanup()
        receipt["cleanup"] = {"verified": not failures, "unremoved": failures}
        if failures:
            receipt["status"] = "FAIL_CLEANUP"
            code = 1
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
