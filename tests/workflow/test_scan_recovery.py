from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
    _synthetic_three_card_feed,
)
from threads_browser import workflow
from threads_browser.reader import (
    ThreadsHomeReader,
    UiReadError,
    canonical_terms,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


@pytest.mark.parametrize("close_context", [False, True], ids=["page", "context"])
def test_closed_session_during_scan_is_classified_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    close_context: bool,
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/closed-scan"><time>now</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p></article></main>
</body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    class ClosedDuringScanReader(ThreadsHomeReader):
        async def scan_viewport(self, *args, **kwargs):
            if close_context:
                await self._page.context.close()
            else:
                await self._page.close()
            await self._page.title()

    monkeypatch.setattr(workflow, "ThreadsHomeReader", ClosedDuringScanReader)

    async def exercise(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "BROWSER_SESSION_UNAVAILABLE"

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=VERIFYING operation=SCAN_ROW error_class=TARGET_CLOSED "
        "error_kind=OTHER page_closed=true context_closed=unknown "
        "row_identity_reused=unknown row_identity_changed=unknown"
    ) in output
    assert output.count("[FAILURE_DIAGNOSTIC]") == 1
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_transient_execution_context_scan_error_is_retried_after_reverification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="candidate-row"><a href="/@candidate/post/detached-row"><time>now</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p></article></main>
</body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    class DetachOnceReader(ThreadsHomeReader):
        detached = False

        async def _read_visible_body_progress(self, root, *args, **kwargs):
            if not self.detached and await root.get_attribute("id") == "candidate-row":
                self.detached = True
                raise PlaywrightError("Execution context was destroyed PRIVATE_MARKER")
            return await super()._read_visible_body_progress(root, *args, **kwargs)

    monkeypatch.setattr(workflow, "ThreadsHomeReader", DetachOnceReader)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="""<!doctype html><html><body><main><article>
<a href="/@candidate/post/detached-row"><time>now</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p>
</article></main></body></html>""",
            )

        await page.route("**/post/detached-row", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 1
        assert result.detail_reads == 1

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=VERIFYING stage=FEED_INITIAL viewport=1 candidate=1 "
        "operation=READ_BODY error_class=DOM_DETACHED error_kind=EXECUTION_CONTEXT "
        "page_closed=false context_closed=unknown row_identity_reused=true "
        "row_identity_changed=false"
    ) in output
    assert "RECOVERED stage=FEED_INITIAL retry=1" in output
    assert "PRIVATE_MARKER" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1


@pytest.mark.parametrize(
    ("error_message", "expected_class", "expected_kind"),
    [
        ("synthetic protocol failure PRIVATE_MARKER", "OTHER", "OTHER"),
        (
            "Protocol error: evaluation failed PRIVATE_MARKER",
            "OTHER",
            "EVALUATION_PROTOCOL",
        ),
        (
            "RangeError: Maximum call stack size exceeded PRIVATE_MARKER",
            "OTHER",
            "JS_STACK_OVERFLOW",
        ),
        ("RangeError: invalid range PRIVATE_MARKER", "OTHER", "JS_RANGE_ERROR"),
        ("Evaluation failed: PRIVATE_MARKER", "OTHER", "EVALUATION"),
        ("strict mode violation: PRIVATE_MARKER", "OTHER", "STRICT_MODE"),
        (
            "Execution context was destroyed PRIVATE_MARKER",
            "DOM_DETACHED",
            "EXECUTION_CONTEXT",
        ),
    ],
    ids=(
        "other",
        "evaluation-protocol",
        "stack-overflow",
        "range-error",
        "evaluation",
        "strict-mode",
        "execution-context",
    ),
)
def test_transient_scan_row_failure_retries_without_replaying_verified_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_message: str,
    expected_class: str,
    expected_kind: str,
) -> None:
    actor = "offline.operator"
    html = _synthetic_three_card_feed(actor)
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")
    detail_requests = 0

    class FailThirdBodyOnceReader(ThreadsHomeReader):
        failures = 0

        async def _read_visible_body_progress(self, root, *args, **kwargs):
            if await root.get_attribute("id") == "scan-row-3" and not self.failures:
                self.failures += 1
                self._body_text.last_failure_substep = "BODY_RANGE_EVALUATE"
                raise PlaywrightError(error_message)
            return await super()._read_visible_body_progress(root, *args, **kwargs)

    monkeypatch.setattr(workflow, "ThreadsHomeReader", FailThirdBodyOnceReader)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            post_id = route.request.url.rsplit("/", maxsplit=1)[-1]
            row_number = post_id.rsplit("-", maxsplit=1)[-1]
            body = f"SYNTHETIC_PRIVATE_ROW_{row_number} weather update"
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    f'<a href="/@candidate/post/{post_id}"><time>now</time></a>'
                    f'<a href="/@candidate">candidate</a><p>{body}</p>'
                    "</article></main></body></html>"
                ),
            )

        await page.route("**/post/scan-row-*", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=0,
            terms=("weather",),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 3
        assert result.home_new == 3
        assert result.classified == 3
        assert result.observed_rows == 3
        assert result.provenance_verified == 3
        assert result.unique_verified_posts == 3
        assert result.visible_text_segments == 3
        assert result.duplicates == 0
        assert result.detail_reads == 1
        assert detail_requests == 1

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=VERIFYING stage=FEED_INITIAL viewport=1 candidate=3 "
        f"operation=READ_BODY error_class={expected_class} error_kind={expected_kind} "
        "page_closed=false context_closed=unknown row_identity_reused=true "
        "row_identity_changed=false"
    ) in output
    assert "RECOVERED stage=FEED_INITIAL retry=1" in output
    assert "body_substep=BODY_RANGE_EVALUATE" in output
    assert "identity_source=CURRENT_CANONICAL retry_eligibility=REUSED" in output
    assert "PRIVATE_MARKER" not in output
    assert all(f"SYNTHETIC_PRIVATE_ROW_{index}" not in output for index in range(1, 4))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("weather",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert len(checkpoint.seen_fingerprints) == 3
    assert checkpoint.detail_reads == 1


def test_read_body_retry_uses_carried_verified_row_after_native_scroll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_body = "Hiring a backend engineer in Vietnam"
    feed_html = f"""<!doctype html><html><head><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
article {{ position: relative; box-sizing: border-box; height: 420px; margin: 0; }}
.identity {{ position: absolute; top: 6px; left: 12px; }}
.body {{ position: absolute; top: 320px; left: 12px; right: 12px; }}
.secondary {{ position: absolute; top: 360px; left: 12px; }}
article:nth-of-type(3) .body {{ top: 70px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<article id="scroll-row-1">
<div class="identity"><a href="/@candidate/post/scroll-row-1"><time>3h</time></a>
<a href="/@candidate">candidate</a></div>
<div class="body"><p>A synthetic weather update</p></div>
<a class="secondary" href="/@candidate/post/related?source=synthetic">related</a>
</article>
<article id="scroll-row-2">
<div class="identity"><a href="/@candidate/post/scroll-row-2"><time>2h</time></a>
<a href="/@candidate">candidate</a></div>
<div class="body"><p>A synthetic local event</p></div>
</article>
<article id="scroll-row-3">
<div class="identity"><a href="/@candidate/post/scroll-row-3"><time>1h</time></a>
<a href="/@candidate">candidate</a></div>
<div class="body"><p>{detail_body}</p></div>
</article>
</main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    class FailCarriedBodyOnceReader(ThreadsHomeReader):
        last_instance = None
        active_reader = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failed = False
            self.inject_body_failure = False
            type(self).last_instance = self

        async def _read_visible_body_progress(self, root, *args, **kwargs):
            if (
                self._scan_stage == "FEED_SCROLL"
                and await root.get_attribute("id") == "scroll-row-1"
                and not self.failed
            ):
                self.failed = True
                self.inject_body_failure = True
                type(self).active_reader = self
                try:
                    return await super()._read_visible_body_progress(root, *args, **kwargs)
                finally:
                    self.inject_body_failure = False
            return await super()._read_visible_body_progress(root, *args, **kwargs)

    original_evaluate = Locator.evaluate

    async def fail_body_range_evaluate(locator, expression, arg=None):
        active_reader = FailCarriedBodyOnceReader.active_reader
        if (
            active_reader is not None
            and active_reader.inject_body_failure
            and "bodyTextNodesPresent" in expression
            and await locator.get_attribute("id") == "scroll-row-1"
        ):
            raise PlaywrightError("synthetic body range failure PRIVATE_MARKER")
        return await original_evaluate(locator, expression, arg)

    monkeypatch.setattr(Locator, "evaluate", fail_body_range_evaluate)
    monkeypatch.setattr(workflow, "ThreadsHomeReader", FailCarriedBodyOnceReader)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    '<a href="/@candidate/post/scroll-row-3"><time>1h</time></a>'
                    '<a href="/@candidate">candidate</a>'
                    f"<p>{detail_body}</p></article></main></body></html>"
                ),
            )

        await page.route("**/post/scroll-row-3", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=1,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 3
        assert result.classified == 3
        assert result.provenance_verified == 3
        assert result.unique_verified_posts == 3
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "operation=READ_BODY" in output
    assert "body_substep=BODY_RANGE_EVALUATE" in output
    assert "identity_source=CARRIED_VERIFIED retry_eligibility=REUSED" in output
    assert "PRIVATE_MARKER" not in output
    assert "POST_ROOT_UNRESOLVED:1" not in output
    reader = FailCarriedBodyOnceReader.last_instance
    assert reader is not None
    assert reader.last_scan_counts.coverage_complete
    assert reader.last_scan_counts.unrepresented_root_count == 0
    assert reader.last_scan_counts.unresolved_visited_rows == 0
    assert reader.last_scan_counts.structural_reasons.get("POST_ROOT_UNRESOLVED", 0) == 0
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.detail_reads == 1
