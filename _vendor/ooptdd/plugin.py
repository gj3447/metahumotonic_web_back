"""pytest plugin — ship every test outcome and assert it arrived.

Registered via the ``pytest11`` entry point, so ``pip install ooptdd`` is enough;
no conftest wiring required. Behaviour:

* **Off is truly off.** When disabled the hooks return immediately — byte-for-byte
  identical run (a property the test suite checks).
* **xdist-safe.** Per-test reports are collected via ``pytest_runtest_logreport``,
  which fires on the controller for every forwarded report (and in-process when
  serial) — so the controller, the only node that ships + verifies, has the full
  set with or without ``-n``. A ``-n 8`` run ships once, not eight times — and,
  crucially, not *zero* times (collecting in ``pytest_runtest_makereport`` would
  only ever see the worker each test ran on, never the controller).
* **Fail-open.** A down backend never hangs or breaks the suite — verification
  defaults to ``warn``; only opt-in ``strict`` can fail the build, and only on a
  *real* miss (never on an unreachable store).

Config: ``[tool.ooptdd]`` in pyproject (see :mod:`ooptdd.config`), env overrides,
or the ini keys ``ooptdd_backend`` / ``ooptdd_service`` / ``ooptdd_verify`` /
``ooptdd_enabled`` / ``ooptdd_cid_env``.
"""
from __future__ import annotations

import os
import uuid

import pytest

from .backends import get_backend, memory_reset
from .config import Settings, from_mapping, load_pyproject
from .engine.verify import session_finish


@pytest.fixture
def ooptdd_memory_reset():
    """Clear the process-global in-memory store around a test — the reset half of the setup
    consumers used to hand-roll. Opt-in (request it); NOT autouse, so a consumer that manages its
    own store lifecycle is never surprised by a hidden reset."""
    memory_reset()
    yield
    memory_reset()


@pytest.fixture
def ooptdd_cid(monkeypatch, ooptdd_memory_reset):
    """A unique correlation id for this test, also exported as ``OOPTDD_CID`` so a gate spec using
    ``cid_env`` resolves to it — replaces the ``monkeypatch.setenv('OOPTDD_CID', …)`` + manual
    store-reset dance. Depends on the reset fixture, so requesting a cid also isolates the store."""
    cid = f"test-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("OOPTDD_CID", cid)
    return cid


def pytest_addoption(parser):
    group = parser.getgroup("ooptdd", "logs-as-spec test verification")
    group.addoption("--ooptdd", action="store_true", default=False,
                    help="force-enable ooptdd for this run")
    group.addoption("--no-ooptdd", action="store_true", default=False,
                    help="force-disable ooptdd for this run")
    for key, help_ in [
        ("ooptdd_backend", "backend name (memory|openobserve|otel|<entrypoint>)"),
        ("ooptdd_service", "service name stamped on events"),
        ("ooptdd_verify", "verify mode: off|warn|strict"),
        ("ooptdd_enabled", "auto|1|0"),
        ("ooptdd_cid_env", "env var holding the correlation id"),
        ("ooptdd_retries", "arrival-poll attempts (int, default 4)"),
        ("ooptdd_delay", "initial arrival-poll delay in seconds (float, default 1.0)"),
        ("ooptdd_backoff", "arrival-poll backoff multiplier (float, default 2.0)"),
    ]:
        parser.addini(key, help=help_, default=None)


def _settings_from_config(config) -> Settings:
    table: dict = {}
    for ini, field_ in [
        ("ooptdd_backend", "backend"),
        ("ooptdd_service", "service"),
        ("ooptdd_verify", "verify"),
        ("ooptdd_enabled", "enabled"),
        ("ooptdd_cid_env", "cid_env"),
        ("ooptdd_retries", "retries"),
        ("ooptdd_delay", "delay"),
        ("ooptdd_backoff", "backoff"),
    ]:
        val = config.getini(ini)
        if val:
            table[field_] = val
    pj = {}
    if getattr(config, "rootpath", None) is not None:
        pj = load_pyproject(str(config.rootpath / "pyproject.toml"))
    s = from_mapping({**pj, **table})
    if config.getoption("--ooptdd"):
        s.enabled = "1"
    if config.getoption("--no-ooptdd"):
        s.enabled = "0"
    return s


def pytest_configure(config):
    s = _settings_from_config(config)
    config._ooptdd_settings = s
    config._ooptdd_reports = []
    config._ooptdd_active = s.is_enabled()
    config._ooptdd_cid = os.getenv(s.cid_env) or f"pytest-{uuid.uuid4().hex[:12]}"
    if config._ooptdd_active:
        # Register the report collector only when active so "off is truly off" (no hook
        # registered at all when disabled). It collects via pytest_runtest_logreport — see
        # _ReportCollector for why that, not pytest_runtest_makereport, is what makes ooptdd
        # actually run under xdist.
        config.pluginmanager.register(_ReportCollector(config), "_ooptdd_report_collector")


class _ReportCollector:
    """Gather each test's report on whichever node *aggregates* results.

    The hook is ``pytest_runtest_logreport`` on purpose, not ``pytest_runtest_makereport``.
    makereport only ever fires where a test *executes* — under ``pytest-xdist`` that is the
    worker, never the controller — so the controller (the only node that ships + verifies,
    see :func:`pytest_sessionfinish`) collected nothing and a ``-n`` run silently shipped and
    verified *nothing*: a false green, the exact failure ooptdd exists to catch. logreport
    fires on the controller for every forwarded report (and once in-process when serial), so
    the report set is identical with or without ``-n``.
    """

    def __init__(self, config):
        self._config = config

    def pytest_runtest_logreport(self, report):
        if report.when not in ("setup", "call", "teardown"):
            return
        self._config._ooptdd_reports.append(
            {
                "nodeid": report.nodeid,
                "outcome": report.outcome,  # passed | failed | skipped
                "when": report.when,
                "duration": getattr(report, "duration", 0.0),
                "longrepr": str(report.longrepr) if report.failed else None,
            }
        )


def _is_xdist_controller(config) -> bool:
    return not hasattr(config, "workerinput")


def _resolve_require_signature(env_value: str | None, signing_key: str | None) -> bool:
    """Enforce-if-keyed — close the "keyed verifier still greenlights an unsigned receipt"
    footgun. ``OOPTDD_SIGNING_KEY`` and ``OOPTDD_REQUIRE_SIGNATURE`` used to be independent, so
    a verifier that *had* a key but never set the require flag still accepted UNSIGNED receipts
    from any producer. Now, when the operator makes no explicit choice, a signature is required
    exactly when a signing key is configured — setting a key is itself the intent to reject
    unsigned receipts. An explicit ``OOPTDD_REQUIRE_SIGNATURE`` always wins either direction:
    ``0``/``false``/``no``/``off`` opts OUT even with a key; any other value opts IN even
    without a local key (verifier side). Keyless + no explicit choice stays lenient, so
    zero-config (the demo, this suite) is unbroken."""
    if env_value is not None and env_value.strip():
        return env_value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(signing_key)


def pytest_collection_finish(session):
    """Ship a `session_start` heartbeat once collection is known (controller only).

    Best-effort: if the backend init/ship fails (e.g. unprovisioned store), swallow it —
    a heartbeat must never break collection. Its only job is to let verify distinguish
    'started but summary lost' from 'nothing arrived' if the summary is later dropped.
    """
    config = session.config
    if not getattr(config, "_ooptdd_active", False) or not _is_xdist_controller(config):
        return
    s: Settings = config._ooptdd_settings
    try:
        from .domain.model import build_session_start

        backend = get_backend(s.backend, service=s.service, **s.backend_options)
        backend.ship([build_session_start(
            config._ooptdd_cid, service=s.service, expected_total=len(session.items)
        )])
    except Exception as exc:  # noqa: BLE001 — heartbeat is best-effort, never gates collection
        # Surface the swallow (it changes what a later 'summary lost' diagnosis can conclude) —
        # but never re-raise into collection.
        _emit(config, [f"ooptdd session_start heartbeat not shipped: {type(exc).__name__}: {exc}"])


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if not getattr(config, "_ooptdd_active", False):
        return
    if not _is_xdist_controller(config):
        return  # workers don't ship; the controller has all forwarded reports
    reports = getattr(config, "_ooptdd_reports", [])
    if not reports:
        return
    s: Settings = config._ooptdd_settings
    try:
        backend = get_backend(s.backend, service=s.service, **s.backend_options)
    except Exception as exc:
        _emit(config, [f"backend init failed ({exc}); skipping (build unaffected)"])
        return
    # signing key is CI-only: read from env, never config/code. Absent -> unsigned no-op.
    signing_key = os.getenv("OOPTDD_SIGNING_KEY")
    result = session_finish(
        backend,
        reports,
        config._ooptdd_cid,
        service=s.service,
        mode=s.mode,
        retries=s.retries,
        delay=s.delay,
        backoff=s.backoff,
        signing_key=signing_key,
        # enforce-if-keyed: a configured key makes unsigned receipts a failure by default,
        # unless OOPTDD_REQUIRE_SIGNATURE explicitly opts out (and keyless stays lenient).
        require_signature=_resolve_require_signature(
            os.getenv("OOPTDD_REQUIRE_SIGNATURE"), signing_key
        ),
    )
    _emit(config, result["messages"])
    if result["fail_build"] and exitstatus == 0:
        session.exitstatus = 1


def _emit(config, messages):
    tr = config.pluginmanager.get_plugin("terminalreporter")
    for m in messages:
        if tr is not None:
            tr.write_line(f"[ooptdd] {m}")
        else:  # pragma: no cover
            print(f"[ooptdd] {m}")
