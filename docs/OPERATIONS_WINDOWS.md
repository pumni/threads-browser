# Windows scheduled read-only cycles

The scheduled runner performs one headed, read-only Home / For You observation
per invocation. It is intended for a dedicated Threads Browser account and a
logged-in Windows desktop session. It does not establish login, solve
challenges, retry failures, or interact with posts.

## Prepare the account

1. Install the project and Chromium dependencies in the checkout with
   `uv sync --group dev` and `uv run playwright install chromium`.
2. Register a dedicated browser profile, verify its expected actor, and save
   explicit relevance terms. Use `tb login <alias>` interactively to establish
   the session; complete any login or security challenge manually.
3. Run one supervised `uv run tb run <alias>` cycle and inspect the result
   before scheduling it. Do not use `--detail` or either text-output option in
   the scheduled task.

## Create the Task Scheduler task

Create a Basic Task with the desired daily or weekly trigger. Set the action to
the installed `powershell.exe` or `pwsh.exe` and pass:

```text
-NoProfile -File "D:\path\to\threads-browser\scripts\windows\run-scheduled.ps1" -Alias local1
```

Replace the script path and alias with the local checkout and account alias.
The wrapper invokes `uv run --project <checkout> tb run` with a 720-second
runtime budget, bounded scan limits, `--no-readiness-prompt`, and `--scheduled`.
It reads the account's saved terms, so terms and profile paths do not appear in
the task action.

In Task Scheduler settings:

- Select **Run only when user is logged on** so headed Chromium has an
  interactive desktop.
- Set **If the task is already running** to **Do not start a new instance**.
  The application also holds the account's OS profile lock; a second process
  exits with `ACCOUNT_BUSY`.
- Set **Stop the task if it runs longer than** 15 minutes for the default
  720-second application deadline. This exceeds the deadline plus up to ten
  seconds of bounded browser and Playwright cleanup, with time for process
  startup and scheduler overhead.
- Leave **Restart every** disabled. A later scheduled trigger is the next
  attempt; operator-intervention failures do not retry immediately.
- Keep the machine awake and the user logged in for the trigger window. Sleep
  pauses browser work, and logoff removes the interactive desktop. If Windows
  misses a trigger while asleep, configure at most one catch-up run after
  wake, not a repeating restart loop.

## Check status and handle failures

Each `--scheduled` attempt atomically replaces
`%LOCALAPPDATA%\ThreadsBrowser\operations\last_run.json` (or the configured
`THREADS_BROWSER_DATA_ROOT\operations\last_run.json`). The single file records
UTC start and finish timestamps, duration, a sanitized status and stop reason,
exit code, checkpoint run count when known, and allowlisted numeric counters.
It contains no account alias, actor, profile path, post text, URL, cookie, or
DOM data. A new attempt first writes `RUNNING`; if Windows kills the process
before cleanup, that status remains and is evidence of an interrupted run.
Successful completion overwrites the same file, so status storage stays
bounded. The CLI output contains only operational statuses and counts unless
an operator explicitly enables a local-only diagnostic mode; the wrapper
rejects those text-output options in scheduled mode.

Use the exit code and `stop_reason` for triage:

| Status | Operator action |
| --- | --- |
| `ACCOUNT_BUSY` | Check for another run using the same profile. Do not start a parallel task. |
| `LOGIN_REQUIRED` | Run `tb login <alias>` in the logged-in desktop session. |
| `CHALLENGE_REQUIRED` | Open the profile manually and resolve the challenge. Do not automate it. |
| `RATE_LIMITED` | Stop scheduled attempts and review manually before scheduling a later run. |
| `CYCLE_TIMEOUT` | Review the browser and machine state; allow the next planned trigger after recovery. |
| `RUNNING` after the task ended | Treat as interrupted cleanup; inspect Task Scheduler history and the browser process before another attempt. |

Do not configure immediate retries for these cases. Keep Task Scheduler history
and any locally collected output on an access-controlled machine; do not attach
raw console transcripts to issues. The runner does not enable tracing,
screenshots, or transcript logging.

Offline tests cover the command and status format only. They do not verify the
live Threads UI, Task Scheduler behavior, login persistence, sleep handling,
or long-run Windows cleanup. Those remain owner-operated pilot checks.
