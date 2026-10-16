# Source architecture

This is a small modular monolith, not a service/microservice system.

```text
src/threads_browser/
  __init__.py, __main__.py   Public Python API and CLI module entry
  cli.py                     Argument parsing, terminal UX, result formatting
  account_service.py         Account creation and profile-lock orchestration
  login.py                   Human-operated persistent Chromium login
  reader.py                  Compatibility API and feed-observation coordinator
  workflow.py                Compatibility API and headed browser lifecycle
  storage.py                 Local accounts, terms, checkpoint and OS locking
  errors.py                  Shared UI failure type
  browser_ui/
    geometry.py              Shared viewport and nested-scrollport geometry
    scrolling.py             Bounded native wheel gesture engine
    session.py               Home, actor and For You UI verification
    feed_rows.py             Candidate-row discovery and structure checks
    body_text.py             Visible body progress and hydration evidence
    loading.py               Render settling and UI readiness observation
    detail.py                Exact Detail navigation and safe return
  nurture/
    engine.py                One bounded observation and selection cycle
    state.py                 Workflow phases, coverage, dedupe and run metrics
    diagnostics.py           Opt-in terminal observations and fixed telemetry
  feed/
    models.py                Feed value objects
    relevance.py             Pure text normalization and classification
    urls.py                  Strict canonical post/profile URL validation
```

## Dependencies and boundaries

- `feed` contains small, testable concepts. It may depend on `errors` and Playwright's `Locator` **type**, but not browser navigation or persistence.
- `browser_ui` owns reusable Playwright, Locator, DOM and geometry operations. Its modules do not access checkpoint files or decide relevance.
- `reader` preserves the current Python API and composes the UI operations for Home observation. It does not access checkpoint files or choose relevance policy.
- `nurture.engine` coordinates the reader and checkpoint store. `nurture.state` owns the finite run phases, coverage ledger, dedupe ledger and run statistics; diagnostics stay separate from those decisions.
- `workflow` preserves the current application entry points and owns only the headed browser context lifecycle. It delegates each page run to `nurture.engine`.
- `storage` owns persisted account references, terms, locks and versioned fingerprints; never store raw post bodies, cookies or raw URLs.
- `login` and `account_service` own interactive session establishment; `cli` is only an adapter around these application entry points.

Existing `threads_browser.reader` and `threads_browser.workflow` entry points remain compatibility APIs. New browser interaction code belongs in `browser_ui`; pure feed policy and canonical data belong in `feed`. The reader remains the integration point for feed scans so changes to geometry, row recognition, body hydration and Detail can be tested independently.

## Evolution

The earlier `feed` extraction's no-behavior-change scope applied to that refactor, not to future tasks. Selectors, readiness, scrolling, Detail and CLI behavior can evolve when needed. Preserve provenance checks, profile isolation, bounded read-only operation and checkpoint privacy; verify affected behavior with focused regression tests. Offline Chromium fixtures do not establish live Threads UI compatibility: that requires owner-authorized private UAT.

## Current nurture cycle

`nurture.engine` counts verified rows once across overlapping viewports while retaining the current visible locators needed for safe selection. Its state machine tracks observation, scroll, content wait, verification, selection, Detail, Back and checkpoint phases. When a relevant Home card appears before any scroll and the feed has room to move, it requires one real bounded wheel advance before Detail. Detail still opens through the verified Home permalink; a visibly truncated body may expand if the canonical author and post remain the same and the visible text is a prefix of the expanded body.

`browser_ui.session` verifies the For You shell and account route, and `browser_ui.detail` returns with browser history and checks the prior feed anchor without a second Profile visit. Wheel input, locators, route waits and native scroll events are the default. Shared geometry identifies an actual nested scroll surface, intersects document and nested scrollports for row eligibility, and confirms measured displacement. `feed_rows` discovers candidates, `body_text` tracks visible text fragments and hydration, and each observation is handed to `nurture.engine` immediately. Offscreen rows with verified identity are deferred through overlapping wheel steps; a partial or unknown row is skipped while later verified rows remain eligible. Before a Detail click, the reader revalidates the selected post's canonical identity, author and body, then uses Playwright mouse input at a visible, unobstructed permalink point so the click cannot auto-scroll an offscreen row into view.

`--detail` can report operator-only `discover_ms`, `read_row_ms`, `stabilize_ms` and a fixed `wait_reason` per viewport. These timings contain no body text, actor or URL. Verified observations and unverified display text remain distinct; printing an observation does not make it checkpoint eligible. Partial and unknown rows do not block a later verified selection. The checkpoint atomically records fully read fingerprints and successful Detail/Back outcomes; skipped rows remain eligible for a later cycle, and a run with skips reports `COMPLETE_WITH_SKIPS` while leaving viewport coverage incomplete. Failed verification of the selected post or a lost return context prevents checkpoint completion.
