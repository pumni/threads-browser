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
| `BROWSER_CLEANUP_FAILED` | Do not retry the profile. Inspect and close the prior managed Chromium process locally, then use attended recovery after verifying it stopped. |
| `LOGIN_REQUIRED` | Run `tb login <alias>` in the logged-in desktop session. |
| `CHALLENGE_REQUIRED` | Open the profile manually and resolve the challenge. Do not automate it. |
| `RATE_LIMITED` | Stop further attempts and review manually before a later run. |
| `CYCLE_TIMEOUT` | Check browser and machine state; let bounded cleanup finish before retrying. |
| `STOPPED` | Review the fixed cycle status and reason; do not count it as a completed invocation. |
| `RUNNING` after process exit | Inspect the process and scheduler history; ensure Chromium closed before retrying. |

Cleanup diagnostics identify only the fixed stages `CLOSE_CONTEXT` or
`STOP_PLAYWRIGHT`; `QUARANTINE_LOCK` means the persistent block itself could not
be recorded. A recorded cleanup failure leaves a per-profile block so another
process cannot reuse that profile after the original process exits. If
`QUARANTINE_LOCK` appears, keep the profile out of use until you have verified
the prior browser process stopped.
Inspect and close the prior managed Chromium window/process locally. Once you
have verified it stopped, run `uv run tb recover <alias>` in the logged-in
desktop session and type `CLOSED`. This command only clears the block; it does
not open a browser. If you cannot verify process closure, leave the block in
place and do not start another run for that profile.

Do not configure automatic retries. Sleep pauses headed browser work; logoff
removes the interactive desktop. Keep Task Scheduler history and any local
operator output private. Do not enable tracing, screenshots, transcript
capture, or shared log upload.

## Task Scheduler activation (owner-controlled)

Do not create or enable a recurring task until the owner has explicitly
approved all of the following in a separate decision:

- trigger cadence and whether any catch-up trigger is allowed;
- the one-file status retention window and local access control;
- recovery actions for `RUNNING`, login expiry, challenges, rate limits,
  timeouts, sleep, and logoff;
- the logged-in desktop, awake-machine, and timeout assumptions.

After that approval and successful supervised Windows UAT, the Windows
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
