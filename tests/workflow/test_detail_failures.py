from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
from threads_browser import workflow
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


def test_failed_selected_detail_saves_home_and_keeps_retry_pending(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/pending-detail"
    relevant_body = "Hiring a frontend engineer"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/pending-detail"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>{relevant_body}</p></div>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  setTimeout(() => window.scrollBy(0, event.deltaY), 40);
}}, {{ once: true, passive: false }});
</script></body></html>"""

    async def timeout_exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><div>"
                    '<a href="/@candidate/post/pending-detail"><time>1h</time></a>'
                    '<a href="/@candidate">@candidate</a>'
                    "</div></main></body></html>"
                ),
            )

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            terms=("hiring", "engineer"),
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 5_000)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)
    with tempfile.TemporaryDirectory(prefix="threads-browser-readiness-") as synthetic_root:
        synthetic_path = Path(synthetic_root)
        data_root = synthetic_path / "data"
        profile = synthetic_path / "profile"
        profile.mkdir()
        account = AccountStore(data_root).add("offline", actor, profile)
        store = CheckpointStore(data_root)
        _run_offline_page(feed_html, timeout_exercise)
        policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert store.load(account.account_id, policy).detail_reads == 0
        assert len(checkpoint.pending_read_fingerprints) == 1

    malformed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/one"><time>2h</time></a>
  <a href="/@quoted/post/two"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><a href="/@quoted">@quoted</a>
<p>Two identities in one card.</p>
</article></main><script>window.wheelCount = 0;
window.addEventListener("wheel", () => window.wheelCount++);</script></body></html>"""

    provenance_prompts: list[tuple[str, int]] = []

    async def provenance_prompt(stage: str, retry: int) -> bool:
        provenance_prompts.append((stage, retry))
        return False

    async def malformed_exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            terms=("hiring",),
            readiness_prompt=provenance_prompt,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 1
        assert result.detail_reads == result.scroll_attempts == 0
        assert result.checkpoint_runs == 1
        assert result.checkpoint_saved is True
        assert await page.evaluate("window.wheelCount") == 0
        assert provenance_prompts == []

    with monkeypatch.context() as short_recovery:
        short_recovery.setattr(reader_module, "_READINESS_TIMEOUT_MS", 15_000)
        short_recovery.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 20)
        short_recovery.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 100)
        short_recovery.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 40)
        _run_offline_page(malformed_html, malformed_exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0
    assert checkpoint.scroll_attempts == 0
    assert checkpoint.scrolls == 0
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN" in output
    assert "VIEWPORT_COVERAGE_INCOMPLETE viewport=1 observed_rows=1 cap=2" in output
    assert "Two identities in one card" not in output

    no_movement_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 1000px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/second"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>Another update</p></div>
</main></body></html>"""

    async def no_movement_exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()

        async def wheel_without_movement(_delta_x: float, _delta_y: float) -> None:
            return None

        monkeypatch.setattr(page.mouse, "wheel", wheel_without_movement)
        monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 5_000)
        monkeypatch.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 100)
        with pytest.raises(UiReadError) as caught:
            await reader.sample_and_scroll(2)
        assert caught.value.code == "HOME_SCROLL_UNVERIFIED"
        assert reader.scroll_attempts == 1
        assert reader.scrolls == 0

    _run_offline_page(no_movement_html, no_movement_exercise)


def test_detached_scroll_anchor_without_feed_displacement_stops_without_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div class="post-card" id="old-card">
  <a href="/@candidate/post/stable"><time>2h</time></a>
  <a href="/@candidate">@candidate</a><p>A weather update</p>
</div></main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const oldCard = document.querySelector("#old-card");
  oldCard.replaceWith(oldCard.cloneNode(true));
}}, {{ once: true, passive: false }});
</script></body></html>"""
    readers: list[ThreadsHomeReader] = []

    class TrackingReader(ThreadsHomeReader):
        def __init__(
            self,
            page,
            expected_actor: str,
            *,
            readiness_prompt=None,
            detail_mode: bool = False,
        ) -> None:
            super().__init__(
                page,
                expected_actor,
                readiness_prompt=readiness_prompt,
                detail_mode=detail_mode,
            )
            readers.append(self)

    monkeypatch.setattr(workflow, "ThreadsHomeReader", TrackingReader)
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 15_000)
    with tempfile.TemporaryDirectory(prefix="threads-browser-detached-scroll-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add("offline", actor, profile)
        store = CheckpointStore(synthetic_root / "data")

        async def exercise(page) -> None:
            with pytest.raises(UiReadError) as caught:
                await _run_page_workflow_for_test(
                    page,
                    account,
                    store,
                    max_posts=2,
                    terms=("hiring",),
                )
            assert caught.value.code == "HOME_SCROLL_UNVERIFIED"

        _run_offline_page(feed_html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring",)))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 0
        assert checkpoint.scrolls == 0
        assert readers[0].scroll_attempts == 1
        assert readers[0].scrolls == 0
