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
- [ ] No unresolved P0 regression remains.
- [ ] Known limits are tracked separately: [issue #23](https://github.com/pumni/threads-browser/issues/23)
  tracks `READ_IN_PROGRESS` at the last-scroll boundary, and [issue #47](https://github.com/pumni/threads-browser/issues/47)
  tracks `ROOT_AMBIGUOUS` on a second Detail. This documentation, PR #51, and
  green CI do not resolve either issue; the release owner records any separate
  resolution or explicit acceptance.

Engineering merge, bounded live UAT, and continuous foreground/scheduler
acceptance are three separate levels. Green CI does not establish live Threads
compatibility, and scheduler activation still needs separate owner approval.

## Bounded owner-supervised live UAT

- [ ] The owner installs or upgrades using `uv sync --locked` and installs the
  pinned Playwright Chromium browser.
- [ ] The owner confirms the intended alias is verified and uses its dedicated
  authenticated profile in a visible, headed Chromium session.
- [ ] The owner runs one bounded Auto cycle with
  `tb auto <verified-alias> --max-cycles 1 --uat-diagnostics`. The cycle limit
  counts completed, checkpointed cycles; bounded supervisor retries may start
  more than one browser attempt.
- [ ] The owner confirms current Vietnamese job intent is required. An English
  role title may appear in a Vietnamese sentence; English-only, third-party,
  historical, negated, advice, course, or ambiguous intent is not eligible.
- [ ] The owner verifies selected post permalink, author, and complete body;
  Detail verification; Back verification; truthful coverage; safe checkpoint
  progress; and clean browser/context shutdown and profile-lock release.
- [ ] The owner supervises one `tb run` that commits a safe checkpoint and one
  expected incomplete/stop result.
- [ ] For an approved `tb session` pilot, the owner checks multiple verified
  Detail/Back operations in one headed Chromium context, coverage/provenance
  stops, idempotent checkpoint progress, and clean browser closure.
- [ ] The owner verifies sanitized stdout and status locally without copying
  private logs, account details, post content, profile paths, or URLs to a
  shared location.
- [ ] The owner treats missing or ambiguous evidence as incomplete UAT and
  does not publish private logs. `--uat-diagnostics` is for local fixed codes
  and aggregate counters only.
- [ ] The owner rehearses rollback to the last known approved code version
  without deleting or editing the private data root.
- [ ] Live Threads UI assumptions are recorded as owner-supervised UAT. Local
  fixture tests do not count as live compatibility evidence.

## Continuous foreground and scheduler acceptance

- [ ] Before relying on repeated Auto cycles, the owner separately supervises
  restart/deduplication, bounded retries and cooldowns, incomplete coverage,
  Detail/Back provenance, checkpoint continuity, Ctrl+C, and browser cleanup.
- [ ] A bounded one-cycle UAT or engineering merge is not recorded as
  continuous foreground acceptance.

### Task Scheduler decision

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
