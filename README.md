# Threads Browser

A small Python package for local Threads account sessions and bounded, read-only Home / For You exploration.

## Setup

From this repository, install the package and its development tools, then install Chromium:

    uv sync
    uv run playwright install chromium

### Windows PowerShell onboarding

Create accounts and open Chromium through the Python service API:

```powershell
uv sync
uv run playwright install chromium

@'
from threads_browser import add_local_account

add_local_account("local1")
'@ | uv run python -

@'
import asyncio
from threads_browser import open_account_login

asyncio.run(open_account_login("local1"))
'@ | uv run python -

# Later, run the same open_account_login("local1") snippet again.
```

`account add local1` creates a unique account ID, account metadata, and a separate Chromium profile under `%LOCALAPPDATA%\ThreadsBrowser\profiles\local1`. The account begins unverified with no expected actor. Each alias gets its own account file and profile. Registration rejects duplicate aliases and does not replace an existing profile. The application rejects unsafe symlinks, junctions, path traversal, and default Chrome or Edge desktop profile roots. Never copy another browser profile or import its cookies or authentication data.

On the first `account login`, Chromium opens visibly at Threads. Sign in and complete any MFA or security challenge directly in the official browser UI. The CLI detects the visible Profile navigation control, clicks it, verifies the resulting on-site `/@actor` route, and atomically binds that actor to the local account. It then returns to Threads Home and leaves Chromium open. No terminal confirmation or password entry is needed. Close the Chromium window when finished; the command then exits and releases the profile lock. Ctrl+C is also a cancellation path.

Later logins open the same persistent profile and check the live session. If it is still signed in as the bound actor, the CLI reports `SESSION_REUSED` and `VERIFIED`, navigates to Threads Home, and keeps the browser open. Chromium stores its session only in its own profile. The account JSON contains the account ID, alias, expected actor, and profile path; it never stores a password, cookies, or exported browser state. The CLI does not automate credential entry or bypass verification.

If a session expires, `nurture run` stops with `LOGIN_REQUIRED`. Call `asyncio.run(open_account_login("local1"))` to authenticate manually again in the same profile. This preserves the account ID, actor binding, and checkpoints. A different signed-in actor stops with `ACTOR_MISMATCH`; normal login never relinks an account. If the account has not completed its first verified login, `nurture run` stops with `ACCOUNT_NOT_VERIFIED`.

`add_local_account(alias)` and the async `open_account_login(alias)` are exported from `threads_browser`. They do not parse command-line arguments, call `input()`, or require a TTY. `open_account_login` accepts an optional `data_root` for application integration and an optional status callback for `WAITING_FOR_AUTHENTICATION`, `SESSION_REUSED`, `VERIFIED`, and `BROWSER_OPEN`. It holds the profile lock for the full time Chromium is open. Do not run nurture or another login for the same account until that browser closes. Login always opens headed Chromium.

The `threads-browser` console command is no longer installed. For development and debugging, the retained module runner can start the bounded read-only workflow:

```powershell
uv run python -m threads_browser.cli nurture run local1 --headed --max-posts 2 --max-scrolls 1 --max-total-posts 5 --term "hiring"
```

Applications can call `threads_browser.workflow.run_browser_nurture(...)` directly when they need to integrate the nurture workflow without this development runner.

### Existing accounts

Existing schema version 1 accounts remain readable and keep their registered actor, account ID, and profile path. Continue registering a pre-existing dedicated automation profile through `AccountStore`:

```python
from pathlib import Path

from threads_browser.storage import AccountStore, resolve_data_root

account = AccountStore(resolve_data_root()).add(
    "legacy1", "@example", Path(r"C:\path\to\dedicated-profile")
)
```

For compatibility, `AccountStore.add(alias, actor, profile_directory, create_profile=True)` can create an absent dedicated directory; without `create_profile`, explicit registration still requires the directory to exist. `add_local_account(alias)` creates a new unverified local account. Existing accounts are not silently migrated or rebound.

### Owner-run live UI UAT

Automated tests use local Chromium fixtures and do not sign in to Threads. Before relying on the current live UI, an authorized operator should run this Windows PowerShell check with a test account:

1. Call `add_local_account("local1")`, then `asyncio.run(open_account_login("local1"))` and sign in manually.
2. Confirm the status callback reports `VERIFIED` and that the saved account actor matches the profile reached from the visible Profile control.
3. Close Chromium, then call `asyncio.run(open_account_login("local1"))` again. Confirm the status callback reports `SESSION_REUSED`, returns to Threads Home, and leaves Chromium open.
4. Close Chromium and call `run_browser_nurture(...)` or use `uv run python -m threads_browser.cli nurture run local1 --headed --max-posts 2 --max-scrolls 1 --max-total-posts 5 --term "hiring"`. Confirm the same profile is used and the bounded, read-only workflow completes safely.

Live Threads UI compatibility is **UNVERIFIED_LIVE_UI** until this owner-run UAT has been completed. Unsupported UI, rate limits, challenges, navigation failures, and actor mismatches stop with a fixed status instead of being bypassed.

Run a headed, read-only Home / For You search. `--max-posts` caps visible post rows accounted for in each viewport (1-20); if more candidate rows are visible than the cap, the run reports `VIEWPORT_COVERAGE_INCOMPLETE` and stops before scrolling, opening Detail, or saving a checkpoint. The defaults allow up to eight observed wheel advances and forty distinct provenance-verified posts per run. Terms are local relevance hints; they are not search queries and do not navigate to Search. Each `--term` is normalized and matched as a literal substring of extracted text. Configure job-seeking and hiring phrases separately when both matter, for example `--term "đang tìm việc" --term "đang tuyển"`.

    uv run python -m threads_browser.cli nurture run my-account --headed --max-posts 20 --max-scrolls 8 --max-total-posts 40 --term hiring --term tuyển-dụng

The data root defaults to %LOCALAPPDATA%\ThreadsBrowser on Windows and ~/.local/share/threads-browser elsewhere. Set THREADS_BROWSER_DATA_ROOT to choose another private root. Account metadata and versioned checkpoints stay there; post text and raw post URLs are never persisted. Text is not printed by default. `--show-post-text` previews only provenance-verified text on a TTY. `--show-observed-text` is a separate TTY-only opt-in for `OBSERVED_UNVERIFIED` text; its warning identifies the lower trust level and terminal scrollback risk.

## Scope and limits

The command verifies that For you is active and checks the visible profile control against the expected actor. It accounts for every candidate row in the viewport up to the explicit cap, then advances with one measured overlapping wheel only after coverage is complete. Each row is either read from its own safely scoped text, marked uncertain, or skipped with a fixed reason. Display text without canonical post and matching author evidence is labeled `OBSERVED_UNVERIFIED`; it may be screened locally, but never becomes a verified count, Detail target, pending item, fingerprint, or checkpoint input. Provenance-verified rows keep the canonical URL and matching author checks. The CLI opens at most one Detail, verifies its provenance and body, returns to the feed, and atomically saves only verified fingerprints and counts. `NO_VERIFIED_POSTS_IN_BUDGET`, `OBSERVATION_UNCERTAIN_ONLY`, `OBSERVED_UNVERIFIED_ONLY`, and `UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION` do not save a completed checkpoint. A stalled wheel stops with `HOME_SCROLL_UNVERIFIED` without a checkpoint. The CLI never likes, follows, replies, posts, sends messages, or accesses the official API.

Every `nurture run` invocation prints `ui=UNVERIFIED_LIVE_UI`. Actor verification searches accessible navigation for one uniquely named Profile/Your profile/Account link or button; if none exists, it accepts one visible exact-text Profile target in navigation. It clicks that target, checks the resulting on-site `/@actor` route against the registered actor, and returns to the exact Home URL. Manual login reuses the same visible-control and route check without scanning feed posts. Missing or ambiguous controls, clicks without navigation, and unverified profile routes stop with a fixed code. The reader scans bounded semantic roots and safely inferred unmarked rows using visible geometry, a timestamp, and profile-author evidence; these are synthetic-tested discovery rules, not claims about the live Threads DOM. It reads only the scoped row's own text and excludes controls, navigation, composer content, nested cards, and quoted content. Provenance verification remains a separate strict check for a canonical on-origin permalink and matching author in that same row. These paths have only been exercised against local Chromium fixtures; owner-run headed UAT is still required to establish compatibility with the current Threads UI. Login, challenge, redirect, ambiguous rows, and unsupported UI stop with a reason code.

The profile lock coordinates processes using this repository and data root. It does not coordinate with threads-standalone; never open the same profile in both CLIs concurrently.
