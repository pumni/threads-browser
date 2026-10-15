# Threads Browser

A small, independent, read-only Playwright CLI for bounded Home / For You exploration.

## Setup

From this repository, install the package and its development tools, then install Chromium:

    uv sync
    uv run playwright install chromium

### Windows PowerShell onboarding

Create a dedicated Chromium profile, then open it in visible Chromium and complete login yourself:

```powershell
# 1. Install dependencies
uv sync
uv run playwright install chromium

# 2. Register the account and create its dedicated profile directory
uv run threads-browser account add my-account --actor "@example" --profile-dir "$env:LOCALAPPDATA\ThreadsBrowser\profiles\my-account" --create-profile

# 3. Log into Threads manually in the Chromium window
uv run threads-browser account login my-account --headed

# 4. Run the existing bounded, read-only workflow
uv run threads-browser nurture run my-account --headed --max-posts 2 --max-scrolls 8 --max-total-posts 40 --term "hiring"
```

`--create-profile` creates the requested directory only when that exact path is absent. It creates missing parent directories and rejects existing targets, explicit `..` path segments, symlinks or junctions in the path, and default desktop Chrome or Edge profile roots. Without the flag, `account add` keeps its existing behavior and requires an existing directory. Never copy another browser profile or import cookies or authentication data.

The account JSON keeps only the account ID, alias, expected actor, and profile path. It never stores credentials, cookies, or exported browser state. Login opens `https://www.threads.com/` in persistent Chromium using the same profile directory as `nurture run`. Enter your username and password and complete any MFA or account challenge in the browser. The CLI does not accept credentials. After you press Enter, it uses the visible Profile control and verifies the resulting `/@actor` route against the registered actor. An incorrect account stops with `ACTOR_MISMATCH`; incomplete login stops with `LOGIN_REQUIRED`. You may correct the browser and explicitly retry once, or enter `q` to cancel.

Login requires `--headed` and an interactive terminal with stdin and stdout attached to a terminal. Chromium remains open while the CLI waits for your confirmation. Cancellation, verification failure, and browser errors close the context and release the profile lock. The lock prevents login and `nurture run` from using the same profile concurrently.

Chromium's persistent profile is reused by later `nurture run` executions. If the session expires, the read-only workflow stops with `LOGIN_REQUIRED`; run `uv run threads-browser account login my-account --headed` to log in again. Re-login preserves the account ID and metadata.

To register an existing profile, omit `--create-profile` and use only a dedicated automation profile provisioned for this application:

```powershell
uv run threads-browser account add my-account --actor "@example" --profile-dir "C:\path\to\dedicated-profile"
```

Run a headed, read-only Home / For You search. `--max-posts` caps verified posts classified in each viewport; the defaults allow up to eight observed wheel advances and forty distinct verified posts per run. Terms are local relevance hints; they are not search queries and do not navigate to Search. Each `--term` is normalized and matched as a literal substring of safely extracted, verified post text. This does not infer semantic job relevance: configure job-seeking and hiring phrases separately when both matter, for example `--term "đang tìm việc" --term "đang tuyển"`.

    uv run threads-browser nurture run my-account --headed --max-posts 2 --max-scrolls 8 --max-total-posts 40 --term hiring --term tuyển-dụng

The data root defaults to %LOCALAPPDATA%\ThreadsBrowser on Windows and ~/.local/share/threads-browser elsewhere. Set THREADS_BROWSER_DATA_ROOT to choose another private root. Account metadata and versioned checkpoints stay there; post text and raw post URLs are not persisted or printed.

## Scope and limits

The command verifies that For you is active and checks the visible profile control against the expected actor. It classifies verified posts at each viewport, then advances with one mouse wheel only when that viewport has no relevant match. A rejected candidate is skipped without reading its body or using it as a target. The scan stops at either budget, or as soon as it finds a relevant unread post to open. It opens at most one Detail, verifies the canonical URL, author, and body, returns to the feed, and atomically saves counts plus fingerprint-only dedupe and pending-read state. Relevant posts that could not be opened remain eligible on later runs; a post is removed from pending state after a verified Detail read. `home_sampled` and `home_relevant_ratio` include distinct verified posts in the run; `home_new` and `duplicates` report dedupe separately. `scroll_attempts` counts issued wheel events, while `scrolls` counts advances with observed scroll displacement. `NO_RELEVANT_POST_IN_BUDGET` is a completed bounded search; `NO_VERIFIED_POSTS_IN_BUDGET` means the run found no post it could classify and does not save a completed checkpoint. A stalled wheel stops with `HOME_SCROLL_UNVERIFIED` without a checkpoint. The CLI never likes, follows, replies, posts, sends messages, or accesses the official API.

Every `nurture run` invocation prints ui=UNVERIFIED_LIVE_UI. Actor verification searches accessible navigation for one uniquely named Profile/Your profile/Account link or button; if none exists, it accepts one visible exact-text Profile target in navigation. It clicks that target, checks the resulting on-site /@actor route against the registered actor, and returns to the exact Home URL. Manual login uses the same visible Profile control and route check without scanning feed posts. Missing or ambiguous controls, clicks without navigation, and unverified profile routes stop with fixed stage-specific codes. The reader scans bounded semantic article roots, candidate non-article roots marked `data-pressable-container="true"`, and permalink-anchored ancestor fallbacks. That attribute is a discovery hypothesis and is not a claim about the live Threads DOM. Every accepted post still needs a canonical on-origin permalink and a matching author profile link in the same scoped card; ambiguous multi-post containers fail closed. After that identity check, body extraction can use a single visible paragraph or multiple top-level `span[dir="auto"]` / `div[dir="auto"]` text nodes, while excluding controls, navigation, composer content, nested cards, and quotes. Ambiguous body structure stays unknown. If visible card-like content has no verifiable canonical post identity, the reader reports `FEED_VISIBLE_UNSUPPORTED` rather than classifying or previewing it. These paths have only been exercised against local Chromium fixtures; owner-run headed UAT is still required to establish compatibility with the current Threads UI. Login, challenge, redirect, ambiguous posts, and unsupported UI stop with a reason code.

The profile lock coordinates processes using this repository and data root. It does not coordinate with threads-standalone; never open the same profile in both CLIs concurrently.
