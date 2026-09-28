#!/usr/bin/env python3
"""Run the compiled TS entrypoint and real Python domains on disposable loopback ports.

No .env, production credentials, data services, or ingress changes. Build TS and
install the Python environment first. The result is local compatibility evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
OPENER = build_opener(ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(port: int, path: str, body: dict | None = None, key: str | None = None, method: str | None = None):
    headers = {"Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
    try:
        response = OPENER.open(req, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(1_048_577)
        assert len(raw) <= 1_048_576
        value = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw.decode()
        return response.status, value, response.headers


def main() -> None:
    processes: list[subprocess.Popen] = []
    marker = f"platform-local-{uuid.uuid4().hex}"
    key = uuid.uuid4().hex + uuid.uuid4().hex
    admin_key = uuid.uuid4().hex + uuid.uuid4().hex
    legacy_port, port = free_port(), free_port()
    while port == legacy_port:
        port = free_port()
    with tempfile.TemporaryDirectory(prefix="mhb-platform-check-") as directory:
        # The temporary cwd also prevents pydantic from loading the repo's .env.
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "PYTHONPATH": str(ROOT), "MHB_NEO4J_LIVE": "false", "MHB_MONGO_URI": "", "MHB_REDIS_URL": "", "MHB_LOG_JSON": "false"}
        with open(Path(directory) / "process.log", "w+") as log:
            try:
                legacy = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(legacy_port)], cwd=directory, env=env, stdout=log, stderr=log)
                processes.append(legacy)
                gateway = subprocess.Popen([str(ROOT / "ts/scripts/with-node.sh"), "node", str(ROOT / "ts/dist/src/main.js")], cwd=directory, env={**env, "MHB_HOST": "127.0.0.1", "MHB_PORT": str(port), "MHB_VERSION": marker, "MHB_PLATFORM_READ_KEY": key, "MHB_FEEDBACK_ADMIN_KEY": admin_key, "MHB_LEGACY_ORIGIN": f"http://127.0.0.1:{legacy_port}", "MHB_LEGACY_REQUIRED": "true"}, stdout=log, stderr=log)
                processes.append(gateway)
                for _ in range(100):
                    assert all(p.poll() is None for p in processes), "a local service exited"
                    try:
                        if request(port, "/health")[1].get("version") == marker and request(port, "/ready")[0] == 200:
                            break
                    except (URLError, ConnectionError, TimeoutError):
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError("local services did not become ready")
                assert request(port, "/")[1]["runtime"] == "effect-ts"
                for path in ("/api/wiki/v1", "/api/wiki/v1/pages", "/api/v1/ontology/search?q=HSWM"):
                    direct, proxied = request(legacy_port, path), request(port, path)
                    # Request IDs are generated separately for each actual request.
                    # Verify their header binding, then compare every stable field.
                    if path.startswith("/api/v1/ontology"):
                        for result in (direct, proxied):
                            request_id = result[1]["error"].pop("request_id")
                            assert request_id and request_id == result[2].get("x-request-id")
                    assert proxied[:2] == direct[:2], path
                    assert proxied[2].get("x-mhb-service") == "legacy-domain", path
                assert request(port, "/api/platform/v1/programs")[0] == 401
                assert request(port, "/api/platform/v1/programs", key=key)[0] == 200
                initialized = request(port, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "local-check", "version": "1"}}}, key)
                assert initialized[0] == 200
                assert initialized[1]["result"]["serverInfo"]["name"] == "metahumotonic-platform"
                created = request(port, "/api/feedback", {"subject": "Lifecycle", "body": "Production ID contract", "email": "fixture@example.invalid", "contact_consent": True})
                assert created[0] == 200
                record_id = created[1]["id"]
                assert re.fullmatch(r"[0-9a-f]{32}", record_id), record_id
                triaged = request(port, f"/internal/feedback/{record_id}", {"status": "reviewed"}, admin_key, "PATCH")
                assert triaged[0] == 200 and triaged[1]["item"]["status"] == "reviewed"
                assert request(port, f"/internal/feedback/{record_id}", key=admin_key, method="DELETE")[0] == 204
                assert request(port, "/internal/feedback", key=admin_key)[1]["count"] == 0
                assert "mhb_http_requests_total" in request(port, "/metrics")[1]
                receipt = {"schema": "metahumotonic/local-platform-check@1", "status": "PASS", "scope": "Compiled TS + real Python domains, zero infrastructure, loopback only; not a production rollout.", "checks": ["own-process-health", "required-domain-readiness", "wiki-index-parity", "wiki-pages-parity", "disabled-ontology-parity", "platform-auth", "mcp-initialize", "feedback-lifecycle", "native-metrics"], "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in ("ts/dist/src/main.js", "ts/dist/src/ports/LegacyService.js", "ts/dist/src/ports/Ids.js", "app/main.py")}}
                print(json.dumps(receipt, indent=2))
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read()[-12000:])
                raise
            finally:
                for process in reversed(processes):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()
