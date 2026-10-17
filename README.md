# Threads Browser

A small Python package for local Threads account sessions and bounded, read-only Home / For You exploration.

## Setup and daily use

Install the package and Chromium:

```powershell
uv sync
uv run playwright install chromium
```

Create a local account and open its persistent Chromium profile:

```powershell
uv run tb add local1
uv run tb login local1
```

Sign in and complete any security challenge in Chromium. The login command checks the visible Profile navigation control, verifies the resulting on-site actor route, and binds that actor to the account. It then returns to Threads Home and keeps Chromium open. Close the browser window to finish the command and release the profile lock. No terminal confirmation or credential entry is needed.

Save relevance terms and run the bounded nurture workflow:

```powershell
uv run tb terms local1 "hiring" "đang tuyển"
uv run tb run local1
```

`tb run` uses headed Chromium, at most 20 visible post rows per viewport, 8 observed feed advances, and 40 distinct verified posts. Terms are local relevance hints, not search queries. They are matched as literal substrings of extracted post text. Terms are stored under the private application data root in `preferences/terms`, separately from account metadata and Chromium session data.

Attended and legacy runs can opt into `--max-runtime-seconds` and
`--no-readiness-prompt`. For Windows Task Scheduler setup, bounded runtime
defaults, and the sanitized latest-run status file, see
[`docs/OPERATIONS_WINDOWS.md`](docs/OPERATIONS_WINDOWS.md).

Useful account and term commands:

```powershell
uv run tb ls
uv run tb terms local1
uv run tb terms local1 --clear
```

One-time terms override saved terms for a single run:

```powershell
uv run tb run local1 -t "remote"
uv run tb run local1 --term "hiring" --term "engineer"
```

The override does not change saved preferences. It also selects the matching policy fingerprint, so its checkpoints remain isolated from runs with other terms. Limits can be adjusted within the existing safety ranges:

```powershell
uv run tb run local1 --max-posts 10 --max-scrolls 3 --max-total-posts 20
```

An account without saved terms must supply `-t`/`--term` or configure terms with `tb terms`. `tb run` never opens a login browser. If the session has expired, it stops with `LOGIN_REQUIRED`; run `tb login local1` to authenticate in the same profile. An unverified account stops with `ACCOUNT_NOT_VERIFIED`, and a profile already in use stops with `ACCOUNT_BUSY`. `tb ls` prints aliases and verification status without showing actor names or profile paths.

## Existing accounts and Python APIs

`tb add alias` creates an unverified account with a unique account ID, account metadata, and a dedicated Chromium profile under `%LOCALAPPDATA%\ThreadsBrowser\profiles\alias` on Windows. Each alias gets its own account file and profile. Registration rejects duplicate aliases and does not replace an existing profile. Never copy another browser profile or import its cookies or authentication data.

To register an existing dedicated profile, the legacy module command retains its explicit options:

```powershell
uv run python -m threads_browser account add legacy1 --actor "@example" --profile-dir "C:\path\to\dedicated-profile"
```

For an absent, dedicated profile directory, add `--create-profile`. Existing schema version 1 accounts remain readable and keep their actor, account ID, and profile path. Existing accounts are not silently migrated or rebound.

The short `tb` commands use the same services as the retained module runner. Legacy commands remain available:

```powershell
uv run python -m threads_browser account add local1
uv run python -m threads_browser account login local1
uv run python -m threads_browser nurture run local1 --headed --max-posts 20 --max-scrolls 8 --max-total-posts 40 --term "hiring"
```

The package APIs remain available to other applications:

```python
import asyncio

from threads_browser import add_local_account, open_account_login

add_local_account("local1")
asyncio.run(open_account_login("local1"))
```

`add_local_account(alias)` and async `open_account_login(alias)` do not parse command-line arguments, call `input()`, or require a TTY. `open_account_login` accepts optional `data_root` and `status_callback` arguments. It holds the profile lock while Chromium is open. The account JSON contains account ID, alias, expected actor, and profile path; it never stores a password, cookies, or exported browser state.

Applications can call `threads_browser.workflow.run_browser_nurture(...)` as the supported Python entry point for a persistent-profile nurture cycle. It acquires the shared profile lock before launching Chromium and holds it through checkpoint persistence and browser shutdown attempts. The page-cycle runner is private; applications should not open their own persistent context and pass a `Page` into the workflow.

## Workflow scope and limits

The workflow verifies that For you is active and checks the visible profile control against the expected actor. It discovers candidate rows top-to-bottom, verifies and emits each row before reading the next, then advances with small measured overlapping wheel steps. A row whose permalink or body is outside the page or nested scrollport is `DEFERRED_OFFSCREEN` and is reconsidered after a wheel step. Partial or unknown rows are skipped while the bounded scan continues; a gap does not block a later verified selection. A candidate cap or scroll budget still ends the scan safely, and the result reports `COMPLETE_WITH_SKIPS` when the feed sample contains skipped rows.

When a relevant post is already visible and scroll budget remains, the workflow first requires one observed feed advance if the feed can scroll. It can then open that post only while its canonical permalink is still visible. If the bounded scan finds no relevant match, it may open a readable, provenance-verified current post as a fallback. Detail may contain a longer body when the Home card was explicitly truncated; the canonical route and matching body must still identify the selected post, and any conflicting author or permalink evidence rejects it. A semantic `main` landmark is optional. If the route is verified but the Detail body is unavailable or unverified, the workflow tries Back once; after Home, For you, account and selected-post identity are reverified, it saves Home progress and leaves the selected post pending for a later Detail attempt. Entering `q` cancels without Back or checkpointing. Back checks the selected post identity rather than its former screen coordinate, and does not reopen Profile after the initial actor check. A wrong route, blocker or lost return context stops without recording a completed run.

Display text without canonical post and matching author evidence is labeled `OBSERVED_UNVERIFIED`. It may be screened locally, but never becomes a verified count, Detail target, pending item, fingerprint, or checkpoint input. Provenance-verified rows keep canonical URL and matching author checks. The workflow opens at most one Detail, verifies the selected post's provenance and body, returns to the feed, and atomically saves fully read fingerprints and successful outcomes. Skipped partial or unknown rows are not marked complete and can be read in a later cycle. Post text and raw post URLs are never persisted or printed by default. Optional `--show-post-text` and `--show-observed-text` diagnostics require an interactive terminal and print explicit warnings.

Use `uv run tb run local1 --detail` for an opt-in trace of Home/account/For You readiness, per-row classifications, measured scrolls, selection reason, verified Home and Detail bodies, Back anchor result, checkpoint outcome, and structural phase timings (`discover_ms`, `read_row_ms`, `stabilize_ms`, `wait_reason`). It also supports `python -m threads_browser nurture run local1 --headed --max-posts 20 --term hiring --detail`. Each row is emitted as it is read, with `VERIFIED` kept distinct from `OBSERVED_UNVERIFIED`; Home truncation is distinguished from the verified Detail body. Partial observations may be printed with their unverified status, but do not become Detail targets or checkpoint input. The mode requires an interactive terminal before account data or Chromium is accessed. Terminal scrollback may retain post text: do not capture, paste, or share this output in GitHub, PRs, chats, or CI logs. Detail output is never written to checkpoints or default logs. The two narrower text flags remain independent and do not duplicate their output when combined with `--detail`.

The command prints `ui=UNVERIFIED_LIVE_UI`. Automated tests use local Chromium fixtures and do not sign in to Threads. These UI assumptions have only been exercised against local fixtures; an authorized operator must complete live headed UAT before relying on current Threads UI compatibility. Unsupported UI, rate limits, challenges, navigation failures, actor mismatches, ambiguous rows, and stalled scrolling stop with a fixed status instead of being bypassed.

The data root defaults to `%LOCALAPPDATA%\ThreadsBrowser` on Windows and `~/.local/share/threads-browser` elsewhere. Set `THREADS_BROWSER_DATA_ROOT` to select another private root. The profile lock coordinates processes using this repository and data root; it does not coordinate with threads-standalone. Never open the same profile in both CLIs concurrently. The browser lane does not use the official API and never likes, follows, comments, replies, posts, or sends messages.
