# Release checklist

This checklist defines a ship candidate; merging code or green CI alone does
not complete the release. Do not create a release tag or enable Task Scheduler
as part of this checklist without the owner's separate approval.

## Engineering gate

- [ ] The intended commit is on `main`; record its full SHA.
- [ ] Exact-head Ubuntu and Windows CI runs are green. Ubuntu uses Xvfb for
  headed synthetic Chromium tests; Windows runs headed Chromium directly and
  exercises the PowerShell scheduled wrapper with a fake `uv` command.
- [ ] `uv sync --locked`, `uv run ruff check .`, full offline pytest, and
  `git diff --check` pass for the release commit.
- [ ] CLI short and legacy command trees retain their contract, and
  `threads_browser.storage` imports remain compatible.
- [ ] Operation status is allowlisted and bounded to one `last_run.json`.
  `COMPLETE` requires a committed checkpoint; `STOPPED` and `FAILED` return a
  nonzero exit code. A killed process may leave `RUNNING` for inspection.
- [ ] Checkpoint v3, profile lock collision handling, atomic writes, privacy,
  and browser cleanup regressions pass.
- [ ] No unresolved P0 regression remains. Issue #23's final-scroll boundary
  is resolved or the owner has explicitly accepted its known limit.

## Windows operator acceptance

- [ ] The owner installs or upgrades using `uv sync --locked` and installs the
  pinned Playwright Chromium browser.
- [ ] The owner supervises one `tb run` that commits a safe checkpoint and one
  expected incomplete/stop result.
- [ ] For an approved `tb session` pilot, the owner checks multiple verified
  Detail/Back operations in one headed Chromium context, coverage/provenance
  stops, idempotent checkpoint progress, and clean browser closure.
- [ ] The owner verifies sanitized stdout and status locally without copying
  private logs, account details, post content, profile paths, or URLs to a
  shared location.
- [ ] The owner rehearses rollback to the last known approved code version
  without deleting or editing the private data root.
- [ ] Live Threads UI assumptions are recorded as owner-supervised UAT. Local
  fixture tests do not count as live compatibility evidence.

## Task Scheduler decision

Recurring scheduling stays disabled until the Windows operator separately
approves:

- [ ] cadence and catch-up policy;
- [ ] status retention window and access control;
- [ ] recovery policy for interrupted runs, authentication expiry, challenges,
  rate limits, timeouts, sleep, and logoff;
- [ ] logged-in desktop and awake-machine requirements.

If that approval is not recorded, the release may be reviewed as code but is a
**NO-SHIP for recurring automation**. A successful one-time run does not grant
recurring activation approval. See
[`OPERATIONS_WINDOWS.md`](OPERATIONS_WINDOWS.md) for the operator procedure.

## Release and rollback record

After all engineering and operator gates are checked, the owner records the
approved version/tag, full commit SHA, test/CI run links, UAT result, accepted
limitations, and rollback target in the private release record. Do not include
account aliases, browser paths, post contents, raw URLs, or local status files
in that record when it is stored in a shared system. Leave Task Scheduler
disabled unless the separate schedule decision is complete.
