from __future__ import annotations

import pytest

from tests.fixtures.browser import (
    _run_offline_page,
)
from threads_browser.browser_ui import body_text as body_text_module
from threads_browser.nurture.observation import RowObservationFacts
from threads_browser.nurture.state import CoverageLedger
from threads_browser.reader import (
    ThreadsHomeReader,
)


def test_visible_body_segments_keep_utf16_surrogate_pairs_intact() -> None:
    actor = "offline.operator"
    emoji = "👩🏽‍💻"
    body = "A" * 4095 + emoji + "B" * 4090
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
nav {{ height: 32px; }}
article {{ position: relative; height: 1400px; margin: 0; }}
article p {{ position: absolute; top: 0; left: 0; width: 1280px; margin: 0;
font: 16px/18px monospace; overflow-wrap: anywhere; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/utf16"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        rows = []

        async def record(row) -> None:
            rows.append(row)

        await reader.scan_viewport(1, on_row=record)
        target = next(row.target for row in rows if row.target is not None)
        assert target.body is not None
        assert not target.body_complete
        assert emoji in target.body
        assert target.visible_segments
        assert all(
            not 0xD800 <= ord(character) <= 0xDFFF
            for segment in target.visible_segments
            for character in segment.text
        )

    _run_offline_page(feed_html, exercise)


def test_partially_clipped_range_stays_missing_after_forward_wheels() -> None:
    letters = "ABCDEFGHIJKLM"
    spans = "".join(f"<span>{letter}</span>" for letter in letters)
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
article {{ position: relative; height: 320px; margin: 0; }}
article p {{ position: absolute; top: 40px; left: 0; width: 200px;
margin: 0; font: 16px/18px monospace; }}
article p span {{ display: block; height: 18px; }}
article p span:first-child {{ position: relative; left: -2px; }}
#spacer {{ height: 2000px; }}
</style></head><body><main><article><p>{spans}</p></article>
<div id="spacer"></div></main></body></html>"""

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 720})
        reader = ThreadsHomeReader(page, "offline.operator")
        root = page.locator("article")
        root_handle = await root.element_handle()
        assert root_handle is not None
        try:
            first = await reader._read_visible_body_progress(
                root,
                root_handle,
                fingerprint="synthetic-clipped-range",
            )
            evidence = first.read_evidence
            assert not first.complete
            assert evidence.render_rect_count == 13
            assert evidence.visible_text_rect_count == 13
            assert evidence.fully_contained_text_rect_count == 12
            assert evidence.intersect_only_text_rect_count == 1
            assert evidence.missing_range_count == 1
            assert "missing_horizontal_left" in evidence.missing_range_topology
            assert evidence.unrecoverable_range_count == 1
            assert evidence.unrecoverable_range_signature

            for _ in range(3):
                previous_scroll = await page.evaluate("window.scrollY")
                await page.mouse.wheel(0, 288)
                await page.wait_for_function(
                    "previous => window.scrollY > previous",
                    arg=previous_scroll,
                )
                current = await reader._read_visible_body_progress(
                    root,
                    root_handle,
                    fingerprint="synthetic-clipped-range",
                )
                assert not current.complete
                assert current.read_evidence.missing_range_count == 1

            assert "missing_vertical_above" in current.read_evidence.missing_range_topology
        finally:
            await root_handle.dispose()

    _run_offline_page(feed_html, exercise)


@pytest.mark.parametrize(
    ("body", "body_top", "body_left", "font", "sequence", "scroll_to_complete"),
    [
        ("😀", 682, 0, "32px/48px monospace", "😀", True),
        ("😀", -20, 0, "32px/48px monospace", "😀", False),
        ("😀", 200, -10, "32px/48px monospace", "😀", False),
        (
            "line\n" * 12 + "👩🏽‍💻" + "\nend",
            320,
            0,
            "24px/32px monospace",
            "👩🏽‍💻",
            True,
        ),
    ],
    ids=("bottom-astral", "top-astral", "left-astral", "bottom-zwj"),
)
def test_partially_clipped_unicode_ranges_wait_until_fully_visible(
    body: str,
    body_top: int,
    body_left: int,
    font: str,
    sequence: str,
    scroll_to_complete: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
article {{ position: relative; height: 1600px; margin: 0; }}
article p {{ position: absolute; top: {body_top}px; left: {body_left}px; width: 1200px;
margin: 0; white-space: pre; font: {font}; }}
</style></head><body><main><article><p>{body}</p></article></main></body></html>"""

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 720})
        reader = ThreadsHomeReader(page, "offline.operator")
        root = page.locator("article")
        root_handle = await root.element_handle()
        assert root_handle is not None
        try:
            first = await reader._read_visible_body_progress(
                root,
                root_handle,
                fingerprint="synthetic-partial-unicode",
            )
            assert not first.complete
            assert first.read_evidence.missing_range_count
            assert first.read_evidence.visible_text_rect_count
            assert sequence not in (first.body or "")

            if not scroll_to_complete:
                return
            await page.mouse.wheel(0, 288)
            await page.wait_for_function("window.scrollY > 0")
            completed = await reader._read_visible_body_progress(
                root,
                root_handle,
                fingerprint="synthetic-partial-unicode",
            )
            assert completed.complete
            assert completed.body is not None
            assert sequence in completed.body
            assert completed.read_evidence.missing_range_count == 0
            assert completed.read_evidence.visited_range_count == (
                completed.read_evidence.expected_range_count
            )
            assert all(
                not 0xD800 <= ord(character) <= 0xDFFF
                for segment in completed.segments
                for character in segment.text
            )
        finally:
            await root_handle.dispose()

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "RangeError" not in output
    assert "error_kind=OTHER" not in output


def test_body_range_depth_limit_marks_read_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    body = "Synthetic body fragment. " * 100
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 32px; }}
article {{ margin: 0; }}
article p {{ width: 300px; margin: 0; font: 20px/24px monospace; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/range-depth"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    monkeypatch.setattr(body_text_module, "_MAX_BODY_RANGE_DEPTH", 1)

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 720})
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        rows = []

        async def record(row) -> None:
            if row.target is not None:
                rows.append(row.target)

        await reader.scan_viewport(1, on_row=record)
        assert rows
        target = rows[-1]
        assert not target.body_complete
        assert target.read_evidence is not None
        assert target.read_evidence.body_incomplete_reason == "LIMIT"
        assert not reader.last_scan_counts.coverage_complete

    _run_offline_page(feed_html, exercise)


def test_lookahead_rows_are_separate_from_visited_body_gaps() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 32px; }}
article {{ position: relative; box-sizing: border-box; height: 400px; margin: 0; }}
.identity {{ position: absolute; top: 8px; left: 8px; }}
.body {{ position: absolute; top: 700px; left: 8px; font: 20px/28px monospace; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<article><div class="identity"><a href="/@candidate/post/lookahead"><time>now</time></a><a href="/@candidate">candidate</a></div>
<div class="body"><p>A synthetic lookahead body.</p></div></article>
<article><div class="identity"><a href="/@candidate/post/lookahead"><time>now</time></a><a href="/@candidate">candidate</a></div>
<div class="body"><p>A synthetic lookahead body.</p></div></article>
</main></body></html>"""

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 720})
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        await reader.scan_viewport(4)

        assert not reader.last_scan_counts.coverage_complete
        assert reader.last_scan_counts.unresolved_visited_rows == 0, [
            (
                diagnostic.state,
                None
                if diagnostic.read_evidence is None
                else (
                    diagnostic.read_evidence.visible_text_rect_count,
                    diagnostic.read_evidence.closest_text_side,
                    diagnostic.read_evidence.closest_text_distance_px,
                ),
            )
            for diagnostic in reader.last_candidate_diagnostics
        ]
        assert reader.last_scan_counts.lookahead_rows == 1
        unresolved = [
            diagnostic
            for diagnostic in reader.last_candidate_diagnostics
            if diagnostic.state not in {"NON_POST", "VERIFIED"}
        ]
        assert len(unresolved) == 2
        assert all(diagnostic.read_evidence is not None for diagnostic in unresolved)
        assert all(
            diagnostic.read_evidence.visible_text_rect_count == 0
            and diagnostic.read_evidence.visited_range_count == 0
            and diagnostic.read_evidence.closest_text_side == "BELOW"
            for diagnostic in unresolved
        )

    _run_offline_page(feed_html, exercise)


def test_body_progress_completes_once_when_remaining_ranges_become_visible() -> None:
    actor = "offline.operator"
    body = "Synthetic hiring fixture fragment. " * 30
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
nav {{ height: 32px; }}
article {{ margin: 0; }}
article p {{ width: 400px; margin: 0; font: 16px/24px monospace; overflow-wrap: anywhere; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/viewport-upgrade"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 220})
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        ledger = CoverageLedger()
        outcomes = []
        first_evidence = []

        async def observe(row) -> None:
            target = row.target
            if target is None:
                return
            facts = RowObservationFacts(
                fingerprint=target.fingerprint,
                observation_id=row.diagnostic.observation_id,
                gap_id="synthetic-row",
                state=row.diagnostic.state,
                has_body=bool(target.body and target.body.strip()),
                body_complete=target.body_complete,
            )
            outcomes.append(ledger.observe_row(facts))
            first_evidence.append(target.read_evidence)

        await reader.scan_viewport(1, on_row=observe)
        assert ledger.read_in_progress
        assert ledger.unresolved_gap_count == 1
        assert first_evidence[-1] is not None
        assert first_evidence[-1].body_incomplete_reason == "UNVISITED_RANGE"
        assert first_evidence[-1].missing_range_count

        await page.set_viewport_size({"width": 1280, "height": 900})
        completed_targets = await reader.scan_viewport(1, on_row=observe)
        assert len(completed_targets) == 1
        assert completed_targets[0].body_complete
        assert completed_targets[0].read_evidence is not None
        assert completed_targets[0].read_evidence.body_incomplete_reason == "NONE"
        assert (
            completed_targets[0].read_evidence.render_rect_count
            == completed_targets[0].read_evidence.visible_text_rect_count
        )
        assert sum(outcome.body_completed for outcome in outcomes) == 1
        assert ledger.read_in_progress == set()
        assert ledger.fully_read == {completed_targets[0].fingerprint}
        assert ledger.unresolved_gap_count == 0

        await reader.scan_viewport(1, on_row=observe)
        assert sum(outcome.body_completed for outcome in outcomes) == 1
        assert ledger.unresolved_gap_count == 0

    _run_offline_page(feed_html, exercise)
