# Source architecture

This is a small modular monolith, not a service/microservice system.

```text
src/threads_browser/
  __init__.py, __main__.py   Public Python API and CLI module entry
  cli.py                     Argument parsing, terminal UX, result formatting
  account_service.py         Account creation and profile-lock orchestration
  login.py                   Human-operated persistent Chromium login
  reader.py                  Compatibility API and UI collaborator composition
  reader_facade/              Feature-group compatibility methods for ThreadsHomeReader
  workflow.py                Compatibility API and headed browser lifecycle
  storage.py                 Accounts, terms, profile locks, checkpoints and scheduled status
  errors.py                  Shared UI failure type
  browser_ui/
    geometry.py              Shared viewport and nested-scrollport geometry
    scrolling.py             Bounded native wheel gesture engine
    session.py               Home, actor and For You UI verification
    feed_rows.py             Candidate-row records and scanner compatibility seam
    root_discovery.py        Visible row discovery and adjacent-viewport identity carry
    viewport_scan.py         Scan request and result compatibility coordinator
    viewport_row_verifier.py Per-row provenance, body and coverage decisions
    card_provenance.py       Canonical card identity, body verification and recovery
    body_text.py             Body-reader compatibility facade
    body_progress.py         Body evidence mapping and scan-count updates
    body_progress_state.py   Run-local text ranges, fragments and geometry coverage
    loading.py               Render settling and UI readiness observation
    detail.py                Exact Detail navigation and safe return
  nurture/
    engine.py                Cycle setup and compatibility entry point
    scan_cycle.py            Bounded viewport observation and target selection loop
    finish_cycle.py          Detail/Back outcome resolution and checkpoint boundary
    cycle_context.py         Run-local collaborator references and counters
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
- `browser_ui.card_provenance` owns card-level canonical permalink, author and body verification, ambiguous-row recovery, stable-card revalidation and the run-local Home body cache. It composes `FeedRowScanner`, `BodyTextReader` and `LoadingObserver`; the reader facade retains scan orchestration, counters, fallback permalink traversal and compatibility wrappers.
- `reader` preserves the current Python API and composes the UI operations for Home observation. It does not access checkpoint files or choose relevance policy.
- `nurture.engine` coordinates the reader and checkpoint store. `nurture.state` owns the finite run phases, coverage ledger, dedupe ledger and run statistics; diagnostics stay separate from those decisions.
- `workflow` owns the headed browser context and shared profile-lock lifecycle. Its supported public persistent-profile nurture entry point is `run_browser_nurture`; it acquires the profile lock before browser startup and holds it through checkpoint persistence and browser shutdown attempts. The private `_run_page_workflow_locked` runner requires that lock to be held by its caller and must not acquire it again. There is no supported caller-owned `Page` workflow API. It delegates each page run to the private cycle in `nurture.engine`.
- `storage` owns persisted account references, terms, locks and versioned fingerprints; never store raw post bodies, cookies or raw URLs.
- `login` and `account_service` own interactive session establishment; `cli` is only an adapter around these application entry points.

## Responsibility boundaries

The Home reader remains the public integration point, while its Playwright work is split by ownership:

- `reader.ThreadsHomeReader` keeps the established API and per-page configuration/state surface. `reader_facade` groups its compatibility methods by body, Detail, feed, navigation and state concerns; the reader composes UI collaborators without owning their algorithms.
- `browser_ui.home_navigation`, `login_navigation` and `home_render` own route, actor and render readiness observations. `feed_observation` coordinates bounded readiness-driven reads; `root_discovery` owns row discovery/carry, while `candidate_structure` reports privacy-safe structural cues.
- `feed_scan.FeedScanSession` binds one scan's hooks to `viewport_scan.ViewportScanCoordinator`. The coordinator owns scan-local state; `viewport_candidate` classifies structural candidates, `viewport_row_verifier` reads and verifies them, and `viewport_row_recovery` owns bounded hydration and ambiguous-provenance handling. `viewport_scan_models` contains records shared within that scan.
- `body_snapshot` captures bounded visible text and geometry without retaining it. `BodyProgressTracker` maps those observations to public evidence and counters; `BodyProgressState` owns the run-local fragments, range evidence and coverage geometry. `BodyVisibilityCoverage` owns visibility across adjacent viewports. `body_text.BodyTextReader` preserves the former reader API and composes those responsibilities.
- `card_provenance` owns canonical author/permalink verification and stable card reads. `detail` owns verified Detail navigation and return. `scroll_evidence` attributes row geometry changes to the measured wheel surface; `scroll_session` owns per-reader measured scroll progress; `geometry` and `scrolling` provide shared stateless geometry and bounded wheel primitives.
- `nurture.engine` validates inputs, creates cycle collaborators and preserves the private page-cycle entry point. `cycle_context` carries run-local references; `scan_cycle` owns bounded observation/selection; `finish_cycle` owns Detail/Back outcome resolution and checkpoint commit. `home_observation`, `selection`, `detail_cycle`, `feed_scroll`, `outcomes`, `results` and `checkpoint` each own one bounded decision or side-effect boundary. `nurture.state` owns the mutable coverage, deduplication, phase and metric ledgers for a single cycle.

Mutable scan and workflow data is run-local. The viewport coordinator creates one `_ViewportScanState` per scan and passes it explicitly to candidate and row collaborators. Recovery does not retain candidate data between scans. `BodyTextReader` owns its run-local text range and visibility trackers, while a nurture cycle creates its own ledgers and commits through `CheckpointCommitter` only after the same verified boundary as before. Browser UI modules do not depend on reader, nurture, workflow, CLI or persistence; feed policy/value modules do not depend on browser UI or application orchestration. `tests/test_architecture.py` checks those import directions and review budgets for the issue-targeted coordinators.

```text
feed ──> errors / Playwright types
browser_ui ──> feed, errors, Playwright
reader ──> browser_ui, feed
nurture ──> reader, browser_ui, feed, storage, cycle state
workflow ──> nurture, storage, browser lifecycle
cli ──> application entry points
```

Existing `threads_browser.reader` APIs and the public `threads_browser.workflow.run_browser_nurture` entry point remain compatibility APIs. The page-cycle runner is private and can only run inside an owner-held profile-lock lifecycle. New browser interaction code belongs in `browser_ui`; pure feed policy and canonical data belong in `feed`. The reader remains the integration point for feed scans so changes to geometry, row recognition, body hydration and Detail can be tested independently.

## Evolution

The earlier `feed` extraction's no-behavior-change scope applied to that refactor, not to future tasks. Selectors, readiness, scrolling, Detail and CLI behavior can evolve when needed. Preserve provenance checks, profile isolation, bounded read-only operation and checkpoint privacy; verify affected behavior with focused regression tests. Offline Chromium fixtures do not establish live Threads UI compatibility: that requires owner-authorized private UAT.

## Current nurture cycle

`nurture.engine` counts verified rows once across overlapping viewports while retaining the current visible locators needed for safe selection. Its state machine tracks observation, scroll, content wait, verification, selection, Detail, Back and checkpoint phases. When a relevant Home card appears before any scroll and the feed has room to move, it requires one real bounded wheel advance before Detail. Detail still opens through the verified Home permalink; a visibly truncated body may expand if the canonical author and post remain the same and the visible text is a prefix of the expanded body.

`browser_ui.session` verifies the For You shell and account route, and `browser_ui.detail` returns with browser history and revalidates the selected post identity without a second Profile visit. Back does not depend on the post returning to its former screen coordinate. Wheel input, locators, route waits and native scroll events are the default. Shared geometry identifies an actual nested scroll surface, intersects document and nested scrollports for row eligibility, and confirms measured displacement. `feed_rows` discovers candidates, `body_text` tracks visible text fragments and hydration, and each observation is handed to `nurture.engine` immediately. Offscreen rows with verified identity are deferred through overlapping wheel steps; a partial or unknown row is skipped while later verified rows remain eligible. Before a Detail click, the reader revalidates the selected post's canonical identity, author and body, then uses Playwright mouse input at a visible, unobstructed permalink point so the click cannot auto-scroll an offscreen row into view.

`--detail` can report operator-only `discover_ms`, `read_row_ms`, `stabilize_ms` and a fixed `wait_reason` per viewport. These timings contain no body text, actor or URL. Verified observations and unverified display text remain distinct; printing an observation does not make it checkpoint eligible. Partial and unknown rows do not block a later verified selection. Detail verification uses the exact selected route and one unique matching primary body; a main landmark or repeated author/permalink is not required when the route supplies identity, while conflicting identity evidence rejects the Detail. If Detail remains unavailable after bounded waits, one Back attempt can preserve the verified Home observations and leave the selected post pending when Home, account and selected-post identity are reverified. Operator cancellation, navigation or blocker errors, and a lost return context prevent checkpoint completion. The checkpoint atomically records Home observations and successful Detail/Back counts; skipped rows remain eligible for a later cycle, and a run with skips reports `COMPLETE_WITH_SKIPS` while leaving viewport coverage incomplete.
