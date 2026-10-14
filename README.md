# Threads Browser

A small, independent, read-only Playwright CLI for bounded Home / For You observation.

## Setup

From this repository, install the package and its development tools, then install Chromium:

    uv sync
    uv run playwright install chromium

Register an existing signed-in Chromium user-data directory (the directory passed to Playwright as user_data_dir). The command stores only its path reference and expected actor in the private Browser data root; it does not copy or inspect profile contents.

    uv run threads-browser account add my-account --actor "@example" --profile-dir "C:\Users\you\AppData\Local\Google\Chrome\User Data"

Run one headed, read-only Home / For You sample. Terms are local relevance hints; they are not search queries and do not navigate to Search.

    uv run threads-browser nurture run my-account --headed --max-posts 10 --term hiring --term tuyển-dụng

The data root defaults to %LOCALAPPDATA%\ThreadsBrowser on Windows and ~/.local/share/threads-browser elsewhere. Set THREADS_BROWSER_DATA_ROOT to choose another private root. Account metadata and versioned checkpoints stay there; post text and raw post URLs are not persisted or printed.

## Scope and limits

The command verifies the visible profile control against the expected actor, samples accessible post cards, scrolls once, opens at most one relevant unseen card, verifies its URL and author, returns to the feed, and atomically saves counts plus hashed dedupe fingerprints. It never likes, follows, replies, posts, sends messages, or accesses the official API.

Every invocation prints ui=UNVERIFIED_LIVE_UI. The current assumptions are a visible For you link/tab, one Profile/Your profile/Account link in navigation, semantic article cards, a single paragraph body, and an @actor/post permalink. They have only been exercised against a minimal local Chromium page. Owner-run, headed UAT is required to establish compatibility with the current Threads UI. Login, challenge, redirect, ambiguous post, and selector failures stop with a reason code.

The profile lock coordinates processes using this repository and data root. It does not coordinate with threads-standalone; do not open the same profile in both CLIs concurrently.
