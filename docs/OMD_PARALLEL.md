# OMD parallel pipeline — RETIRED

> Effective 2026-08-08. This is a tombstone, not an executable runbook.

The experimental OMD queue used on 2026-07-13 is retired. No live OMD server,
lease, heartbeat, or coordinator owns this repository. Stale `WORKING` and
`PENDING` rows in the historical database are not active ownership.

Do not:

- register or start an OMD MCP server;
- run begin, claim, heartbeat, sweep, heal, finish, or connect operations;
- use the historical driver to create or merge worktrees;
- treat `.omd/coord.db` or old lock files as a live source of truth.

The previously untracked driver is preserved byte-for-byte under
[`archive/omd/`](archive/omd/) with its SHA-256. The active driver path is a
fail-closed tombstone so stale automation exits clearly.

Current coordination rules are in [`DEV_STACK.md`](DEV_STACK.md): one canonical
tracking-`main` writer, preserve foreign changes, validate, stage exact paths,
commit, push, and read back the remote commit.

The original queue design remains recoverable from Git history and the archive;
it must not be revived as an operating dependency.

<!-- Historical KG: project_metahumotonic_web_integrate_core_dev_tech_2026_07_13, project_omd_develop_queue_2026_07_13 -->
