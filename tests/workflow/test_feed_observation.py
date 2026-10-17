from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
from threads_browser.reader import (
    ThreadsHomeReader,
    canonical_terms,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


def test_modern_landmark_free_div_feed_verifies_cards_and_detail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/job-seeking"
    first_body = "A short weather update."
    verified_body_first = "Tôi đang tìm việc"
    verified_body_second = "trong lĩnh vực kỹ thuật"
    excluded_text = (
        "đang tìm việc in a quoted post",
        "đang tìm việc in the sidebar",
        "đang tìm việc in the composer",
        "đang tìm việc in an unrelated article",
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: sans-serif; }}
[data-pressable-container="true"] {{ display: block; margin: 16px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<aside><article><a href="/@sidebar/post/decoy"><time>1h</time></a>
  <a href="/@sidebar">@sidebar</a><span dir="auto">{excluded_text[1]}</span>
</article></aside>
<form><div contenteditable="true" role="textbox">{excluded_text[2]}</div>
  <a href="/@composer/post/decoy">Compose</a></form>
<aside><article><p>{excluded_text[3]}</p></article></aside>
<div id="first-card" data-pressable-container="true">
  <a href="/@candidate/post/weather"><time>3h</time></a>
  <a href="/@candidate">@candidate</a>
  <div><span dir="auto">{first_body}</span></div>
  <div data-pressable-container="true"><blockquote>
    <a href="/@quoted/post/quote"><time>4h</time></a>
    <a href="/@quoted">@quoted</a>
    <span dir="auto">{excluded_text[0]}</span>
  </blockquote></div>
  <button type="button">Like</button>
</div>
<div id="second-card" data-pressable-container="true">
  <a href="/@candidate/post/job-seeking"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <span dir="auto">{verified_body_first}</span>
  <div dir="auto">{verified_body_second}</div>
  <button type="button">Reply</button>
</div>
</body></html>"""
    detail_html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body><main>
<div data-pressable-container="true">
  <a href="/@candidate/post/job-seeking"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <span dir="auto">{verified_body_first}</span>
  <div dir="auto">{verified_body_second}</div>
</div>
</main></body></html>"""
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

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            max_total_posts=10,
            terms=("đang tìm việc",),
            show_post_text=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 2
        assert result.home_irrelevant == result.home_relevant == 1
        assert result.detail_reads == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "article_roots=0 pressable_roots=2" in output
    assert f"| {first_body}" in output
    assert f"| {verified_body_first}" in output
    assert f"| {verified_body_second}" in output
    assert all(text not in output for text in excluded_text)
    preview_blocks = re.findall(
        r"<<< POST TEXT BEGIN >>>\n(.*?)\n<<< POST TEXT END >>>",
        output,
        re.DOTALL,
    )
    assert preview_blocks == [
        f"| {first_body}",
        f"| {verified_body_first}\n| {verified_body_second}",
    ]
    assert all(label not in "\n".join(preview_blocks) for label in ("Like", "Reply"))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("đang tìm việc",))),
    )
    assert checkpoint.home_sampled == 2
    assert checkpoint.detail_reads == 1
    persisted = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    ).read_text(encoding="utf-8")
    assert all(body not in persisted for body in (first_body, *excluded_text))
    assert verified_body_first not in persisted
    assert verified_body_second not in persisted


def test_visible_card_without_permalink_is_skipped_without_fingerprint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    private_text = "Hiring text must stay unread without a post permalink"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<aside><div data-pressable-container="true">
  <a href="/@sidebar/post/decoy"><time>2h</time></a>
  <a href="/@sidebar">@sidebar</a><span dir="auto">Hiring sidebar decoy</span>
</div></aside>
<form><div contenteditable="true" role="textbox">Hiring composer decoy</div>
  <a href="/@composer/post/decoy">Compose</a></form>
<article><p>Hiring unrelated article without feed evidence</p></article>
<div data-pressable-container="true">
  <time>now</time><a href="/@candidate">@candidate</a>
  <span dir="auto">{private_text}</span>
</div>
<script>window.addEventListener("wheel", () => {{
  sessionStorage.setItem("wheel-count", "1");
}});</script>
</body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 20)
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
            max_scrolls=0,
            max_total_posts=10,
            terms=("hiring",),
            show_post_text=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 1
        assert result.body_extracted == 1
        assert result.provenance_verified == 0
        assert result.unverified_relevant == 1
        assert result.detail_reads == result.scroll_attempts == 0
        assert result.checkpoint_runs == 1
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") is None

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN verified=0" in output
    assert "pressable_roots=1" in output
    assert "observation_status=RELEVANT_UNVERIFIED" in output
    assert private_text not in output
    assert "Hiring sidebar decoy" not in output
    assert "Hiring composer decoy" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0
    assert checkpoint.seen_fingerprints == ()


def test_delayed_permalink_hydration_recovers_before_unsupported_stop() -> None:
    actor = "offline.operator"
    body = "A safely identified hydrated post"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<div data-pressable-container="true">
  <time>now</time><a href="/@candidate">@candidate</a>
  <span dir="auto">{body}</span>
</div></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        await page.evaluate(
            """() => setTimeout(() => {
                const card = document.querySelector(
                    '[data-pressable-container="true"]'
                );
                const time = card.querySelector('time');
                const permalink = document.createElement('a');
                permalink.href = '/@candidate/post/hydrated';
                time.replaceWith(permalink);
                permalink.append(time);
            }, 80)"""
        )
        targets = await reader.scan_viewport(1)
        assert len(targets) == 1
        assert targets[0].url == "https://www.threads.com/@candidate/post/hydrated"
        assert targets[0].author == "candidate"
        assert targets[0].body == body

    _run_offline_page(feed_html, exercise)


def test_body_fallback_span_rows_ignore_pressable_non_post_and_nurture_verified_post(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/relevant"
    bodies = (
        "Synthetic fixture weather report.",
        "Synthetic fixture hiring a backend engineer.",
        "Synthetic fixture music note.",
    )
    rows = "".join(
        (
            '<div class="card" data-pressable-container="true">\n'
            f'<a href="/@candidate/post/{name}"><time>{index}h</time></a>\n'
            '<a href="/@candidate">@candidate</a>'
            f'<span dir="auto">{body}</span>\n'
            + ('<button aria-label="More">More</button>\n' if name == "relevant" else "")
            + "</div>"
        )
        for index, (name, body) in enumerate(
            (("weather", bodies[0]), ("relevant", bodies[1]), ("music", bodies[2])),
            start=1,
        )
    )
    feed_html = f"""<!doctype html><html><head><style>
nav {{ height: 40px; }}
.card {{ box-sizing: border-box; height: 220px; padding: 12px; }}
#non-post {{ position: fixed; top: 2px; right: 2px; width: 90px; height: 40px; }}
.spacer {{ height: 500px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>{rows}
<div id="non-post" data-pressable-container="true"><button>Open filters</button></div>
<div class="spacer"></div></body></html>"""
    detail_body = f"{bodies[1]} with its expanded synthetic fixture detail."
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/relevant"><time>2h</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">{detail_body}</span>
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

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=1,
            max_total_posts=10,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 3
        assert result.provenance_verified >= result.home_sampled
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert "region=BODY_FALLBACK" in output
    assert "root=PRESSABLE has_visible_time=false" in output
    assert "state=NON_POST reason=NO_POST_ROW_EVIDENCE" in output
    assert "[SCROLL] attempt=1 status=ADVANCED" in output
    assert "[SELECT] post=" in output
    assert f"| {bodies[1]}" in output
    assert f"| {detail_body}" in output
    assert "Open filters" not in output
    assert "@candidate" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.detail_reads == 1


def test_progressive_body_fallback_emits_row_before_reading_next_with_y_jitter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    first_url = "https://www.threads.com/@candidate/post/progressive-first"
    first_body = "Synthetic hiring a backend engineer."
    second_body = "Synthetic weather update."
    feed_html = f"""<!doctype html><html><head><style>
@keyframes jitter {{ from {{ transform: translateY(0); }} to {{ transform: translateY(2px); }} }}
.card {{ height: 190px; animation: jitter 20ms infinite alternate; }}
.settled .card {{ animation-play-state: paused; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><div class="card" id="first">
<a href="/@candidate/post/progressive-first"><time>now</time></a>
<a href="/@candidate">@candidate</a><div><span dir="auto">{first_body}</span></div>
</div><div class="card" id="second">
<a href="/@candidate/post/progressive-second"><time>1h</time></a>
<a href="/@candidate">@candidate</a><div><span dir="auto">{second_body}</span></div>
</div></main></body></html>"""
    feed_html = feed_html.replace(
        "</body>",
        "<script>setTimeout(() => document.documentElement.classList.add('settled'), 2200);</script></body>",
    )
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/progressive-first"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{first_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    first_read_seen_before_second: list[str] = []
    original_read_card = ThreadsHomeReader._read_card

    async def record_before_second_row(reader, root, **kwargs):
        if await root.get_attribute("id") == "second" and not first_read_seen_before_second:
            first_read_seen_before_second.append(capsys.readouterr().out)
        return await original_read_card(reader, root, **kwargs)

    monkeypatch.setattr(ThreadsHomeReader, "_read_card", record_before_second_row)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(first_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=4,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 2
        assert result.detail_reads == 1

    _run_offline_page(feed_html, exercise)
    output = "".join(first_read_seen_before_second) + capsys.readouterr().out
    assert first_read_seen_before_second
    assert f"| {first_body}" in first_read_seen_before_second[0]
    assert second_body not in first_read_seen_before_second[0]
    assert output.count(f"| {first_body}") == 2  # Home observation and Detail.
    assert output.count(f"| {second_body}") == 1
    assert "stabilize_ms=" in output
    assert "/post/" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 2


def test_three_verified_rows_stream_around_rejected_candidate_and_checkpoint_selection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/second"
    verified = (
        ("first", "Synthetic weather note one."),
        ("second", "Synthetic hiring a backend engineer."),
        ("third", "Synthetic weather note three."),
    )
    cards = [
        f"""<div class="card"><a href="/@candidate/post/{slug}"><time>1h</time></a>
<a href="/@candidate">candidate</a><span dir="auto">{body}</span></div>"""
        for slug, body in verified[:1]
    ]
    cards.append(
        """<div class="card"><a href="/@candidate/post/rejected?share=1"><time>now</time></a>
<a href="/@candidate">candidate</a><span dir="auto">Synthetic rejected container body.</span></div>"""
    )
    cards.extend(
        f"""<div class="card"><a href="/@candidate/post/{slug}"><time>1h</time></a>
<a href="/@candidate">candidate</a><span dir="auto">{body}</span></div>"""
        for slug, body in verified[1:]
    )
    feed_html = f"""<!doctype html><html><head><style>
nav {{ height: 36px; }} .card {{ height: 155px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{"".join(cards)}</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        await page.route(
            detail_url,
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    '<a href="/@candidate/post/second"><time>1h</time></a>'
                    '<a href="/@candidate">candidate</a>'
                    f"<p>{verified[1][1]}</p>"
                    "</article></main></body></html>"
                ),
            ),
        )
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == 3
        assert result.provenance_verified == 3
        assert result.detail_reads == 1
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    for _slug, body in verified:
        assert f"| {body}" in output
    assert "| Synthetic rejected container body." in output
    assert "state=OBSERVED_UNVERIFIED" in output
    assert "checkpoint=PENDING" in output
    assert "/post/" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert len(checkpoint.seen_fingerprints) == 3
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()
