# Repository guidance

Threads Browser is an independent local Playwright CLI. Browser runs are foreground, headed, bounded, and read-only; there is no official API client or LLM runtime.

**Hard boundaries:** No automatic engagement, stealth, CAPTCHA bypass, private GraphQL/XHR, credential extraction, profile copying, or official API dependencies. Preserve actor/post provenance, profile isolation, scan limits, and checkpoint privacy. Never inspect operator profiles or expose profile paths, actor handles, post bodies, cookies, or raw post URLs in logs, test output, or PRs.

**Find context on demand:** `docs/ARCHITECTURE.md` maps module ownership; `README.md` documents CLI behavior. Read task-relevant code and tests rather than loading the entire repository.

**Verify proportionally:** Use fresh synthetic data roots and local-only Chromium fixtures. Run focused tests first; for substantive code changes, run `uv run ruff check .` and `uv run pytest -q --maxfail=1` when feasible, and report any checks not run. Offline tests do not verify live Threads UI; report assumptions as `UNVERIFIED_LIVE_UI` until owner-authorized private UAT.
