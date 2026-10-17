from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
    _seed_pending_checkpoint,
)
from threads_browser import reader as reader_module
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


def test_ephemeral_gap_upgrade_keeps_below_fold_row_as_lookahead(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    eligible_body = "Hiring a synthetic engineer in Vietnam."
    unrelated_body = "Synthetic weather row remains unresolved."
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ height: 100%; margin: 0; overflow: hidden; }}
nav {{ height: 36px; }}
#scroll-port {{ height: 260px; overflow-y: auto; margin-top: 8px; }}
.card {{ position: relative; height: 260px; }}
.body {{ position: absolute; top: 180px; left: 12px; line-height: 24px; }}
#filler {{ height: 720px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="scroll-port">
<div id="upgrade" class="card" data-pressable-container="true">
<time>now</time><a href="/@candidate">candidate</a>
<p class="body">{eligible_body}</p></div>
<div id="unrelated" class="card" data-pressable-container="true">
<a href="/@candidate/post/unrelated/extra"><time>1h</time></a>
<a href="/@candidate">candidate</a><p class="body">{unrelated_body}</p></div>
<div id="filler"></div></main><script>
window.nativeWheelCount = 0;
window.addEventListener("wheel", () => {{
  window.nativeWheelCount += 1;
  if (window.nativeWheelCount !== 1) return;
  const timestamp = document.querySelector("#upgrade time");
  const permalink = document.createElement("a");
  permalink.href = "/@candidate/post/upgraded-row";
  timestamp.replaceWith(permalink);
  permalink.append(timestamp);
}});
</script></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=1,
            max_total_posts=5,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "RELEVANT_POST_NOT_CURRENT"
        assert result.scroll_attempts == result.scrolls == 1
        assert result.unresolved_gaps == 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 0
        assert result.checkpoint_saved is False
        # The second row is below-fold lookahead, not an encountered read gap.
        assert result.viewport_coverage_complete
        assert await page.evaluate("window.nativeWheelCount") >= 2

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    records = re.findall(
        r"\[ROW row_id=([0-9a-f]+).*?state=([A-Z_]+)",
        output,
    )
    upgraded_id = next(row_id for row_id, state in records if state == "PENDING_HYDRATION")
    assert (upgraded_id, "VERIFIED") in records
    assert any(
        row_id != upgraded_id and state == "DEFERRED_TEXT_BELOW" for row_id, state in records
    )
    assert "SEARCH_INCOMPLETE reason=RELEVANT_POST_NOT_CURRENT" in output
    assert "COVERAGE_STOP reason=SCROLL_BUDGET_EXHAUSTED_WITH_GAP" not in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=RELEVANT_POST_NOT_CURRENT" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.detail_reads == 0


@pytest.mark.parametrize("max_scrolls", (3, 8))
def test_continuous_below_viewport_lookahead_does_not_block_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    max_scrolls: int,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-boundary-pending"
    terms = ("hiring",)
    # At a 720px viewport, rows are 288px tall and the new last row's body
    # starts at the lower viewport boundary. Each 288px wheel brings the old
    # lookahead body fully into view while another row becomes lookahead.
    cards = "".join(
        f"""<article class="card"><a href="/@candidate/post/lookahead-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body">Synthetic weather update {index}.</p></article>"""
        for index in range(20)
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
main {{ display: block; }}
.card {{ position: relative; height: 288px; }}
.body {{ position: absolute; top: 108px; left: 12px; line-height: 24px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{cards}</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        terms,
        stale_url,
    )
    actionability_checks: list[str] = []

    async def reject_target(self, target):
        actionability_checks.append(target.fingerprint)
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

    monkeypatch.setattr(ThreadsHomeReader, "actionable_now", reject_target)
    run_results = []

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=max_scrolls,
            max_total_posts=40,
            terms=terms,
            detail=True,
        )
        assert result.status == "NO_ACTIONABLE_TARGET"
        assert result.scroll_attempts == result.scrolls == max_scrolls
        assert result.unresolved_gaps == 0
        assert result.gap_sources_total == 0
        assert result.gap_category == "NONE"
        assert result.viewport_coverage_complete
        assert result.detail_reads == 0
        assert page.url == HOME_URL
        run_results.append(result)

    _run_offline_page(feed_html, exercise, headless=False)
    output = capsys.readouterr().out
    scroll_deltas = [
        float(value)
        for value in re.findall(
            r"\[SCROLL\] attempt=\d+ status=ADVANCED observed=1 delta_px=([0-9.]+)",
            output,
        )
    ]
    assert len(scroll_deltas) == max_scrolls
    assert all(value == pytest.approx(288, abs=1) for value in scroll_deltas)
    assert actionability_checks
    viewport_lookahead = [
        int(value) for value in re.findall(r"\[VIEWPORT \d+\].*?lookahead_rows=(\d+)", output)
    ]
    assert len(viewport_lookahead) == max_scrolls + 1
    assert run_results[0].lookahead_rows == sum(viewport_lookahead)
    assert "state=DEFERRED_TEXT_BELOW" in output
    assert "unresolved_gaps=0" in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=NO_ACTIONABLE_TARGET" in output
    assert all(value not in output for value in (stale_url, actor, str(profile)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.schema_version == 3
    assert checkpoint.completed_runs == 1
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)


@pytest.mark.parametrize("max_scrolls", (3, 8))
def test_encountered_incomplete_body_keeps_fallback_blocked_after_scrolls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    max_scrolls: int,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-incomplete-pending"
    terms = ("hiring",)
    long_body = " ".join(["Synthetic weather remains unresolved."] * 120)
    cards = "".join(
        f"""<article class="card"><a href="/@candidate/post/verified-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body">Synthetic weather update {index}.</p></article>"""
        for index in range(12)
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
main {{ display: block; }}
.card {{ position: relative; height: 288px; }}
.body {{ position: absolute; top: 108px; left: 12px; line-height: 24px; }}
.incomplete {{ height: 288px; }}
.incomplete .body {{ top: 80px; width: 220px; height: 24px; overflow: hidden; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article class="card incomplete">
<a href="/@candidate/post/incomplete"><time>now</time></a>
<a href="/@candidate">candidate</a><p class="body">{long_body}</p></article>
{cards}</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        terms,
        stale_url,
    )
    actionability_checks: list[str] = []

    async def reject_target(self, target):
        actionability_checks.append(target.fingerprint)
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

    monkeypatch.setattr(ThreadsHomeReader, "actionable_now", reject_target)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=max_scrolls,
            max_total_posts=40,
            terms=terms,
            detail=True,
        )
        assert result.status == "RELEVANT_POST_NOT_CURRENT"
        assert result.scroll_attempts == result.scrolls == max_scrolls
        assert result.unresolved_gaps == 1
        assert result.read_in_progress == 1
        assert result.unresolved_observations == 0
        assert result.unrepresented_gaps == 0
        assert result.gap_sources_total == result.unresolved_gaps
        assert result.gap_category == "READ_IN_PROGRESS"
        assert not result.viewport_coverage_complete
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise, headless=False)
    output = capsys.readouterr().out
    scroll_deltas = [
        float(value)
        for value in re.findall(
            r"\[SCROLL\] attempt=\d+ status=ADVANCED observed=1 delta_px=([0-9.]+)",
            output,
        )
    ]
    assert len(scroll_deltas) == max_scrolls
    assert all(value == pytest.approx(288, abs=1) for value in scroll_deltas)
    assert not actionability_checks
    assert "state=PARTIAL_BODY" in output
    assert "reason=VISIBLE_TEXT_PROGRESS" in output
    assert "SEARCH_INCOMPLETE reason=RELEVANT_POST_NOT_CURRENT" in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=RELEVANT_POST_NOT_CURRENT" in output
    assert all(value not in output for value in (stale_url, actor, long_body, str(profile)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.schema_version == 3
    assert checkpoint.completed_runs == 1
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)


@pytest.mark.parametrize("max_scrolls", (3, 8))
@pytest.mark.parametrize("body_visible_at_budget_edge", (True, False))
def test_last_scroll_boundary_distinguishes_partial_body_from_below_viewport_lookahead(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    max_scrolls: int,
    body_visible_at_budget_edge: bool,
) -> None:
    actor = "offline.operator"
    terms = ("hiring",)
    boundary_index = max_scrolls + 2
    boundary_body_top = 90 if body_visible_at_budget_edge else 110
    boundary_body = "Synthetic weather details remain beyond the viewport. " * 3
    card_markup = []
    for index in range(boundary_index + 4):
        boundary_id = ' id="boundary-body"' if index == boundary_index else ""
        body = boundary_body if index == boundary_index else f"Synthetic weather update {index}."
        card_markup.append(
            f'''<article class="card"><a href="/@candidate/post/boundary-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body"{boundary_id}>{body}</p></article>'''
        )
    cards = "".join(card_markup)
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
main {{ display: block; }}
.card {{ position: relative; height: 288px; }}
.body {{ position: absolute; top: 108px; left: 12px; width: 320px;
  margin: 0; line-height: 24px; }}
#boundary-body {{ top: {boundary_body_top}px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{cards}</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    stale_url = "https://www.threads.com/@candidate/post/absent-budget-edge"
    _, _, _ = _seed_pending_checkpoint(store, account, terms, stale_url)
    actionability_checks: list[str] = []

    async def reject_target(self, target):
        actionability_checks.append(target.fingerprint)
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

    monkeypatch.setattr(ThreadsHomeReader, "actionable_now", reject_target)
    observed_results = []

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=max_scrolls,
            max_total_posts=40,
            terms=terms,
            detail=True,
        )
        geometry = await page.locator("#boundary-body").evaluate(
            """element => {
              const rect = element.getBoundingClientRect();
              return {
                top: rect.top,
                bottom: rect.bottom,
                viewport_height: window.innerHeight,
              };
            }"""
        )
        assert result.scroll_attempts == result.scrolls == max_scrolls
        assert result.detail_reads == 0
        assert result.checkpoint_saved is False
        assert page.url == HOME_URL
        observed_results.append((result, geometry))

    _run_offline_page(feed_html, exercise, headless=False)
    output = capsys.readouterr().out
    result, geometry = observed_results[0]
    scroll_deltas = [
        float(value)
        for value in re.findall(
            r"\[SCROLL\] attempt=\d+ status=ADVANCED observed=1 delta_px=([0-9.]+)",
            output,
        )
    ]
    assert len(scroll_deltas) == max_scrolls
    assert all(value == pytest.approx(288, abs=1) for value in scroll_deltas)

    if body_visible_at_budget_edge:
        assert geometry["viewport_height"] - 24 <= geometry["top"] < geometry["viewport_height"]
        assert geometry["bottom"] > geometry["viewport_height"]
        assert result.status == "RELEVANT_POST_NOT_CURRENT"
        assert result.unresolved_gaps == result.gap_sources_total == 1
        assert result.read_in_progress == 1
        assert result.unresolved_observations == 0
        assert result.unrepresented_gaps == 0
        assert result.gap_category == "READ_IN_PROGRESS"
        assert not result.viewport_coverage_complete
        assert not actionability_checks
    else:
        assert geometry["top"] >= geometry["viewport_height"]
        assert result.status == "NO_ACTIONABLE_TARGET"
        assert result.unresolved_gaps == result.gap_sources_total == 0
        assert result.read_in_progress == 0
        assert result.gap_category == "NONE"
        assert result.viewport_coverage_complete
        assert actionability_checks

    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert all(value not in output for value in (stale_url, actor, str(profile), boundary_body))


def test_carried_candidate_status_is_kept_per_row() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<div id="carried"><time>2h</time><span dir="auto">Observed synthetic body.</span></div>
<div id="verified"><a href="/@candidate/post/verified"><time>1h</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">Synthetic weather.</span></div>
</body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()

        carried_root = page.locator("#carried")
        carried_handle = await carried_root.element_handle()
        assert carried_handle is not None
        carried_identity = reader_module._TrackedRowIdentity(
            carried_handle,
            "synthetic-carried-observation",
        )
        await reader._remember_viewport_candidates(
            (carried_root,),
            (carried_identity,),
        )
        targets = await reader.scan_viewport(5)
        assert len(targets) == 1
        assert not reader.last_viewport_coverage_complete
        assert reader.last_candidate_diagnostics[0].state == "OBSERVED_UNVERIFIED"
        assert reader.last_candidate_diagnostics[1].state == "VERIFIED"
        assert any(
            observation.body == "Observed synthetic body."
            for observation in reader.last_display_observations
        )

    _run_offline_page(feed_html, exercise)


def test_relevant_visible_post_is_selected_when_initial_scroll_is_at_feed_end(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/at-end"
    body = "Hiring a synthetic fixture engineer at the feed end."
    feed_html = f"""<!doctype html><html><head><style>
main {{ height: 320px; overflow-y: auto; }}
.row {{ box-sizing: border-box; height: 500px; padding: 420px 10px 10px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="feed"><div class="row">
<a href="/@candidate/post/at-end"><time>now</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">{body}</span>
<button aria-label="More">More</button>
</div></main><script>
const feed = document.querySelector('#feed');
feed.scrollTop = feed.scrollHeight;
</script></body></html>"""
    detail_body = f"{body} with a verified synthetic detail expansion."
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/at-end"><time>now</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">{detail_body}</span>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=1,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE"
        assert result.home_relevant == 1
        assert result.detail_reads == 1
        assert result.scroll_attempts == 0

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1
