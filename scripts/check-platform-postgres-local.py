#!/usr/bin/env python3
"""Run the company inventory PostgreSQL tests on an exclusively created local cluster."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = {"schema": "metahumotonic/platform-postgres-check@1", "observedAt": datetime.now(timezone.utc).isoformat(),
               "scope": "Disposable local PostgreSQL and native TS inventory store and real compiled-server tests; no production DSN or service changes", "status": "RUNNING"}
    with args.receipt.open("x") as stream:
        stream.write(json.dumps(receipt) + "\n")
    work = Path(tempfile.mkdtemp(prefix="mhb-platform-postgres-"))
    cluster = work / "cluster"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "PYTHONPATH": str(ROOT),
           "MHB_NEO4J_LIVE": "false", "MHB_WIKI_PUBLIC_WRITES": "false"}
    # Support a relocated user-owned PostgreSQL installation without a system
    # package install; never inherit arbitrary loader paths from production env.
    library = args.postgres_bin.resolve().parent / "lib"
    if (library / "libpq.so.5").exists():
        env["LD_LIBRARY_PATH"] = str(library)
    started = False
    code = 1
    stage = "preflight"

    def run(argv, timeout=60):
        result = subprocess.run([str(item) for item in argv], cwd=work, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            # Useful failure diagnostics without connection strings or temporary passwords.
            detail = (result.stdout + result.stderr)[-6000:]
            for key in ("MHB_TEST_PLATFORM_DATABASE_URL",):
                if env.get(key): detail = detail.replace(env[key], "[redacted-test-dsn]")
            print(detail, flush=True)
            raise RuntimeError("command failed during " + stage)
        return result.stdout.strip()

    try:
        for binary in ("postgres", "initdb", "pg_ctl"):
            if not (args.postgres_bin / binary).is_file():
                raise RuntimeError("missing PostgreSQL binary")
        receipt["postgresVersion"] = run([args.postgres_bin / "postgres", "--version"])
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        password_file = work / "password"
        password_file.write_text(secret + "\n")
        password_file.chmod(0o600)
        stage = "init-disposable-cluster"
        run([args.postgres_bin / "initdb", "-D", cluster, "--username=mhb_platform_test", "--auth=scram-sha-256", "--no-locale", "--encoding=UTF8", "--pwfile", password_file])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        (work / "socket").mkdir()
        stage = "start-disposable-cluster"
        # If startup partially succeeds, the owned data directory still identifies
        # the exact process to stop; never search/kill another PostgreSQL process.
        started = True
        run([args.postgres_bin / "pg_ctl", "-D", cluster, "-l", work / "postgres.log", "-w", "start", "-o",
             f"-h 127.0.0.1 -p {port} -k {work / 'socket'} -c max_connections=30 -c shared_buffers=32MB"])
        env["MHB_TEST_PLATFORM_DATABASE_URL"] = f"postgresql://mhb_platform_test:{secret}@127.0.0.1:{port}/postgres"
        env["MHB_REQUIRE_PLATFORM_POSTGRES_TESTS"] = "1"
        stage = "typescript-build"
        run(["bash", ROOT / "ts/scripts/with-node.sh", "npm", "--prefix", ROOT / "ts", "run", "build"], timeout=120)
        stage = "platform-postgres-tests"
        run(["bash", ROOT / "ts/scripts/with-node.sh", "npm", "--prefix", ROOT / "ts", "test", "--",
             "test/platform-postgres.test.ts", "--reporter=junit", "--outputFile", work / "tests.xml"], timeout=180)
        suites = ET.parse(work / "tests.xml").getroot().iter("testsuite")
        counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
        for suite in suites:
            for key in counts:
                counts[key] += int(suite.get(key, "0"))
        if not counts["tests"] or any(counts[key] for key in ("failures", "errors", "skipped")):
            raise RuntimeError("PostgreSQL tests must execute with no failures or skips")
        receipt["tests"] = counts
        receipt["sourceSha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in ("scripts/check-platform-postgres-local.py", "ts/test/platform-postgres.test.ts",
                         "ts/src/ports/PlatformInventoryPostgres.ts", "ts/src/domain/ObservationIngest.ts",
                         "ts/config/migrations/001-platform.sql", "ts/src/server/Composition.ts", "ts/package-lock.json")}
        receipt["status"] = "PASS"
        code = 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        receipt.update(status="FAIL", failure={"stage": stage, "type": type(error).__name__})
        if (work / "tests.xml").exists():
            report = ET.parse(work / "tests.xml").getroot()
            for failure in report.iter("failure"):
                detail = (failure.text or "")[-4000:]
                if env.get("MHB_TEST_PLATFORM_DATABASE_URL"):
                    detail = detail.replace(env["MHB_TEST_PLATFORM_DATABASE_URL"], "[redacted-test-dsn]")
                print(detail, flush=True)
    finally:
        try:
            if started:
                stage = "stop-owned-cluster"
                run([args.postgres_bin / "pg_ctl", "-D", cluster, "-m", "fast", "-w", "stop"])
                if (cluster / "postmaster.pid").exists():
                    raise RuntimeError("owned cluster still has its process marker")
            shutil.rmtree(work)
            receipt["cleanup"] = {"clusterStopped": True, "temporaryDirectoryRemoved": True}
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            receipt.update(status="FAIL_CLEANUP", retainedWorkdir=str(work))
            code = 1
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
