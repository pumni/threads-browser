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

## Scope of this refactor

Only extract pure value objects, URLs and text semantics. Do not modify selectors, Playwright event ordering, browser profiles, actor/checkpoint invariants, automation cadence, or CLI behavior. Fix the existing workflow correctness gaps separately with focused regressions, then conduct owner-authorized private UAT before treating live Threads compatibility as proven.
