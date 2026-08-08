#!/usr/bin/env python3
"""Fail-closed tombstone for the retired OMD coordinator."""

from __future__ import annotations

import sys


MESSAGE = """\
OMD is retired for this repository.

Do not start, heal, sweep, or reconnect the historical OMD queue. Work from the
canonical main checkout, preserve foreign changes, stage exact paths, and push
validated commits normally. See docs/DEV_STACK.md and docs/OMD_PARALLEL.md.
"""


def main() -> int:
    print(MESSAGE, file=sys.stderr)
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
