from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-api-parity.py"


class FixtureServer(ThreadingHTTPServer):
    def __init__(self, payloads: dict[str, tuple[int, dict[str, str], object]]):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.payloads = payloads


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        status, headers, body = self.server.payloads.get(self.path, (404, {"content-type": "application/json"}, {"detail": "missing"}))
        encoded = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else b""
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: object) -> None:
        return


def payloads(*, instance: str, source: str = "live", different: bool = False) -> dict[str, tuple[int, dict[str, str], object]]:
    headers = {"content-type": "application/json", "cache-control": "private, no-store"}
    common = {"instance": instance, "request_id": f"request-{instance}"}
    routes: dict[str, tuple[int, dict[str, str], object]] = {
        "/api/stats": (200, headers, {**common, "nodes": 4}),
        "/api/domains": (200, headers, [{**common, "name": "graph"}]),
        "/api/skills": (200, headers, [{**common, "name": "navigate"}]),
        "/api/research/summary": (200, headers, {**common, "source": source, "findings": 1}),
        "/api/research/findings?limit=1&offset=0": (200, headers, [{**common, "name": "f"}]),
        "/api/research/lessons?limit=1&offset=0": (200, headers, [{**common, "name": "l"}]),
        "/api/research/papers?limit=1&offset=0": (200, headers, [{**common, "title": "p"}]),
        "/api/research/consensus?limit=1": (200, headers, [{**common, "name": "c"}]),
        "/api/research/recent?limit=1": (200, headers, [{**common, "name": "r"}]),
        "/api/research/agent": (200, headers, {**common, "summary": {"source": source}}),
        "/api/mcp": (200, headers, {**common, "schema": "mcp"}),
        "/api/mcp/servers": (200, headers, {**common, "servers": []}),
        "/api/mcp/manifest": (200, headers, {**common, "servers": []}),
        "/api/mcp/health": (200, headers, {**common, "servers": []}),
        "/api/mcp/status": (200, headers, {**common, "summary": {"total": 0}}),
        "/.well-known/mcp-servers.json": (302, {"content-type": "application/json"}, ""),
    }
    if different:
        routes["/api/stats"] = (200, headers, {**common, "nodes": 5})
    return routes


def running(server: FixtureServer):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def invoke(tmp_path: Path, left: FixtureServer, right: FixtureServer) -> subprocess.CompletedProcess[str]:
    receipt = tmp_path / "receipt.json"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--python-origin", f"http://127.0.0.1:{left.server_port}", "--ts-origin", f"http://127.0.0.1:{right.server_port}", "--receipt", str(receipt)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_parity_gate_allows_only_declared_noise_and_receipt_has_no_target_or_body(tmp_path: Path) -> None:
    left, right = FixtureServer(payloads(instance="python")), FixtureServer(payloads(instance="ts"))
    running(left); running(right)
    try:
        result = invoke(tmp_path, left, right)
    finally:
        left.shutdown(); right.shutdown(); left.server_close(); right.server_close()
    assert result.returncode == 0, result.stderr
    receipt = (tmp_path / "receipt.json").read_text(encoding="utf-8")
    parsed = json.loads(receipt)
    assert parsed["status"] == "PASS"
    assert len(parsed["checks"]) == 17
    assert "127.0.0.1" not in receipt and "nodes" not in receipt and "request-python" not in receipt
    assert parsed["noiseFieldsExcluded"] == sorted({"request_id", "instance", "deployedAt", "generated_at", "evaluatedAt", "updated", "last_verify_at"})


def test_parity_gate_rejects_domain_difference_and_does_not_write_pass_receipt(tmp_path: Path) -> None:
    left, right = FixtureServer(payloads(instance="python")), FixtureServer(payloads(instance="ts", different=True))
    running(left); running(right)
    try:
        result = invoke(tmp_path, left, right)
    finally:
        left.shutdown(); right.shutdown(); left.server_close(); right.server_close()
    assert result.returncode == 1
    assert "canonical JSON differs" in result.stderr
    assert not (tmp_path / "receipt.json").exists()


def test_parity_gate_requires_live_research_source(tmp_path: Path) -> None:
    left, right = FixtureServer(payloads(instance="python", source="snapshot")), FixtureServer(payloads(instance="ts", source="snapshot"))
    running(left); running(right)
    try:
        result = invoke(tmp_path, left, right)
    finally:
        left.shutdown(); right.shutdown(); left.server_close(); right.server_close()
    assert result.returncode == 1
    assert "research is not live" in result.stderr
