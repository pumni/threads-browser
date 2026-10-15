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

Existing `threads_browser.reader` imports remain compatibility aliases; new pure helpers belong in `feed`. The reader currently holds tightly coupled DOM state in one class. Refactor it when justified by a task, with regression coverage for behavior.

## Evolution

The earlier `feed` extraction's no-behavior-change scope applied to that refactor, not to future tasks. Selectors, readiness, scrolling, Detail and CLI behavior can evolve when needed. Preserve provenance checks, profile isolation, bounded read-only operation and checkpoint privacy; verify affected behavior with focused regression tests. Offline Chromium fixtures do not establish live Threads UI compatibility: that requires owner-authorized private UAT.
