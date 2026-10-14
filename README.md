# Threads Browser

A small, independent, read-only Playwright CLI for bounded Home / For You observation.

## Setup

From this repository, install the package and its development tools, then install Chromium:

    uv sync
    uv run playwright install chromium

Register an existing, separately provisioned, dedicated Chromium automation profile (the directory passed to Playwright as user_data_dir). For example, use a path such as `C:\Users\you\AppData\Local\ThreadsBrowser\profiles\my-account`, chosen for this automation by the owner. Do not use the default desktop Chrome or Edge data directory, and do not copy cookies or another profile to work around this restriction. The command stores only the path reference and expected actor in the private Browser data root; it does not copy or inspect profile contents.

    uv run threads-browser account add my-account --actor "@example" --profile-dir "C:\Users\you\AppData\Local\ThreadsBrowser\profiles\my-account"

Run one headed, read-only Home / For You sample. Terms are local relevance hints; they are not search queries and do not navigate to Search.

    uv run threads-browser nurture run my-account --headed --max-posts 10 --term hiring --term tuyển-dụng

The data root defaults to %LOCALAPPDATA%\ThreadsBrowser on Windows and ~/.local/share/threads-browser elsewhere. Set THREADS_BROWSER_DATA_ROOT to choose another private root. Account metadata and versioned checkpoints stay there; post text and raw post URLs are not persisted or printed.

## Scope and limits

The command verifies that For you is active and checks the visible profile control against the expected actor. It samples up to `--max-posts` visible cards before and after one wheel action, deduplicates them by permalink, and prioritizes after-scroll cards when choosing what to open. It opens at most one relevant unread card, verifies its URL and author, returns to the feed, and atomically saves counts plus hashed dedupe and pending-read fingerprints. Relevant cards that could not be opened remain eligible on later runs; a card is removed from pending state after a verified detail read. `home_sampled` and `home_relevant_ratio` include all distinct cards in the bounded sample for that run; `home_new` and `duplicates` report dedupe counts separately. `scroll_attempts` counts the wheel action, while `scrolls` increases only when the after-sample shows a new permalink or a sampled card changes its viewport position. If movement is not observable, the run stops with `HOME_SCROLL_UNVERIFIED` without saving a completed checkpoint. It never likes, follows, replies, posts, sends messages, or accesses the official API.

Every invocation prints ui=UNVERIFIED_LIVE_UI. Actor verification searches accessible navigation for one uniquely named Profile/Your profile/Account link or button; if none exists, it accepts one visible exact-text Profile target in navigation. It clicks that target, checks the resulting on-site /@actor route against the registered actor, and returns to the exact Home URL. Missing or ambiguous controls, clicks without navigation, and unverified profile routes stop with fixed stage-specific codes. The reader supports semantic article cards and bounded non-article containers anchored by a canonical on-origin permalink plus matching author link; ambiguous multi-post containers fail closed. Body extraction requires one visible paragraph. The non-article path has only been exercised against local Chromium fixtures; owner diagnostics establish that live permalink links exist, but not their containing card structure. Owner-run, headed UAT is still required to establish compatibility with the current Threads UI. Login, challenge, redirect, ambiguous posts, and unsupported UI stop with a reason code.

The profile lock coordinates processes using this repository and data root. It does not coordinate with threads-standalone; never open the same profile in both CLIs concurrently.
