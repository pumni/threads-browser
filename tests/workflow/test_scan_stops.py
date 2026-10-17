from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Route

from tests.fixtures.browser import (
    _is_offline_profile_url,
    _offline_playwright_manager,
    _run_offline_page,
    _run_page_workflow_for_test,
    _synthetic_three_card_feed,
)
from threads_browser import workflow
from threads_browser.cli import main
from threads_browser.nurture import state as nurture_state
from threads_browser.nurture.state import WorkflowPhase
from threads_browser.reader import (
    HOME_URL,
    ThreadsHomeReader,
    UiReadError,
    canonical_terms,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


@pytest.mark.parametrize(
    "failure_mode",
    ["persistent", "canonical-change", "canonical-author-change", "root-replaced"],
)
def test_unrevalidated_scan_row_failure_stops_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_mode: str,
) -> None:
    actor = "offline.operator"
    html = _synthetic_three_card_feed(actor)
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")
    detail_route_requests = 0

    class FailThirdBodyReader(ThreadsHomeReader):
        last_instance = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failures = 0
            self.disposed_handle_batches = []
            type(self).last_instance = self

        async def _read_visible_body_progress(self, root, *args, **kwargs):
            if await root.get_attribute("id") != "scan-row-3":
                return await super()._read_visible_body_progress(root, *args, **kwargs)
            self.failures += 1
            if failure_mode == "canonical-change" and self.failures == 1:
                await root.locator('a[href*="/post/"]').first.evaluate(
                    "node => node.setAttribute('href', '/@candidate/post/rebound-row')"
                )
            elif failure_mode == "canonical-author-change" and self.failures == 1:
                await root.locator('a[href="/@candidate"]').evaluate(
                    "node => node.setAttribute('href', '/@other-candidate')"
                )
            elif failure_mode == "root-replaced" and self.failures == 1:
                await root.evaluate("node => node.replaceWith(node.cloneNode(true))")
            self._body_text.last_failure_substep = "BODY_RANGE_EVALUATE"
            raise PlaywrightError("synthetic protocol failure PRIVATE_MARKER")

        async def _dispose_active_scan_candidate_handles(self):
            handles = self._active_scan_candidate_handles
            if handles is not None:
                self.disposed_handle_batches.append(len(handles))
            await super()._dispose_active_scan_candidate_handles()

    monkeypatch.setattr(workflow, "ThreadsHomeReader", FailThirdBodyReader)

    async def exercise(page) -> None:
        async def track_detail(route: Route) -> None:
            nonlocal detail_route_requests
            detail_route_requests += 1
            await route.fulfill(status=500, content_type="text/plain", body="unavailable")

        await page.route("**/post/**", track_detail)
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=3,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "SCAN_ROW_UNVERIFIED"
        assert detail_route_requests == 0

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert "PRIVATE_MARKER" not in output
    assert "body_substep=BODY_RANGE_EVALUATE" in output
    assert (
        "retry_eligibility=REUSED" in output
        if failure_mode == "persistent"
        else "retry_eligibility=CHANGED" in output
    ), output
    assert all(f"SYNTHETIC_PRIVATE_ROW_{index}" not in output for index in range(1, 4))
    assert "offline.operator" not in output
    assert "scan-row-3" not in output
    assert "/@candidate/post/" not in output
    assert str(profile) not in output
    if failure_mode == "persistent":
        assert output.count("error_kind=OTHER") == 2
        assert output.count("row_identity_reused=true row_identity_changed=false") == 2
    else:
        assert output.count("error_kind=OTHER") == 1
        assert "row_identity_reused=false row_identity_changed=true" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.home_sampled == 0
    assert checkpoint.detail_reads == 0
    assert checkpoint.seen_fingerprints == ()
    assert FailThirdBodyReader.last_instance is not None
    assert FailThirdBodyReader.last_instance._active_scan_candidate_handles is None
    assert FailThirdBodyReader.last_instance.disposed_handle_batches
    assert FailThirdBodyReader.last_instance.disposed_handle_batches[0] == 3


def test_invalid_workflow_transition_is_not_mapped_to_browser_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
</body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")
    monkeypatch.setitem(
        nurture_state._ALLOWED_TRANSITIONS,
        WorkflowPhase.INITIALIZING,
        frozenset(),
    )

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
        assert caught.value.code == "INTERNAL_STATE_ERROR"

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=INITIALIZING operation=STATE_TRANSITION "
        "error_class=STATE_INVALID error_kind=OTHER page_closed=false "
        "context_closed=unknown row_identity_reused=unknown row_identity_changed=unknown"
    ) in output
    assert "BROWSER_SESSION_UNAVAILABLE" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_other_playwright_scan_error_is_not_mapped_to_browser_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
</body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    class OtherErrorReader(ThreadsHomeReader):
        async def scan_viewport(self, *args, **kwargs):
            raise PlaywrightError("synthetic protocol failure PRIVATE_MARKER")

    monkeypatch.setattr(workflow, "ThreadsHomeReader", OtherErrorReader)

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
        assert caught.value.code == "UI_READ_UNVERIFIED"

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=VERIFYING operation=SCAN_ROW error_class=OTHER "
        "error_kind=OTHER page_closed=false context_closed=unknown "
        "row_identity_reused=unknown row_identity_changed=unknown"
    ) in output
    assert "PRIVATE_MARKER" not in output
    assert output.count("[FAILURE_DIAGNOSTIC]") == 1
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_detail_does_not_auto_scroll_an_offscreen_target() -> None:
    actor = "offline.operator"
    body = "Synthetic fixture post moves outside the viewport."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="offscreen-detail">
<a href="/@candidate/post/offscreen-detail"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        target = (await reader.scan_viewport(1))[0]
        original_scroll_y = await page.evaluate("() => window.scrollY")
        await page.locator("#offscreen-detail").evaluate(
            "node => node.style.transform = 'translateY(1200px)'"
        )

        with pytest.raises(UiReadError) as caught:
            await reader.read_detail(target)

        assert caught.value.code == "DETAIL_NAVIGATION_UNVERIFIED"
        assert page.url == HOME_URL
        assert await page.evaluate("() => window.scrollY") == original_scroll_y

    _run_offline_page(feed_html, exercise)


def test_detail_body_mismatch_returns_home_without_claiming_read(tmp_path: Path) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/body-mismatch"
    home_body = "Hiring a frontend engineer"
    wrong_detail_body = "A different post body"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
<a href="/@candidate/post/body-mismatch"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{home_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_wrong_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/body-mismatch"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{wrong_detail_body}</p>
 </article></main></body></html>""",
            )

        await page.route(target_url, serve_wrong_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert len(checkpoint.pending_read_fingerprints) == 1


def test_overlapping_scan_completes_coverage_before_detail_and_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    pending_url = "https://www.threads.com/@candidate/post/pending"
    first_url = "https://www.threads.com/@candidate/post/first"
    relevant_body = "Hiring a frontend engineer in Vietnam"
    irrelevant_body = "A weather update"
    first_feed = (
        ("pending", relevant_body),
        ("old", irrelevant_body),
        ("middle", irrelevant_body),
        ("first", relevant_body),
    )
    resumed_feed = (
        ("old", irrelevant_body),
        ("middle", irrelevant_body),
        ("pending", relevant_body),
        ("first", relevant_body),
    )

    def render_feed(
        posts: tuple[tuple[str, str], ...],
        *,
        detail: bool = False,
    ) -> str:
        cards = "".join(
            f"""<article><a href="/@candidate/post/{slug}"><time>1h</time></a>
<a href="/@candidate">@candidate</a><p>{body}</p></article>"""
            for slug, body in posts
        )
        content = f"<main>{cards}</main>" if detail else cards
        return f"""<!doctype html><html><head>
<style>article {{ height: 360px; }}</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>{content}</body></html>"""

    detail_pages = {
        pending_url: render_feed((("pending", relevant_body),), detail=True),
        first_url: render_feed((("first", relevant_body),), detail=True),
    }
    account_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(account_root).add("offline", actor, profile)
    store = CheckpointStore(account_root)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(account_root))
    requests: list[str] = []
    home_requests = 0

    async def respond(route: Route) -> None:
        nonlocal home_requests
        url = route.request.url
        requests.append(url)
        if url == HOME_URL:
            home_requests += 1
            posts = first_feed if home_requests == 1 else resumed_feed
            html = render_feed(posts)
        elif url.rstrip("/") in detail_pages:
            html = detail_pages[url.rstrip("/")]
        elif _is_offline_profile_url(url):
            html = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=html)

    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond),
    )
    command = [
        "nurture",
        "run",
        "offline",
        "--headed",
        "--max-posts",
        "4",
        "--max-scrolls",
        "12",
        "--term",
        "hiring",
        "--term",
        "engineer",
    ]

    first_code = main(command)
    first = capsys.readouterr()
    policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
    checkpoint = store.load(account.account_id, policy)
    assert first_code == 0, first.out + first.err
    assert "NURTURE status=COMPLETE source=HOME_FOR_YOU" in first.out
    assert "home_sampled=3 home_new=3" in first.out
    assert "home_relevant=1 home_irrelevant=2" in first.out
    assert "detail_reads=1" in first.out
    assert "viewport_coverage_complete=true" in first.out
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.pending_read_fingerprints == ()
    assert len(checkpoint.seen_fingerprints) == 3
    assert requests.count(pending_url) + requests.count(first_url) == 1
