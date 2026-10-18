# Windows installation and operations

All browser work is a foreground, headed, bounded, read-only operation. The
repository includes an opt-in `--scheduled` status mode and a PowerShell
wrapper, but it does not create or enable a Task Scheduler task. Recurring
automation remains disabled until the owner separately approves its cadence,
retention, recovery policy, and desktop/power assumptions.

## Install

Use a dedicated checkout and a logged-in Windows desktop account:

```powershell
git clone https://github.com/pumni/threads-browser.git D:\Apps\threads-browser
Set-Location D:\Apps\threads-browser
uv sync --locked
uv run playwright install chromium
uv run tb --help
```

Register a local account, configure literal relevance terms, and establish its
session interactively:

```powershell
uv run tb add local1
uv run tb terms local1 "hiring" "đang tuyển"
uv run tb login local1
```

Complete authentication and any security challenge manually in Chromium. The
browser must remain headed. Before considering automation, supervise a normal
`uv run tb run local1` invocation and check its sanitized result. Do not use
text-output options in scheduled mode.

The application data root defaults to
`%LOCALAPPDATA%\ThreadsBrowser`; set `THREADS_BROWSER_DATA_ROOT` to use a
different private directory. Account metadata, relevance terms, checkpoints,
profile locks, Chromium profile data, and the single latest scheduled status
are stored locally. Keep this root out of source control and shared logs. It
contains account references and authenticated browser state.

## Upgrade

1. Let the current foreground run finish or stop it with Ctrl+C. A stopped
   cycle does not become a completed checkpoint; earlier safe session
   checkpoints remain available.
2. Check `git status --short` and preserve any local changes before switching
   versions. Do not reset or clean the checkout as an upgrade step.
3. Fetch the approved release reference and check it out in the dedicated
   checkout. Use the version tag or commit recorded by the owner.
4. Run `uv sync --locked` and `uv run playwright install chromium`.
5. Check `uv run tb --help`, then supervise one attended `tb run` before
   relying on the updated installation.

Updates use the existing private data root. The current checkpoint schema is
version 3. A release that changes persisted schemas must include a migration
and rollback note before use. Never hand-edit checkpoint JSON or copy an
authenticated browser profile to another account or machine.

## Roll back

1. Stop the foreground application and wait for Chromium to close. If Windows
   killed the process, confirm its browser process has ended before retrying.
2. Check out the last owner-approved version tag or commit in the dedicated
   checkout and run `uv sync --locked`, followed by
   `uv run playwright install chromium`.
3. Keep the existing private data root in place. Do not delete profile locks,
   checkpoints, or browser data as a rollback shortcut. The OS releases an
   abandoned lock after its owning process exits; the lock file itself may
   remain.
4. If the previous version rejects the current checkpoint schema or reports a
   state error, stop and preserve the files for owner review. Do not edit or
   restore a guessed checkpoint.
5. Run `uv run tb --help`, then perform one supervised, attended `tb run`.

Rollback restores application code and its locked dependencies. It does not
revert completed checkpoints or copy profile state. Keep any owner-managed
backup encrypted and access-controlled; do not attach it to issues or PRs.

## Scheduled mode and status

The optional wrapper can be exercised manually after the attended CLI contract
is understood:

```powershell
pwsh -NoProfile -File .\scripts\windows\run-scheduled.ps1 -Alias local1
```

It calls one bounded `tb run` with saved terms, suppresses per-row diagnostics
and operator prompts, and forwards the application exit code. It does not log
in, retry, or engage with posts. The wrapper requires `uv` on `PATH` and a
logged-in interactive desktop.

Each scheduled attempt atomically replaces
`%LOCALAPPDATA%\ThreadsBrowser\operations\last_run.json` (or the configured
data root). This is the only operational history file; there is no rotating
log or alert store. It contains a schema version, UTC start/finish times,
duration, fixed status/reason codes, exit code, a checkpoint count when known,
and allowlisted nonnegative counters. It excludes aliases, actors, profile
paths, post contents, URLs, cookies, and DOM data. A killed process may leave
`RUNNING`; treat that as interrupted cleanup and inspect the process and Task
Scheduler history before another attempt. Do not edit the status file.

`COMPLETE` means the invocation committed a valid checkpoint. It does not
claim that the entire feed was covered or that no rows were skipped. A
`COMPLETE_WITH_SKIPS` cycle may preserve verified observations while leaving
unresolved observations uncommitted. `STOPPED` means the invocation returned
without committing its current boundary; `FAILED` means it encountered an
error. Scheduled mode exits nonzero for either `STOPPED` or `FAILED`.

Use the status and fixed stop reason to triage:

| Status or reason | Operator action |
| --- | --- |
| `ACCOUNT_BUSY` | Check for another process using the profile. Do not start a parallel run. |
| `BROWSER_CLEANUP_FAILED` | Do not retry the profile. The run could not durably arm its guard, verify shutdown, or persist a clean guard state. Inspect and close the prior managed Chromium process locally, then use attended recovery after verifying it stopped. |
| `LOGIN_REQUIRED` | Run `tb login <alias>` in the logged-in desktop session. |
| `CHALLENGE_REQUIRED` | Open the profile manually and resolve the challenge. Do not automate it. |
| `RATE_LIMITED` | Stop further attempts and review manually before a later run. |
| `CYCLE_TIMEOUT` | Check browser and machine state; let bounded cleanup finish before retrying. |
| `STOPPED` | Review the fixed cycle status and reason; do not count it as a completed invocation. |
| `RUNNING` after process exit | Inspect the process and scheduler history; ensure Chromium closed before retrying. |

Before each managed nurture launch, the runner durably arms a per-profile guard
while holding the OS profile lock. It clears the guard, still under that lock,
only after context close and Playwright manager shutdown both complete
successfully. An interrupted process or unverified shutdown therefore leaves
the profile blocked. Cleanup diagnostics identify the fixed stages
`CLOSE_CONTEXT`, `STOP_PLAYWRIGHT`, or `QUARANTINE_LOCK`; the latter reports
that the guard could not be durably armed or disarmed, and the returned error
code is `BROWSER_CLEANUP_FAILED`.

After confirming the prior managed Chromium process stopped, run
`uv run tb recover <alias>` in the logged-in desktop session and type `CLOSED`.
Recovery clears only a valid armed guard. A malformed or unknown guard state is
rejected and cannot be repaired by `tb recover`; do not edit that file or reuse
the profile until the owner provides recovery instructions. Recovery does not
open a browser. If you cannot verify process closure, leave the block in place
and do not start another run for that profile.

Do not configure automatic retries. Sleep pauses headed browser work; logoff
removes the interactive desktop. Keep Task Scheduler history and any local
operator output private. Do not enable tracing, screenshots, transcript
capture, or shared log upload.

## Owner-supervised Auto live UAT

Auto's built-in eligibility policy is Vietnamese-only: a post must clearly
express current hiring or job-seeking intent using Vietnamese sentence
structure. English role titles may appear within a Vietnamese sentence;
English-only posts are not eligible. `--max-cycles 1` caps one completed,
checkpointed cycle, not one browser attempt. Bounded retries can start another
headed browser operation before the cycle completes or Auto stops.

Only the account owner runs live UAT, in the logged-in desktop session and on
their verified account/profile. First use `uv run tb ls` to check the intended
alias is verified. Keep the run in the foreground and confirm the visible,
headed Chromium session uses that account/profile; stop if it does not.
Replace `<verified-alias>` below with that alias:

```powershell
uv run tb auto <verified-alias> --max-cycles 1 --uat-diagnostics
```

During this bounded run, the owner checks that:

- only a clearly current Vietnamese hiring or job-seeking intent expressed by
  the post's own author is selected; an English title inside Vietnamese
  text is allowed, while English-only and third-party intent are not;
- the Home target's canonical permalink and author remain verified through
  final pre-click revalidation; Detail's canonical route, author, and complete
  body are verified before it is counted as read;
- Detail content is verified, Back returns to the expected Home / For You
  context and selected-post identity, and no other account action occurs;
- coverage status is truthful, incomplete coverage is not treated as complete,
  and only safe completed progress is checkpointed;
- the headed browser/context closes and the profile lock is released. If
  cleanup reports `BROWSER_CLEANUP_FAILED`, do not retry the profile; follow
  the attended recovery instructions above.

Read `--uat-diagnostics` output locally. It contains fixed phase/status codes
and allowlisted aggregate counters; it is not permission to share private
terminal output. Do not paste full logs, account aliases, actor names, profile
paths, post text, URLs, screenshots, or DOM data into GitHub or other shared
locations. If the expected target or a verification step is absent or
ambiguous, record UAT as incomplete rather than inferring success.

Engineering merge and green synthetic CI are the engineering gate only. They
do not establish current live UI compatibility. This bounded owner UAT is a
separate gate; it does not accept continuous foreground operation or enable
Task Scheduler. Scheduler activation still needs separate owner approval. Known
issue [#23](https://github.com/pumni/threads-browser/issues/23)
(`READ_IN_PROGRESS` at the last-scroll boundary) and [#47](https://github.com/pumni/threads-browser/issues/47)
(`ROOT_AMBIGUOUS` on a second Detail) remain separate, unresolved limits.

## Continuous foreground and scheduler acceptance

A successful one-cycle UAT does not accept continuous use. Before relying on a
multi-cycle foreground run, the owner separately supervises bounded cycles,
restart/deduplication behavior, retry and cooldown stops, incomplete coverage,
Detail/Back provenance, Ctrl+C handling, checkpoint continuity, and browser
cleanup. Do not infer this acceptance from CI, merge, or one successful cycle.

### Task Scheduler activation (owner-controlled)

Do not create or enable a recurring task until the owner has explicitly
approved all of the following in a separate decision:

- trigger cadence and whether any catch-up trigger is allowed;
- the one-file status retention window and local access control;
- recovery actions for `RUNNING`, login expiry, challenges, rate limits,
  timeouts, sleep, and logoff;
- the logged-in desktop, awake-machine, and timeout assumptions.

After that approval and separate successful supervised Windows UAT, the Windows
operator can create a task with the PowerShell wrapper above. Set it to run
only while the user is logged on, disallow overlapping instances, disable
automatic restart, and set its stop time beyond the configured runtime plus
browser/Playwright cleanup. Keep the task disabled until its action and
one-time trigger have been checked. A recurring trigger requires the separate
owner approval; do not infer it from a successful one-time run.

To stop an active task, use Task Scheduler's **End** action or
`Stop-ScheduledTask -TaskName 'approved-task-name'`. To prevent future starts,
use **Disable** or `Disable-ScheduledTask -TaskName 'approved-task-name'`.
Inspect the task's History, Last Run Result, and the sanitized `last_run.json`
locally. Do not paste full private logs or account details into GitHub.

Offline Windows tests exercise the PowerShell wrapper with a fake `uv` command
and local Chromium fixtures. They do not verify live Threads UI, real login
state, Task Scheduler, or sleep handling. Those checks require owner-operated
supervised UAT.
