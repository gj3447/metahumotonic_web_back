"""Settings resolution: defaults < pyproject [tool.ooptdd] < environment.

The plugin reads `[tool.ooptdd]` via pytest's ini machinery; the CLI reads
``pyproject.toml`` directly. Either way, environment variables win — and secrets
(URLs, passwords) live *only* in the environment, never in the table.
"""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field

_VERIFY_MODES = {"off", "warn", "strict"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def parse_bool(value, default: bool = False) -> bool:
    """The single boolean convention for ooptdd config/env: case- and whitespace-insensitive,
    accepting {1,true,yes,on} / {0,false,no,off}. Empty/None returns ``default`` silently (no
    opinion); an unrecognized spelling returns ``default`` with a one-time warning, so a typo like
    ``OOPTDD_ENABLED=of`` is loud instead of silently flipping a verdict."""
    if value is None:
        return default
    s = str(value).strip().lower()
    if not s:
        return default
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    warnings.warn(f"ooptdd: unrecognized boolean {value!r}; using default {default}", stacklevel=2)
    return default


@dataclass
class Settings:
    backend: str = "memory"
    service: str = "ooptdd.tests"
    cid_env: str = "OOPTDD_CID"
    verify: str = "warn"  # off | warn | strict
    enabled: str = "auto"  # auto | "1"/"true" | "0"/"false"
    retries: int = 4
    delay: float = 1.0
    backoff: float = 2.0
    backend_options: dict = field(default_factory=dict)

    def is_enabled(self) -> bool:
        """`auto`: on for zero-infra backends (memory), on for network backends only when
        OOPTDD_ENABLED parses truthy; an explicit `enabled` flag always wins. All boolean reads go
        through :func:`parse_bool`, so ``off``/``no``/``FALSE`` disable (they used to enable a
        network backend under `auto` via a case-sensitive membership test)."""
        flag = str(self.enabled).strip().lower()
        if flag not in ("auto", ""):
            return parse_bool(flag, default=False)
        # auto
        if self.backend == "memory":
            return True
        return parse_bool(os.getenv("OOPTDD_ENABLED"), default=False)

    @property
    def mode(self) -> str:
        return self.verify if self.verify in _VERIFY_MODES else "warn"


def _coerce(raw: dict) -> dict:
    out = dict(raw)
    for k in ("retries",):
        if k in out:
            out[k] = int(out[k])
    for k in ("delay", "backoff"):
        if k in out:
            out[k] = float(out[k])
    return out


def from_mapping(table: dict | None) -> Settings:
    """Build Settings from a flat mapping (pyproject table or ini values),
    letting env vars override the scalar fields."""
    table = _coerce(table or {})
    s = Settings(
        backend=table.get("backend", "memory"),
        service=table.get("service", "ooptdd.tests"),
        cid_env=table.get("cid_env", "OOPTDD_CID"),
        verify=table.get("verify", "warn"),
        enabled=str(table.get("enabled", "auto")),
        retries=table.get("retries", 4),
        delay=table.get("delay", 1.0),
        backoff=table.get("backoff", 2.0),
        backend_options=dict(table.get("backend_options", {})),
    )
    # environment overrides
    s.backend = os.getenv("OOPTDD_BACKEND", s.backend)
    s.service = os.getenv("OOPTDD_SERVICE", s.service)
    s.verify = os.getenv("OOPTDD_VERIFY", s.verify)
    return s


def load_pyproject(path: str = "pyproject.toml") -> dict:
    """Read ``[tool.ooptdd]`` from a pyproject file (returns {} if absent)."""
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    return data.get("tool", {}).get("ooptdd", {})
