from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
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


def test_unverified_display_row_does_not_block_verified_selected_detail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@verified/post/selected"
    unverified_body = "Tôi đang tìm việc kỹ sư"
    quoted_body = "Hiring quoted context must not affect the parent row"
    verified_body = "A weather update"
    selected_body = "Hiring an engineer"
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
.row {{ box-sizing: border-box; min-height: 190px; padding: 12px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<div class="row" id="display-only">
  <div><a href="/@seeker">@seeker</a><time>3h</time></div>
  <span>{unverified_body}</span>
  <blockquote><a href="/@quoted/post/quote"><time>4h</time></a>
    <a href="/@quoted">@quoted</a><span>{quoted_body}</span>
  </blockquote>
</div>
<div class="row" id="verified-quoted">
  <div><a href="/@verified/post/weather"><time>2h</time></a>
    <a href="/@verified">@verified</a></div>
  <span>{verified_body}</span>
  <blockquote><a href="/@quoted/post/quote-two"><time>5h</time></a>
    <a href="/@quoted">@quoted</a><span>{quoted_body}</span>
  </blockquote>
</div>
<div class="row" id="selected">
  <div><a href="/@verified/post/selected"><time>1h</time></a>
    <a href="/@verified">@verified</a></div>
  <span>{selected_body}</span>
</div>
</main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@verified/post/selected"><time>1h</time></a>
<a href="/@verified">@verified</a><span>{selected_body}</span>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=0,
            max_total_posts=20,
            terms=("đang tìm việc", "hiring"),
            detail=True,
            show_post_text=True,
            show_observed_text=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 3
        assert result.body_extracted == 3
        assert result.provenance_verified == 2
        assert result.unverified_relevant == 1
        assert result.detail_reads == 1
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN verified=2" in output
    assert "VIEWPORT_COVERAGE_INCOMPLETE" in output
    assert "[DETAIL] post=2 status=VERIFIED" in output
    assert "[BACK] post=2 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert output.count(f"| {unverified_body}") == 1
    assert output.count(f"| {verified_body}") == 1
    assert output.count(f"| {selected_body}") == 2
    assert quoted_body not in output
    assert "status=OBSERVED_UNVERIFIED decision=RELEVANT_UNVERIFIED" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("đang tìm việc", "hiring"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 2
    assert checkpoint.detail_reads == 1
    checkpoint_path = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    )
    assert checkpoint_path.exists()


def test_two_row_cap_completes_with_skips_without_wheel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    rows = "".join(
        f"""<div class="row"><div><a href="/@author{index}">@author{index}</a>
          <time>{index}h</time></div><span>Visible row {index}</span></div>"""
        for index in range(4)
    )
    feed_html = f"""<!doctype html><html><head><style>
.row {{ box-sizing: border-box; min-height: 150px; padding: 10px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>{rows}</main>
<script>window.wheelCount = 0;
window.addEventListener("wheel", () => window.wheelCount++);</script>
</body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=2,
            max_total_posts=40,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 4
        assert not result.viewport_coverage_complete
        assert result.detail_reads == result.scroll_attempts == 0
        assert result.checkpoint_runs == 1
        assert await page.evaluate("window.wheelCount") == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN" in output
    assert "READY stage=FEED_INITIAL" not in output
    assert "VIEWPORT_COVERAGE_INCOMPLETE viewport=1 observed_rows=4 cap=2" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0


def test_unmarked_row_body_hydration_is_rescanned_without_early_ready(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body = "A hydrated display observation"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div class="row"><a href="/@candidate">@candidate</a>
  <time>now</time><span id="body"></span></div></main>
<script>setTimeout(() => document.querySelector("#body").textContent = "{body}", 120);</script>
</body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS", (
            f"status={result.status} unresolved_gaps={result.unresolved_gaps} "
            f"scroll_attempts={result.scroll_attempts} scrolls={result.scrolls}"
        )
        assert result.body_extracted == 1
        assert result.provenance_verified == 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN verified=0" in output
    assert "READY stage=FEED_INITIAL" not in output
    assert body not in output
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    assert store.load(account.account_id, policy).completed_runs == 1


def test_final_feed_gate_failure_cannot_print_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        reader.last_scan_counts.verified = 1
        reader.last_scan_counts.body_read = 1
        reader.last_scan_counts.body_extracted = 1
        reader.last_scan_counts.observed_rows = 1

        async def observe():
            return reader_module._RenderObservation(
                ("stable-verified-feed",),
                SimpleNamespace(observed_rows=1),
                ready_state="BODY_EXTRACTED",
            )

        async def fail_final_validation() -> None:
            raise UiReadError("FEED_INITIAL_NOT_READY")

        with pytest.raises(UiReadError) as caught:
            await reader._await_render_settled(
                "FEED_INITIAL",
                reader._readiness_deadline(1_000),
                timeout_code="FEED_INITIAL_NOT_READY",
                observe=observe,
                validate=fail_final_validation,
            )
        assert caught.value.code == "FEED_INITIAL_NOT_READY"

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "READY stage=FEED_INITIAL" not in output


def test_unverified_keyword_mismatch_never_becomes_a_verified_match(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body = "I am looking for work as an engineer"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div><a href="/@seeker">@seeker</a><time>now</time>
  <span>{body}</span></div></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.unverified_relevant == 0
        assert result.home_sampled == 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert body not in output
    assert "unverified_relevant=0" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1


def test_unmarked_rows_use_one_overlapping_wheel_and_checkpoint_skips(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    bodies = (
        "Private synthetic weather observation one",
        "Private synthetic music observation two",
        "Private synthetic local observation three",
    )
    quoted = "Private quoted context must stay outside the parent row"
    rows = "".join(
        f"""<div class="row"><a href="/@candidate{index}">@candidate{index}</a>
<time>{index}h</time><span>{body}</span>
{f"<blockquote><span>{quoted}</span></blockquote>" if index == 1 else ""}
</div>"""
        for index, body in enumerate(bodies, start=1)
    )
    feed_html = f"""<!doctype html><html><head><style>
.row {{ box-sizing: border-box; height: 420px; padding: 24px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>{rows}</main>
<script>
window.wheelCount = 0;
window.rowClickCount = 0;
window.addEventListener("wheel", event => window.wheelCount++);
window.addEventListener("click", event => {{
  if (event.target.closest(".row")) window.rowClickCount++;
}});
</script></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=1,
            max_total_posts=20,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 5
        assert result.body_extracted == result.classified == 3
        assert result.provenance_verified == result.detail_reads == 0
        assert result.scroll_attempts == result.scrolls == 1
        assert result.checkpoint_runs == 1
        assert await page.evaluate("window.wheelCount") > result.scroll_attempts
        assert await page.evaluate("window.rowClickCount") == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "SCROLL attempt=1 observed=1 delta_band=SMALL" in output
    assert "SEARCH_BUDGET_EXHAUSTED reason=MAX_SCROLLS viewports=2" in output
    assert all(value not in output for value in (*bodies, quoted))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0


def test_explicit_observed_text_preview_contains_only_scoped_unverified_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body = "Synthetic display text about hiring"
    excluded = (
        "Synthetic quote must be excluded",
        "Synthetic composer must be excluded",
        "Synthetic action must be excluded",
    )
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<form><div contenteditable="true" role="textbox">{excluded[1]}</div></form>
<main><div><a href="/@candidate">@candidate</a><time>now</time>
  <span>{body}</span><blockquote><span>{excluded[0]}</span></blockquote>
  <button>{excluded[2]}</button></div></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
            show_observed_text=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.unverified_relevant == 1
        assert result.provenance_verified == result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "status=OBSERVED_UNVERIFIED decision=RELEVANT_UNVERIFIED" in output
    assert "<<< UNVERIFIED OBSERVED TEXT BEGIN >>>" in output
    assert f"| {body}" in output
    assert all(value not in output for value in excluded)
    assert "POST TEXT BEGIN" not in output


def test_unverified_relevance_is_skipped_while_verified_rows_checkpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    verified_body = "A synthetic weather update"
    observed_body = "I am looking for work as an engineer"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<div><a href="/@verified/post/weather"><time>2h</time></a>
  <a href="/@verified">@verified</a><span>{verified_body}</span></div>
<div><a href="/@seeker">@seeker</a><time>1h</time>
  <span>{observed_body}</span></div>
</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("looking for work",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == result.home_irrelevant == 1
        assert result.unverified_relevant == 1
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "SEARCH_INCOMPLETE reason=UNVERIFIED_RELEVANCE" not in output
    assert verified_body not in output
    assert observed_body not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("looking for work",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1
    assert checkpoint.home_irrelevant == 1
    assert len(checkpoint.seen_fingerprints) == 1
