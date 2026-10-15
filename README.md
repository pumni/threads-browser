# Threads Browser

A small, independent, read-only Playwright CLI for bounded Home / For You exploration.

## Setup

From this repository, install the package and its development tools, then install Chromium:

    uv sync
    uv run playwright install chromium

### Windows PowerShell onboarding

Create a dedicated profile, then open it in visible Chromium and complete login yourself:

```powershell
# 1. Install dependencies and Chromium
uv sync
uv run playwright install chromium

# 2. Register the account and create its dedicated profile directory
uv run threads-browser account add my-account --actor "@example" --profile-dir "$env:LOCALAPPDATA\ThreadsBrowser\profiles\my-account" --create-profile

# 3. Log into Threads manually in the Chromium window
uv run threads-browser account login my-account --headed

# 4. Run the existing bounded, read-only workflow
uv run threads-browser nurture run my-account --headed --max-posts 2 --max-scrolls 8 --max-total-posts 40 --term "hiring"
```

`--create-profile` creates the requested profile directory only when that exact path is absent. It creates missing parent directories, rejects existing targets, explicit `..` path segments, and paths under default desktop Chrome or Edge profile roots, and does not follow symlinks or junctions while creating the path. Without the flag, `account add` keeps its existing behavior and requires an existing directory. Never use a default desktop browser data directory or copy another browser profile, cookies, or authentication data.

The account JSON keeps only the account ID, alias, expected actor, and profile path. It never stores credentials, cookies, or exported browser state. The login command launches persistent Chromium with that same profile path used by `nurture run`, opens `https://www.threads.com/`, and waits for you to finish login and any MFA or account challenge in the visible browser. The CLI does not accept a password. After you press Enter, it clicks the visible Profile control and checks the resulting on-site `/@actor` route against the registered actor. An incorrect account stops with `ACTOR_MISMATCH`; incomplete login stops with `LOGIN_REQUIRED`. After a failed check, you can correct the browser and explicitly retry once, or enter `q` to cancel.

Login requires `--headed` and an interactive terminal with both stdin and stdout attached to a terminal. It keeps Chromium open while waiting for your input. Cancellation, verification failure, and browser errors close the context and release the profile lock. The same lock prevents `account login` and `nurture run` from using one profile concurrently.

Chromium’s persistent profile is reused by later `nurture run` executions. If the session expires, the read-only workflow stops with `LOGIN_REQUIRED`; run `uv run threads-browser account login my-account --headed` to authenticate again. Re-login reuses the account ID and profile and does not rewrite account metadata.

If a profile directory already exists, register it without `--create-profile` only when it is a dedicated automation profile that you provisioned for this application:

```powershell
uv run threads-browser account add my-account --actor "@example" --profile-dir "C:\path\to\dedicated-profile"
```

Run a headed, read-only Home / For You search. `--max-posts` caps visible post rows accounted for in each viewport (1-20); if more candidate rows are visible than the cap, the run reports `VIEWPORT_COVERAGE_INCOMPLETE` and stops before scrolling, opening Detail, or saving a checkpoint. The defaults allow up to eight observed wheel advances and forty distinct provenance-verified posts per run. Terms are local relevance hints; they are not search queries and do not navigate to Search. Each `--term` is normalized and matched as a literal substring of extracted text. Configure job-seeking and hiring phrases separately when both matter, for example `--term "đang tìm việc" --term "đang tuyển"`.

    uv run threads-browser nurture run my-account --headed --max-posts 20 --max-scrolls 8 --max-total-posts 40 --term hiring --term tuyển-dụng

The data root defaults to %LOCALAPPDATA%\ThreadsBrowser on Windows and ~/.local/share/threads-browser elsewhere. Set THREADS_BROWSER_DATA_ROOT to choose another private root. Account metadata and versioned checkpoints stay there; post text and raw post URLs are never persisted. Text is not printed by default. `--show-post-text` previews only provenance-verified text on a TTY. `--show-observed-text` is a separate TTY-only opt-in for `OBSERVED_UNVERIFIED` text; its warning identifies the lower trust level and terminal scrollback risk.

## Scope and limits

The command verifies that For you is active and checks the visible profile control against the expected actor. It accounts for every candidate row in the viewport up to the explicit cap, then advances with one measured overlapping wheel only after coverage is complete. Each row is either read from its own safely scoped text, marked uncertain, or skipped with a fixed reason. Display text without canonical post and matching author evidence is labeled `OBSERVED_UNVERIFIED`; it may be screened locally, but never becomes a verified count, Detail target, pending item, fingerprint, or checkpoint input. Provenance-verified rows keep the canonical URL and matching author checks. The CLI opens at most one Detail, verifies its provenance and body, returns to the feed, and atomically saves only verified fingerprints and counts. `NO_VERIFIED_POSTS_IN_BUDGET`, `OBSERVATION_UNCERTAIN_ONLY`, `OBSERVED_UNVERIFIED_ONLY`, and `UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION` do not save a completed checkpoint. A stalled wheel stops with `HOME_SCROLL_UNVERIFIED` without a checkpoint. The CLI never likes, follows, replies, posts, sends messages, or accesses the official API.

Every `nurture run` invocation prints `ui=UNVERIFIED_LIVE_UI`. Actor verification searches accessible navigation for one uniquely named Profile/Your profile/Account link or button; if none exists, it accepts one visible exact-text Profile target in navigation. It clicks that target, checks the resulting on-site `/@actor` route against the registered actor, and returns to the exact Home URL. Manual login reuses the same visible-control and route check without scanning feed posts. Missing or ambiguous controls, clicks without navigation, and unverified profile routes stop with a fixed code. The reader scans bounded semantic roots and safely inferred unmarked rows using visible geometry, a timestamp, and profile-author evidence; these are synthetic-tested discovery rules, not claims about the live Threads DOM. It reads only the scoped row's own text and excludes controls, navigation, composer content, nested cards, and quoted content. Provenance verification remains a separate strict check for a canonical on-origin permalink and matching author in that same row. These paths have only been exercised against local Chromium fixtures; owner-run headed UAT is still required to establish compatibility with the current Threads UI. Login, challenge, redirect, ambiguous rows, and unsupported UI stop with a reason code.

The profile lock coordinates processes using this repository and data root. It does not coordinate with threads-standalone; never open the same profile in both CLIs concurrently.
