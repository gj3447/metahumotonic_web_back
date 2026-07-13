"""Engine layer — the evaluation logic, written against domain ports only.

This is the kernel and the verdict machinery:

  - :mod:`ooptdd.engine.monitor`  streaming LTL₃/MTL monitor automata (the kernel)
  - :mod:`ooptdd.engine.gate`     compile a YAML trace spec into monitors and run it
  - :mod:`ooptdd.engine.verify`   poll a backend port, produce the three-valued verdict

Dependency rule: ``engine`` imports only from ``engine``, ``domain`` (the ``Backend`` port,
the event model/ontology), and the stdlib — never from a concrete adapter (a backend
driver, the CLI, the pytest plugin). The architecture fitness test enforces this, so the
evaluation logic stays runnable against any store and trivially unit-testable.
"""
from .gate import can_i_deploy, check, evaluate, load_gate
from .monitor import Monitor, run_monitor
from .verify import session_finish, verify_policy, verify_trace

__all__ = [
    "evaluate",
    "can_i_deploy",
    "check",
    "load_gate",
    "Monitor",
    "run_monitor",
    "verify_trace",
    "verify_policy",
    "session_finish",
]
