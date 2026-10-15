"""Bounded read-only Home / For You workflow."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .feed.models import DisplayObservation
from .feed.relevance import canonical_terms, classify_body
from .reader import ThreadsHomeReader, UiReadError, _FeedScanCounts
from .storage import (
    BrowserAccount,
    BrowserDataError,
    Checkpoint,
    CheckpointStore,
    ensure_safe_browser_profile,
)

_MAX_SEEN = 5_000


def _terminal_safe_post_text(body: str) -> str:
    safe: list[str] = []
    for character in body:
        codepoint = ord(character)
        if character == "\n":
            safe.append(character)
        elif character == "\t":
            safe.append("\\t")
        elif (
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        ):
            safe.append(
                f"\\u{codepoint:04X}"
                if codepoint <= 0xFFFF
                else f"\\U{codepoint:08X}"
            )
        else:
            safe.append(character)
    return "".join(safe)


def _print_post_text(viewport: int, decision: str, body: str | None) -> bool:
    if body is None or not body.strip():
        print(
            f"POST_TEXT viewport={viewport} status=UNKNOWN "
            "decision=UNKNOWN availability=UNKNOWN"
        )
        return False
    print(
        f"POST_TEXT viewport={viewport} status=EXTRACTED decision={decision} "
        f"availability=EXTRACTED chars={len(body)}"
    )
    print("<<< POST TEXT BEGIN >>>")
    for line in _terminal_safe_post_text(body).split("\n"):
        print(f"| {line}")
    print("<<< POST TEXT END >>>")
    return True


def _print_observed_text(
    viewport: int,
    decision: str,
    observation: DisplayObservation,
) -> bool:
    body = observation.body
    if body is None or not body.strip():
        print(
            f"DISPLAY_OBSERVATION viewport={viewport} "
            "status=OBSERVATION_UNCERTAIN reason=BODY_UNAVAILABLE"
        )
        return False
    print(
        f"DISPLAY_OBSERVATION viewport={viewport} "
        f"status=OBSERVED_UNVERIFIED decision={decision} chars={len(body)}"
    )
    print("<<< UNVERIFIED OBSERVED TEXT BEGIN >>>")
    for line in _terminal_safe_post_text(body).split("\n"):
        print(f"| {line}")
    print("<<< UNVERIFIED OBSERVED TEXT END >>>")
    return True


def _format_structure_diagnostics(counts: _FeedScanCounts) -> str:
    structural_reasons = ",".join(
        f"{reason}:{count}"
        for reason, count in sorted(counts.structural_reasons.items())
    ) or "none"
    return (
        f"article_roots={counts.article_roots} "
        f"pressable_roots={counts.pressable_roots} "
        f"time_nodes={counts.visible_times} "
        f"profile_anchor_candidates={counts.profile_anchor_candidates} "
        f"anchors={counts.visible_anchors} post_links={counts.visible_post_links} "
        f"body_candidates={counts.body_candidates} "
        f"paragraph_candidates={counts.paragraph_candidates} "
        f"region={counts.region_status} "
        f"scroll_surface={counts.scroll_surface_status} "
        f"structural_reasons={structural_reasons}"
    )


@dataclass(frozen=True, slots=True)
class NurtureResult:
    status: str
    home_sampled: int
    home_new: int
    home_relevant: int
    home_irrelevant: int
    home_unknown: int
    duplicates: int
    detail_reads: int
    scroll_attempts: int
    scrolls: int
    checkpoint_runs: int
    home_relevant_ratio: float | None
    skipped_unverified: int = 0
    ui_status: str = "UNVERIFIED_LIVE_UI"
    observed_rows: int = 0
    body_extracted: int = 0
    provenance_verified: int = 0
    classified: int = 0
    observation_unknown: int = 0
    unverified_relevant: int = 0
    skipped_rows: int = 0
    viewport_coverage_complete: bool = True


def policy_fingerprint(terms: tuple[str, ...]) -> str:
    payload = json.dumps(tuple(sorted(terms)), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


async def run_page_workflow(
    page: Page,
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int = 8,
    max_total_posts: int = 40,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    show_post_text: bool = False,
    show_observed_text: bool = False,
) -> NurtureResult:
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")
    if not 1 <= max_posts <= 20:
        raise UiReadError("POST_LIMIT_INVALID")
    if not 0 <= max_scrolls <= 20:
        raise UiReadError("SCROLL_LIMIT_INVALID")
    if not 1 <= max_total_posts <= 400:
        raise UiReadError("TOTAL_SCAN_LIMIT_INVALID")
    normalized_terms = canonical_terms(terms)
    fingerprint = policy_fingerprint(normalized_terms)
    checkpoint = checkpoint_store.load(account.account_id, fingerprint)
    reader = ThreadsHomeReader(
        page,
        account.expected_actor,
        readiness_prompt=readiness_prompt,
    )
    await reader.open_home()
    previously_seen = set(checkpoint.seen_fingerprints)
    seen_this_run: set[str] = set()
    new_fingerprints: set[str] = set()
    duplicates = 0
    relevant = 0
    irrelevant = 0
    unknown = 0
    selected = None
    pending = list(checkpoint.pending_read_fingerprints)
    pending_set = set(pending)
    found_relevant = False
    previewed_bodies = 0
    observed_rows = 0
    body_extracted = 0
    provenance_verified = 0
    classified = 0
    observation_unknown = 0
    unverified_relevant = 0
    skipped_rows = 0
    viewport_coverage_complete = True
    previous_unverified_rows: list[tuple[str, float]] = []
    viewport_number = 0
    exhaustion_reason = "MAX_SCROLLS"
    while True:
        remaining = max_total_posts - len(seen_this_run)
        if remaining <= 0:
            exhaustion_reason = "MAX_TOTAL_POSTS"
            break
        viewport_number += 1
        duplicate_count_before_scan = reader.feed_duplicates
        try:
            targets = await reader.scan_viewport(
                min(max_posts, remaining),
                scanned_fingerprints=seen_this_run,
            )
        except UiReadError:
            counts = reader.last_scan_counts
            rejection_reasons = ",".join(
                f"{reason}:{count}"
                for reason, count in sorted(counts.rejection_reasons.items())
            ) or "none"
            print(
                f"SCAN_STOP viewport={viewport_number} visible_links={counts.visible_links} "
                f"inspected={counts.inspected} url_parsed={counts.url_parsed} "
                f"author_unverified={counts.author_unverified} "
                f"wrapper_ambiguous={counts.wrapper_ambiguous} "
                f"card_not_ready={counts.card_not_ready} "
                f"verified={counts.verified} body_read={counts.body_read} "
                f"body_unknown={counts.body_unknown} classified=0 relevant=0 "
                f"skipped={counts.skipped} rejection_reasons={rejection_reasons} "
                f"{_format_structure_diagnostics(counts)}"
            )
            raise
        duplicates += reader.feed_duplicates - duplicate_count_before_scan
        viewport_relevant = 0
        viewport_irrelevant = 0
        viewport_unknown = 0
        viewport_classified = 0
        viewport_unverified_relevant = 0
        viewport_observation_unknown = 0
        viewport_selected = None
        viewport_relevant_targets = []
        scan_counts = reader.last_scan_counts
        for target in targets:
            if target.fingerprint in seen_this_run:
                continue
            seen_this_run.add(target.fingerprint)
            if target.body and target.body.strip():
                body_extracted += 1
            viewport_classified += 1
            is_new = target.fingerprint not in previously_seen
            if is_new:
                new_fingerprints.add(target.fingerprint)
            else:
                duplicates += 1
            decision = classify_body(target.body, normalized_terms)
            classified += 1
            if show_post_text and _print_post_text(
                viewport_number,
                decision,
                target.body,
            ):
                previewed_bodies += 1
            if decision == "RELEVANT":
                relevant += 1
                viewport_relevant += 1
                found_relevant = True
                if is_new and target.fingerprint not in pending_set:
                    pending.append(target.fingerprint)
                    pending_set.add(target.fingerprint)
                if target.fingerprint in pending_set:
                    viewport_relevant_targets.append(target)
            elif decision == "IRRELEVANT":
                irrelevant += 1
                viewport_irrelevant += 1
            else:
                unknown += 1
                viewport_unknown += 1
        current_unverified_rows: list[tuple[str, float]] = []
        matched_overlap_rows: set[int] = set()
        for observation in reader.last_display_observations:
            if observation.body is None or not observation.body.strip():
                observation_unknown += 1
                viewport_observation_unknown += 1
                if show_observed_text:
                    _print_observed_text(
                        viewport_number,
                        "UNKNOWN_UNVERIFIED",
                        observation,
                    )
                continue
            current_unverified_rows.append((observation.body, observation.viewport_y))
            overlap_duplicate = False
            if reader.last_scroll_delta > 0:
                expected_y = observation.viewport_y
                for previous_index, (previous_body, previous_y) in enumerate(
                    previous_unverified_rows
                ):
                    if previous_index in matched_overlap_rows or previous_body != observation.body:
                        continue
                    if abs((previous_y - reader.last_scroll_delta) - expected_y) <= 48:
                        matched_overlap_rows.add(previous_index)
                        overlap_duplicate = True
                        break
            if overlap_duplicate:
                continue
            body_extracted += 1
            display_decision = classify_body(observation.body, normalized_terms)
            classified += 1
            viewport_classified += 1
            if display_decision == "RELEVANT":
                display_decision = "RELEVANT_UNVERIFIED"
                unverified_relevant += 1
                viewport_unverified_relevant += 1
            elif display_decision == "IRRELEVANT":
                display_decision = "IRRELEVANT_UNVERIFIED"
            else:
                display_decision = "UNKNOWN_UNVERIFIED"
                observation_unknown += 1
                viewport_observation_unknown += 1
            if show_observed_text:
                _print_observed_text(viewport_number, display_decision, observation)
        # Only use this immediate-viewport geometry/body comparison to suppress
        # duplicate overlap work; unverified rows receive no stable identity.
        previous_unverified_rows = current_unverified_rows
        observed_rows += scan_counts.observed_rows
        provenance_verified += len(targets)
        skipped_rows += scan_counts.skipped_rows
        viewport_coverage_complete = (
            viewport_coverage_complete and scan_counts.coverage_complete
        )
        observation_status = (
            "RELEVANT_UNVERIFIED"
            if viewport_unverified_relevant
            else "OBSERVED_UNVERIFIED"
            if scan_counts.body_extracted > scan_counts.body_read
            else "OBSERVATION_UNCERTAIN"
            if scan_counts.uncertain_rows
            else "NONE"
        )
        print(
            f"SCANNING viewport={viewport_number} verified={scan_counts.verified} "
            f"relevant={viewport_relevant} skipped={scan_counts.skipped} "
            f"visible_links={scan_counts.visible_links} "
            f"inspected={scan_counts.inspected} "
            f"body_read={scan_counts.body_read} "
            f"body_unknown={scan_counts.body_unknown} "
            f"classified={viewport_classified} "
            f"observed_rows={scan_counts.observed_rows} "
            f"body_extracted={scan_counts.body_extracted} "
            f"provenance_verified={scan_counts.verified} "
            f"unverified_relevant={viewport_unverified_relevant} "
            f"observation_status={observation_status} "
            f"skipped_rows={scan_counts.skipped_rows} "
            f"viewport_coverage_complete={str(scan_counts.coverage_complete).lower()} "
            f"irrelevant={viewport_irrelevant} unknown={viewport_unknown} "
            f"observation_unknown={viewport_observation_unknown} "
            f"url_parsed={scan_counts.url_parsed} "
            f"author_unverified={scan_counts.author_unverified} "
            f"wrapper_ambiguous={scan_counts.wrapper_ambiguous} "
            f"card_not_ready={scan_counts.card_not_ready} "
            "rejection_reasons="
            + (
                ",".join(
                    f"{reason}:{count}"
                    for reason, count in sorted(scan_counts.rejection_reasons.items())
                )
                or "none"
            )
            + " "
            + _format_structure_diagnostics(scan_counts)
        )
        if not scan_counts.coverage_complete:
            print(
                f"VIEWPORT_COVERAGE_INCOMPLETE viewport={viewport_number} "
                f"observed_rows={scan_counts.observed_rows} cap={min(max_posts, remaining)}"
            )
            return NurtureResult(
                status="VIEWPORT_COVERAGE_INCOMPLETE",
                home_sampled=len(seen_this_run),
                home_new=len(new_fingerprints),
                home_relevant=relevant,
                home_irrelevant=irrelevant,
                home_unknown=unknown,
                duplicates=duplicates,
                detail_reads=0,
                scroll_attempts=reader.scroll_attempts,
                scrolls=reader.scrolls,
                checkpoint_runs=checkpoint.completed_runs,
                home_relevant_ratio=(
                    None if not seen_this_run else relevant / len(seen_this_run)
                ),
                skipped_unverified=reader.skipped_unverified,
                observed_rows=observed_rows,
                body_extracted=body_extracted,
                provenance_verified=provenance_verified,
                classified=classified,
                observation_unknown=observation_unknown,
                unverified_relevant=unverified_relevant,
                skipped_rows=skipped_rows,
                viewport_coverage_complete=False,
            )
        if viewport_relevant_targets:
            viewport_selected = viewport_relevant_targets[0]
        if viewport_selected is not None:
            selected = viewport_selected
            break
        if len(seen_this_run) >= max_total_posts:
            exhaustion_reason = "MAX_TOTAL_POSTS"
            break
        if reader.scroll_attempts >= max_scrolls:
            exhaustion_reason = "MAX_SCROLLS"
            break
        await reader.advance_feed()

    detail_reads = 0
    if selected is None and not found_relevant:
        print(
            f"SEARCH_BUDGET_EXHAUSTED reason={exhaustion_reason} "
            f"viewports={viewport_number}"
        )
    if selected is not None:
        await reader.read_detail(selected)
        print("DETAIL_VERIFIED status=PROVENANCE_VERIFIED")
        await reader.return_to_home()
        detail_reads = 1
        pending.remove(selected.fingerprint)
        pending_set.remove(selected.fingerprint)
        status = "COMPLETE"
    elif selected is None and unverified_relevant and not found_relevant:
        print("SEARCH_INCOMPLETE reason=UNVERIFIED_RELEVANCE")
        return NurtureResult(
            status="UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
            home_sampled=len(seen_this_run),
            home_new=len(new_fingerprints),
            home_relevant=relevant,
            home_irrelevant=irrelevant,
            home_unknown=unknown,
            duplicates=duplicates,
            detail_reads=0,
            scroll_attempts=reader.scroll_attempts,
            scrolls=reader.scrolls,
            checkpoint_runs=checkpoint.completed_runs,
            home_relevant_ratio=(
                None if not seen_this_run else relevant / len(seen_this_run)
            ),
            skipped_unverified=reader.skipped_unverified,
            observed_rows=observed_rows,
            body_extracted=body_extracted,
            provenance_verified=provenance_verified,
            classified=classified,
            observation_unknown=observation_unknown,
            unverified_relevant=unverified_relevant,
            skipped_rows=skipped_rows,
            viewport_coverage_complete=viewport_coverage_complete,
        )
    elif len(seen_this_run) == 0:
        if show_post_text and previewed_bodies == 0:
            print(
                "POST_TEXT status=NONE "
                f"verified_posts={len(seen_this_run)} extracted={previewed_bodies}"
            )
        return NurtureResult(
            status=(
                "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
                if unverified_relevant
                else "OBSERVED_UNVERIFIED_ONLY"
                if body_extracted
                else "OBSERVATION_UNCERTAIN_ONLY"
                if observed_rows
                else "NO_VERIFIED_POSTS_IN_BUDGET"
            ),
            home_sampled=0,
            home_new=0,
            home_relevant=0,
            home_irrelevant=0,
            home_unknown=0,
            duplicates=duplicates,
            detail_reads=0,
            scroll_attempts=reader.scroll_attempts,
            scrolls=reader.scrolls,
            checkpoint_runs=checkpoint.completed_runs,
            home_relevant_ratio=None,
            skipped_unverified=reader.skipped_unverified,
            observed_rows=observed_rows,
            body_extracted=body_extracted,
            provenance_verified=provenance_verified,
            classified=classified,
            observation_unknown=observation_unknown,
            unverified_relevant=unverified_relevant,
            skipped_rows=skipped_rows,
            viewport_coverage_complete=viewport_coverage_complete,
        )
    elif pending_set:
        status = "RELEVANT_POST_NOT_CURRENT"
    elif unverified_relevant and not seen_this_run:
        status = "OBSERVED_UNVERIFIED_ONLY"
    elif found_relevant or relevant:
        status = "NO_NEW_RELEVANT_POST"
    elif unknown:
        status = "VERIFIED_BUT_UNKNOWN_BODY"
    elif irrelevant:
        status = "VERIFIED_IRRELEVANT"
    else:
        status = "NO_RELEVANT_POST_IN_BUDGET"

    if len(pending) > _MAX_SEEN:
        raise BrowserDataError("STATE_INVALID")
    seen = list(dict.fromkeys((*checkpoint.seen_fingerprints, *seen_this_run)))
    if len(seen) > _MAX_SEEN:
        retained_pending = [value for value in seen if value in pending_set]
        recent = [value for value in seen if value not in pending_set]
        available = _MAX_SEEN - len(retained_pending)
        seen = retained_pending + (recent[-available:] if available else [])
    updated = Checkpoint(
        account.account_id,
        fingerprint,
        completed_runs=checkpoint.completed_runs + 1,
        home_sampled=checkpoint.home_sampled + len(seen_this_run),
        home_new=checkpoint.home_new + len(new_fingerprints),
        home_relevant=checkpoint.home_relevant + relevant,
        home_irrelevant=checkpoint.home_irrelevant + irrelevant,
        home_unknown=checkpoint.home_unknown + unknown,
        duplicates=checkpoint.duplicates + duplicates,
        detail_reads=checkpoint.detail_reads + detail_reads,
        scroll_attempts=checkpoint.scroll_attempts + reader.scroll_attempts,
        scrolls=checkpoint.scrolls + reader.scrolls,
        pending_read_fingerprints=tuple(pending),
        seen_fingerprints=tuple(seen),
    )
    checkpoint_store.save(updated)
    if show_post_text and previewed_bodies == 0:
        print(
            "POST_TEXT status=NONE "
            f"verified_posts={len(seen_this_run)} extracted={previewed_bodies}"
        )
    ratio = None if not seen_this_run else relevant / len(seen_this_run)
    return NurtureResult(
        status=status,
        home_sampled=len(seen_this_run),
        home_new=len(new_fingerprints),
        home_relevant=relevant,
        home_irrelevant=irrelevant,
        home_unknown=unknown,
        duplicates=duplicates,
        detail_reads=detail_reads,
        scroll_attempts=reader.scroll_attempts,
        scrolls=reader.scrolls,
        checkpoint_runs=updated.completed_runs,
        home_relevant_ratio=ratio,
        skipped_unverified=reader.skipped_unverified,
        observed_rows=observed_rows,
        body_extracted=body_extracted,
        provenance_verified=provenance_verified,
        classified=classified,
        observation_unknown=observation_unknown,
        unverified_relevant=unverified_relevant,
        skipped_rows=skipped_rows,
        viewport_coverage_complete=viewport_coverage_complete,
    )


async def run_browser_nurture(
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int = 8,
    max_total_posts: int = 40,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    show_post_text: bool = False,
    show_observed_text: bool = False,
) -> NurtureResult:
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")
    ensure_safe_browser_profile(account.profile_directory)
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(account.profile_directory),
                headless=False,
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                return await run_page_workflow(
                    page,
                    account,
                    checkpoint_store,
                    max_posts=max_posts,
                    terms=terms,
                    max_scrolls=max_scrolls,
                    max_total_posts=max_total_posts,
                    readiness_prompt=readiness_prompt,
                    show_post_text=show_post_text,
                    show_observed_text=show_observed_text,
                )
            finally:
                await context.close()
    except (UiReadError, BrowserDataError):
        raise
    except PlaywrightError:
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
    except (OSError, RuntimeError, ValueError):
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
