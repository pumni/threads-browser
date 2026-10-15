# Source architecture

This is a small modular monolith, not a service/microservice system.

```text
src/threads_browser/
  __init__.py, __main__.py   Public Python API and CLI module entry
  cli.py                     Argument parsing, terminal UX, result formatting
  account_service.py         Account creation and profile-lock orchestration
  login.py                   Human-operated persistent Chromium login
  reader.py                  Stateful read-only Playwright UI adapter
  workflow.py                One-shot nurture orchestration and metrics
  storage.py                 Local accounts, terms, checkpoint and OS locking
  errors.py                  Shared UI failure type
  feed/
    models.py                Feed value objects
    relevance.py             Pure text normalization and classification
    urls.py                  Strict canonical post/profile URL validation
```

## Dependencies and boundaries

- `feed` contains small, testable concepts. It may depend on `errors` and Playwright's `Locator` **type**, but not browser navigation or persistence.
- `reader` owns locators, clicks, scrolls and UI verification. It imports feed value objects/helpers and does not access checkpoint files.
- `workflow` coordinates the reader and checkpoint store, with one bounded foreground run and a single optional Detail. It must not bypass reader provenance checks.
- `storage` owns persisted account references, terms, locks and versioned fingerprints; never store raw post bodies, cookies or raw URLs.
- `login` and `account_service` own interactive session establishment; `cli` is only an adapter around these application entry points.

Existing `threads_browser.reader` imports are kept as aliases for callers/tests while new feature code can import pure helpers from `threads_browser.feed`. The reader stays one class for now: splitting its tightly coupled DOM state across mixins before private Threads UAT would increase risk and obscure failures.

## Evolution

The earlier `feed` extraction's no-behavior-change scope applied to that refactor, not to future tasks. Selectors, readiness, scrolling, Detail and CLI behavior can evolve when needed. Preserve provenance checks, profile isolation, bounded read-only operation and checkpoint privacy; verify affected behavior with focused regression tests. Offline Chromium fixtures do not establish live Threads UI compatibility: that requires owner-authorized private UAT.

## Current nurture cycle

`workflow` counts verified rows once across overlapping viewports while retaining the current visible locators needed for safe selection. When a relevant Home card appears before any scroll and the feed has room to move, it requires one real bounded wheel advance before Detail. Detail still opens through the verified Home permalink; a visibly truncated body may expand if the canonical author and post remain the same and the visible text is a prefix of the expanded body.

The reader returns with browser history, checks the For You shell and a lightweight post anchor, and avoids a second Profile visit. Wheel input, locators, route waits and native scroll events are the default. DOM geometry is limited to identifying an actual nested scroll surface and confirming its measured displacement. Checkpoints are written only for a safely completed scan or cycle; incomplete selection, uncertain coverage, failed Detail, or lost return context does not increment completed runs.
