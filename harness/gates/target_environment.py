#!/usr/bin/env python3
"""SPEC §5 `Environment(F)` — does it hold WHERE IT SHIPS?

Until 2026-08-10 this term was NOT_RUN, and that blocked DONE. Correctly: the
TypeScript tree had never been deployed anywhere, so no finite local trace
could decide it, and a binary gate answering GREEN there would have been the
false GREEN §4-5 exists to prevent.

runtime-01 (LXC 308, 192.168.0.33) changed that. There is now a real
environment, running a real artifact under systemd against the live KG, and
this gate interrogates it from OUTSIDE the process — the same way
ops/check-web-back-live.sh interrogates production.

What it refuses to do:

  - It does not check production (VM100). That is a different environment with
    a different artifact, and claiming this one covers it would be a lie.
  - It reports INCONCLUSIVE (exit 2), never GREEN, when runtime-01 is simply
    unreachable. "I could not ask" is not "the answer is yes".

exit 0 = GREEN, 1 = RED, 2 = INCONCLUSIVE
"""
from __future__ import annotations

import json
import subprocess
import sys

TARGET = "deploy@192.168.0.33"
BASE = "http://127.0.0.1:8000"


def curl(path: str, timeout: int = 12, method: str = "GET", body: str = "") -> tuple[int, str]:
    """One request against the deployed artifact, from outside its process.

    `method` matters: the first version of this gate probed the POST-only
    /api/kg/read with a GET and read the resulting 404 as a failure of the
    service. The service was right and the check was wrong — so the method is
    explicit here rather than assumed.
    """
    extra = ""
    if method != "GET":
        extra = f"-X {method} "
        if body:
            extra += f"-H Content-Type:application/json --data '{body}' "
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", TARGET,
        f"curl -s --max-time {timeout} {extra}-w '\\n%{{http_code}}' {BASE}{path}",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return -1, "__TIMEOUT__"
    if p.returncode != 0:
        return -1, f"__SSH_FAIL__ {p.stderr.strip()[:120]}"
    body, _, code = p.stdout.rpartition("\n")
    return (int(code) if code.strip().isdigit() else -1), body


def main() -> int:
    code, _ = curl("/health", timeout=5)
    if code == -1:
        print("INCONCLUSIVE runtime-01 is unreachable — cannot decide Environment")
        print("             (this is not a pass; DONE stays blocked)")
        return 2

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(label)

    # the artifact actually answers
    code, body = curl("/health")
    health = {}
    try:
        health = json.loads(body)
    except Exception:
        pass
    check("/health is 200 with status ok", code == 200 and health.get("status") == "ok", body[:80])

    # readiness in the SHAPE the live checker reads (ops/check-web-back-live.sh)
    code, body = curl("/ready")
    ready = {}
    try:
        ready = json.loads(body)
    except Exception:
        pass
    required = {"status", "kg_live", "wiki_required", "wiki_live",
                "wiki_store_live", "wiki_rate_limit_live", "degraded"}
    check("/ready carries all 7 contract keys", required.issubset(ready), f"missing {sorted(required - set(ready))}")
    check("/ready reports ready", ready.get("status") == "ready", str(ready)[:80])
    check("not degraded", ready.get("degraded") is False, str(ready.get("degraded")))

    # the environment's whole point: the KG is actually reachable FROM THERE
    check("kg_live true in the target environment", ready.get("kg_live") is True)

    code, body = curl("/api/research/summary")
    summary = {}
    try:
        summary = json.loads(body)
    except Exception:
        pass
    check("research summary is a live read, not the snapshot",
          summary.get("source") == "live", f"source={summary.get('source')}")

    # the contract corrections hold in the deployed artifact too, not just in tests
    code, _ = curl("/api/research/findings?limit=99999")
    check("out-of-range pagination rejected with 422", code == 422, f"got {code}")
    code, _ = curl("/api/kg/read", method="POST", body='{"query":"RETURN 1"}')
    check("kg proxy disabled without a key (503, not open)", code == 503, f"got {code}")

    # a GET on the POST-only proxy must not be routable at all
    code, _ = curl("/api/kg/read")
    check("kg proxy is POST-only", code in (404, 405), f"got {code}")

    # supervised, and not running as root
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET,
         "sudo -n systemctl is-active mhb-ts.service; ps -o user= -p $(systemctl show mhb-ts.service -p MainPID --value)"],
        capture_output=True, text=True, timeout=30,
    )
    out = p.stdout.split()
    check("unit is active", "active" in out, p.stdout.strip()[:60])
    check("service does not run as root", "root" not in out[1:], p.stdout.strip()[:60])

    if failures:
        print(f"\nRED  {len(failures)} environment check(s) failed: {', '.join(failures)}")
        return 1
    print("\nGREEN runtime-01 serves the artifact correctly against the live KG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
