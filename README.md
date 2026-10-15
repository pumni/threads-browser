# Threads Browser

A small, independent, read-only Playwright CLI for bounded Home / For You exploration.

## Setup

From this repository, install the package and its development tools, then install Chromium:

    uv sync
    uv run playwright install chromium

Register an existing, separately provisioned, dedicated Chromium automation profile (the directory passed to Playwright as user_data_dir). For example, use a path such as `C:\Users\you\AppData\Local\ThreadsBrowser\profiles\my-account`, chosen for this automation by the owner. Do not use the default desktop Chrome or Edge data directory, and do not copy cookies or another profile to work around this restriction. The command stores only the path reference and expected actor in the private Browser data root; it does not copy or inspect profile contents.

    uv run threads-browser account add my-account --actor "@example" --profile-dir "C:\Users\you\AppData\Local\ThreadsBrowser\profiles\my-account"

Run a headed, read-only Home / For You search. `--max-posts` caps verified posts classified in each viewport; the defaults allow up to eight observed wheel advances and forty distinct verified posts per run. Terms are local relevance hints; they are not search queries and do not navigate to Search. Each `--term` is normalized and matched as a literal substring of safely extracted, verified post text. This does not infer semantic job relevance: configure job-seeking and hiring phrases separately when both matter, for example `--term "đang tìm việc" --term "đang tuyển"`.

    uv run threads-browser nurture run my-account --headed --max-posts 2 --max-scrolls 8 --max-total-posts 40 --term hiring --term tuyển-dụng

The data root defaults to %LOCALAPPDATA%\ThreadsBrowser on Windows and ~/.local/share/threads-browser elsewhere. Set THREADS_BROWSER_DATA_ROOT to choose another private root. Account metadata and versioned checkpoints stay there; post text and raw post URLs are not persisted or printed.

## Scope and limits

The command verifies that For you is active and checks the visible profile control against the expected actor. It classifies verified posts at each viewport, then advances with one mouse wheel only when that viewport has no relevant match. A rejected candidate is skipped without reading its body or using it as a target. The scan stops at either budget, or as soon as it finds a relevant unread post to open. It opens at most one Detail, verifies the canonical URL, author, and body, returns to the feed, and atomically saves counts plus fingerprint-only dedupe and pending-read state. Relevant posts that could not be opened remain eligible on later runs; a post is removed from pending state after a verified Detail read. `home_sampled` and `home_relevant_ratio` include distinct verified posts in the run; `home_new` and `duplicates` report dedupe separately. `scroll_attempts` counts issued wheel events, while `scrolls` counts advances with observed scroll displacement. `NO_RELEVANT_POST_IN_BUDGET` is a completed bounded search; `NO_VERIFIED_POSTS_IN_BUDGET` means the run found no post it could classify and does not save a completed checkpoint. A stalled wheel stops with `HOME_SCROLL_UNVERIFIED` without a checkpoint. The CLI never likes, follows, replies, posts, sends messages, or accesses the official API.

Every invocation prints ui=UNVERIFIED_LIVE_UI. Actor verification searches accessible navigation for one uniquely named Profile/Your profile/Account link or button; if none exists, it accepts one visible exact-text Profile target in navigation. It clicks that target, checks the resulting on-site /@actor route against the registered actor, and returns to the exact Home URL. Missing or ambiguous controls, clicks without navigation, and unverified profile routes stop with fixed stage-specific codes. The reader scans bounded semantic article roots, candidate non-article roots marked `data-pressable-container="true"`, and permalink-anchored ancestor fallbacks. That attribute is a discovery hypothesis and is not a claim about the live Threads DOM. Every accepted post still needs a canonical on-origin permalink and a matching author profile link in the same scoped card; ambiguous multi-post containers fail closed. After that identity check, body extraction can use a single visible paragraph or multiple top-level `span[dir="auto"]` / `div[dir="auto"]` text nodes, while excluding controls, navigation, composer content, nested cards, and quotes. Ambiguous body structure stays unknown. If visible card-like content has no verifiable canonical post identity, the reader reports `FEED_VISIBLE_UNSUPPORTED` rather than classifying or previewing it. These paths have only been exercised against local Chromium fixtures; owner-run headed UAT is still required to establish compatibility with the current Threads UI. Login, challenge, redirect, ambiguous posts, and unsupported UI stop with a reason code.

The profile lock coordinates processes using this repository and data root. It does not coordinate with threads-standalone; never open the same profile in both CLIs concurrently.
