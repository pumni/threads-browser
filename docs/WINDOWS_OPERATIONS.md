# Bounded Windows operation (read-only)

This guide applies to the **headed, read-only** `tb run` workflow. It is not a guarantee of 24/7 availability or Threads UI compatibility. It does not authorize engagement, scraping private data, bypassing challenges, or evading rate limits.

## Choose short scheduled runs, not a persistent worker

Use Windows Task Scheduler to start one bounded cycle and exit. Each cycle creates and closes a Playwright persistent Chromium context, checks the bound Threads actor, samples For You, optionally opens one verified Detail, returns Home, and updates a fingerprint-only checkpoint. The workflow remains read-only.

Prerequisites: Windows desktop session with the account owner **logged on**, locally installed Chromium via `uv run playwright install chromium`, a dedicated verified account (`tb login <alias>`), and configured terms. Do not copy profiles between machines or users. A headed Playwright browser is not reliable in a background session without an interactive desktop.

Suggested starting budgets (tune only after private UAT):

```powershell
uv run tb run local1 --max-posts 10 --max-scrolls 3 --max-total-posts 20
```

Run manually and confirm a successful Home / Detail / Back cycle before scheduling. These terms are configured separately with `tb terms`. Never add `--detail`, `--show-post-text`, or `--show-observed-text` to a scheduled task.

## Task Scheduler settings

1. **General:** select **Run only when user is logged on**. Use the same Windows account as the dedicated browser profile; avoid elevated privileges.
2. **Triggers:** start with separated periods in the day, for example 09:00, 13:00 and 18:00 local time. Do not configure rapid retries or catch-up bursts after sleep/reboot.
3. **Action:** point Program to the installed `uv.exe` (absolute path); use arguments `run tb run local1 --max-posts 10 --max-scrolls 3 --max-total-posts 20`; set *Start in* to the repository checkout directory. Verify the exact command and paths manually first. Task Scheduler does not infer working directory from the executable.
4. **Settings:** **If the task is already running: Do not start a new instance**. Set **Stop the task if it runs longer than** to an operator-approved ceiling (e.g. 30 minutes). Do not enable an automatic restart loop. Disable immediate catch-up of multiple missed starts.
5. **Conditions:** decide explicitly whether to run on battery. After Windows sleep/logoff, treat missed periods as missed; wait for the next scheduled trigger rather than launching multiple compensating runs.

The same-profile `profile_lock` prevents overlapping invocations that share the same application data root. It does **not** coordinate with a separate tool using the Chromium profile, and is not a substitute for the Task Scheduler no-overlap setting. Configure `THREADS_BROWSER_DATA_ROOT` consistently for registration, login and all scheduled runs.

## Time limits, recovery and cancellation

CLI limits cap visible rows and observed scroll advances; they are **not a strict end-to-end wall-clock deadline**. Interactive readiness extension can continue when an operator repeatedly opts in. A scheduled non-interactive run does not receive that prompt, but should still have the Task Scheduler execution ceiling above.

A task terminated externally may not run Python cleanup handlers. The OS releases its file lock after the process exits; the last successfully written atomic checkpoint remains. Do not infer that the interrupted cycle completed. Before restarting after a crash, verify no orphaned Chromium process is still using the dedicated profile. Never delete live Chromium lock files to force entry.

Use `Ctrl+C` for foreground operator cancellation. Do not automate reauthentication, challenge completion, account switching, or repeated immediate restart after errors.

## Run-result triage

| Result / signal | Meaning | Next step |
| --- | --- | --- |
| `COMPLETE`, `COMPLETE_WITH_SKIPS` | Bounded cycle finished; skips do not mean crash | Continue next scheduled run |
| `DETAIL_RETRY_PENDING` | Verified Home progress saved; Detail not counted successful | Preserve checkpoint, retry in a future normal cycle |
| `MAX_SCROLLS`, no new post, `FEED_END_REACHED` | Budget or feed boundary; not necessarily a bug | Observe trends across cycles; do not increase limits reflexively |
| `ACCOUNT_BUSY` | Same-profile process lock could not be taken | Find the other legitimate owner; do not bypass the lock |
| `LOGIN_REQUIRED`, `CHALLENGE_REQUIRED`, rate-limit/account restriction | Operator action required | Suspend the scheduled task; use authorized manual login/review |
| `ACTOR_MISMATCH`, `OFF_ORIGIN_REDIRECT`, `RETURN_CONTEXT_CHANGED` | Identity/navigation guard rejected evidence | Suspend and investigate; do not count cycle complete |
| `STATE_INVALID`, `STATE_PATH_INVALID` | Checkpoint or local storage rejected | Stop and inspect private state; do not overwrite it or silently reset |
| Repeated `UI_READ_UNVERIFIED`, `HOME_SCROLL_UNVERIFIED`, `BROWSER_SESSION_UNAVAILABLE` | UI/network/browser compatibility or lifecycle problem | Pause retries and inspect locally before resuming |

The CLI prints a default machine-readable `NURTURE status=...` summary and returns nonzero for exceptions. Some non-complete workflow outcomes are structured results rather than crashes, and may still use process exit 0; **do not monitor only exit code**. Prefer the status field, last-run timestamp, `checkpoint_runs`, and resource usage together.

## Resource and privacy policy

Start with one Chrome profile active at a time. Record the duration, peak RAM of Chromium/process tree, latest status, scrolls, verified Home count, Detail reads, and checkpoint run number. Compare the first, middle and last cycles of a day before increasing frequency. A synthetic offline multi-cycle soak test is still required before production claims.

Default logs must not include raw post bodies, authors, post URLs, cookies, credentials, or DOM dumps. Use only default CLI output for scheduled runs. Limit local log size and access via an operator-controlled log collector or log rotation policy, for example retain 7–14 days and cap each file to a small size. Do not upload private logs or browser profiles to CI, GitHub, or a shared monitoring service.

If identity or access restrictions change, **stop rather than retry more aggressively**. Every new scheduling pattern should first be exercised under authorized, supervised, headed UAT.
