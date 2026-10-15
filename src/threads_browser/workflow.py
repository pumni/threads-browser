"""Bounded read-only Home / For You workflow."""

from __future__ import annotations

import json
import sys
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .feed.models import DisplayObservation, PostTarget
from .feed.relevance import canonical_terms, classify_body
from .reader import (
    ThreadsHomeReader,
    UiReadError,
    _FeedScanCounts,
    _HomeRowObservation,
)
from .storage import (
    BrowserAccount,
    BrowserDataError,
    Checkpoint,
    CheckpointStore,
    ensure_safe_browser_profile,
)

_MAX_SEEN = 5_000


def _detail_terminal_ready() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError, ValueError):
        return False


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


def _print_detail_body(
    stage: str,
    reference: str,
    status: str,
    decision: str,
    extent: str,
    body: str | None,
) -> bool:
    if body is None or not body.strip():
        print(
            f"[POST {reference}] status={status} decision={decision} "
            f"source={stage}_BODY extent={extent} body=UNAVAILABLE"
        )
        return False
    print(
        f"[POST {reference}] status={status} decision={decision} "
        f"source={stage}_BODY extent={extent} chars={len(body)}",
        flush=True,
    )
    print(
        f"<<< {stage}_BODY BEGIN status={status} decision={decision} "
        f"extent={extent} >>>",
        flush=True,
    )
    for line in _terminal_safe_post_text(body).split("\n"):
        print(f"| {line}", flush=True)
    print(f"<<< {stage}_BODY END >>>", flush=True)
    return True


def _detail_stop(status: str) -> None:
    print(f"[CHECKPOINT] status=NOT_SAVED reason={status}")
    print(f"[STOP] code={status}")


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
        f"deferred_offscreen={counts.deferred_offscreen} "
        f"region={counts.region_status} "
        f"scroll_surface={counts.scroll_surface_status} "
        f"structural_reasons={structural_reasons}"
    )


def _print_candidate_diagnostics(reader: ThreadsHomeReader) -> None:
    for row in reader.last_candidate_diagnostics:
        print(
            f"[ROW row={row.row_index}] root={row.root_kind} "
            f"has_visible_time={str(row.has_visible_time).lower()} "
            f"has_post_permalink={str(row.has_post_permalink).lower()} "
            f"has_profile_author={str(row.has_profile_author).lower()} "
            f"has_readable_body={str(row.has_readable_body).lower()} "
            f"state={row.state} reason={row.reason}",
            flush=True,
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
    detail: bool = False,
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
        detail_mode=detail,
    )
    try:
        await reader.open_home()
    except UiReadError as error:
        if detail:
            print(f"[HOME] status=FAILED code={error.code}")
            _detail_stop(error.code)
        raise
    if detail:
        print("[HOME] status=READY account=VERIFIED source=FOR_YOU")
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
    decisions_by_fingerprint: dict[str, str] = {}
    previous_unverified_rows: list[tuple[str, float]] = []
    viewport_number = 0
    post_numbers: dict[str, int] = {}
    next_post_number = 0
    exhaustion_reason = "MAX_SCROLLS"
    streamed_home_fingerprints: set[str] = set()
    unresolved_deferred_fingerprints: set[str] = set()
    unresolved_deferred_without_identity = False

    async def advance_feed(*, scan_after_scroll: bool = False) -> bool:
        attempt = reader.scroll_attempts + 1
        if detail:
            print(f"[SCROLL] attempt={attempt} status=REQUESTED")
        try:
            advanced = await reader.advance_feed(scan_after_scroll=scan_after_scroll)
        except UiReadError as error:
            if detail:
                print(f"[SCROLL] attempt={attempt} status=FAILED code={error.code}")
                _detail_stop(error.code)
            raise
        if detail:
            if advanced:
                print(
                    f"[SCROLL] attempt={reader.scroll_attempts} status=ADVANCED "
                    f"observed=1 delta_px={reader.last_scroll_delta:g}"
                )
            else:
                print(
                    f"[SCROLL] attempt={attempt} status=STOPPED "
                    f"reason={reader.last_scroll_stop_reason or 'NOT_ADVANCED'}"
                )
        return advanced

    def select_verified_fallback(targets: list[PostTarget]) -> None:
        nonlocal selected
        if found_relevant or unverified_relevant or pending_set or not targets:
            return
        selected = min(
            targets,
            key=lambda target: target.fingerprint in previously_seen,
        )
        if selected.fingerprint not in pending_set:
            pending.append(selected.fingerprint)
            pending_set.add(selected.fingerprint)

    while True:
        remaining = max_total_posts - len(seen_this_run)
        if remaining <= 0:
            exhaustion_reason = "MAX_TOTAL_POSTS"
            break
        viewport_number += 1
        duplicate_count_before_scan = reader.feed_duplicates
        viewport_relevant = 0
        viewport_irrelevant = 0
        viewport_unknown = 0
        viewport_classified = 0
        viewport_unverified_relevant = 0
        viewport_observation_unknown = 0
        viewport_selected = None
        viewport_relevant_targets = []
        viewport_fallback_targets = []
        current_unverified_rows: list[tuple[str, float]] = []
        matched_overlap_rows: set[int] = set()
        async def observe_row(
            row: _HomeRowObservation,
            *,
            viewport_number: int = viewport_number,
            viewport_relevant_targets: list[PostTarget] = viewport_relevant_targets,
            viewport_fallback_targets: list[PostTarget] = viewport_fallback_targets,
            current_unverified_rows: list[tuple[str, float]] = current_unverified_rows,
            previous_unverified_rows: list[tuple[str, float]] = previous_unverified_rows,
            matched_overlap_rows: set[int] = matched_overlap_rows,
        ) -> None:
            nonlocal body_extracted, classified, duplicates, found_relevant
            nonlocal irrelevant, next_post_number, observation_unknown, previewed_bodies
            nonlocal relevant, unknown
            nonlocal unverified_relevant, viewport_classified, viewport_irrelevant
            nonlocal viewport_observation_unknown, viewport_relevant
            nonlocal viewport_unverified_relevant, viewport_unknown
            nonlocal unresolved_deferred_without_identity

            diagnostic = row.diagnostic
            if diagnostic.state == "DEFERRED_OFFSCREEN":
                if row.fingerprint in seen_this_run:
                    pass
                elif row.fingerprint is not None:
                    unresolved_deferred_fingerprints.add(row.fingerprint)
                else:
                    unresolved_deferred_without_identity = True
            if detail:
                print(
                    f"[ROW row={diagnostic.row_index}] root={diagnostic.root_kind} "
                    f"has_visible_time={str(diagnostic.has_visible_time).lower()} "
                    f"has_post_permalink={str(diagnostic.has_post_permalink).lower()} "
                    f"has_profile_author={str(diagnostic.has_profile_author).lower()} "
                    f"has_readable_body={str(diagnostic.has_readable_body).lower()} "
                    f"state={diagnostic.state} reason={diagnostic.reason} "
                    f"observation={'EMITTED' if row.target or (row.display and row.display.body) else 'RECORDED'} "
                    "checkpoint=PENDING",
                    flush=True,
                )
            target = row.target
            if target is not None:
                if diagnostic.state != "VERIFIED" or not target.body:
                    return
                unresolved_deferred_fingerprints.discard(target.fingerprint)
                if target.fingerprint in seen_this_run:
                    duplicates += 1
                    if detail and target.fingerprint not in streamed_home_fingerprints:
                        print(
                            f"[POST post={post_numbers[target.fingerprint]}] "
                            "status=VERIFIED decision=OVERLAP_DUPLICATE HOME_BODY=OMITTED",
                            flush=True,
                        )
                    if (
                        decisions_by_fingerprint.get(target.fingerprint) == "RELEVANT"
                        and target.fingerprint in pending_set
                    ):
                        viewport_relevant_targets.append(target)
                    elif (
                        target.body.strip()
                        and decisions_by_fingerprint.get(target.fingerprint) != "RELEVANT"
                    ):
                        viewport_fallback_targets.append(target)
                    return
                seen_this_run.add(target.fingerprint)
                next_post_number += 1
                post_numbers[target.fingerprint] = next_post_number
                body_extracted += int(bool(target.body and target.body.strip()))
                viewport_classified += 1
                is_new = target.fingerprint not in previously_seen
                if is_new:
                    new_fingerprints.add(target.fingerprint)
                else:
                    duplicates += 1
                decision = classify_body(target.body, normalized_terms)
                decisions_by_fingerprint[target.fingerprint] = decision
                classified += 1
                if detail:
                    if target.fingerprint not in streamed_home_fingerprints:
                        if _print_detail_body(
                            "HOME",
                            f"post={post_numbers[target.fingerprint]}",
                            "VERIFIED",
                            decision,
                            "TRUNCATED" if target.body_truncated else "COMPLETE",
                            target.body,
                        ):
                            previewed_bodies += 1
                        streamed_home_fingerprints.add(target.fingerprint)
                elif show_post_text and _print_post_text(
                    viewport_number, decision, target.body
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
                    viewport_fallback_targets.append(target)
                else:
                    unknown += 1
                    viewport_unknown += 1
                    viewport_fallback_targets.append(target)
                return

            observation = row.display
            if observation is None:
                return
            if observation.body is None or not observation.body.strip():
                observation_unknown += 1
                viewport_observation_unknown += 1
                if detail:
                    print(
                        f"[POST viewport={viewport_number} row={diagnostic.row_index}] "
                        "status=OBSERVATION_UNCERTAIN decision=UNKNOWN body=UNAVAILABLE",
                        flush=True,
                    )
                elif show_observed_text:
                    _print_observed_text(viewport_number, "UNKNOWN_UNVERIFIED", observation)
                return
            current_unverified_rows.append((observation.body, observation.viewport_y))
            overlap_duplicate = False
            if reader.last_scroll_delta > 0:
                for previous_index, (previous_body, previous_y) in enumerate(
                    previous_unverified_rows
                ):
                    if matched_overlap_rows and previous_index in matched_overlap_rows:
                        continue
                    if previous_body != observation.body:
                        continue
                    if abs((previous_y - reader.last_scroll_delta) - observation.viewport_y) <= 48:
                        matched_overlap_rows.add(previous_index)
                        overlap_duplicate = True
                        break
            if overlap_duplicate:
                if detail:
                    print(
                        f"[POST viewport={viewport_number} row={diagnostic.row_index}] "
                        "status=OBSERVED_UNVERIFIED decision=OVERLAP_DUPLICATE "
                        "HOME_BODY=OMITTED",
                        flush=True,
                    )
                return
            body_extracted += 1
            decision = classify_body(observation.body, normalized_terms)
            classified += 1
            viewport_classified += 1
            if decision == "RELEVANT":
                decision = "RELEVANT_UNVERIFIED"
                unverified_relevant += 1
                viewport_unverified_relevant += 1
            elif decision == "IRRELEVANT":
                decision = "IRRELEVANT_UNVERIFIED"
            else:
                decision = "UNKNOWN_UNVERIFIED"
                observation_unknown += 1
                viewport_observation_unknown += 1
            if detail:
                if _print_detail_body(
                    "HOME",
                    f"viewport={viewport_number} row={diagnostic.row_index}",
                    "OBSERVED_UNVERIFIED",
                    decision,
                    "OBSERVED",
                    observation.body,
                ):
                    previewed_bodies += 1
            elif show_observed_text:
                _print_observed_text(viewport_number, decision, observation)

        try:
            await reader.scan_viewport(
                min(max_posts, remaining),
                on_row=observe_row,
            )
        except UiReadError as error:
            counts = reader.last_scan_counts
            duplicates += reader.feed_duplicates - duplicate_count_before_scan
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
                f"body_unknown={counts.body_unknown} classified={viewport_classified} "
                f"relevant={viewport_relevant} skipped={counts.skipped} "
                f"rejection_reasons={rejection_reasons} "
                f"{_format_structure_diagnostics(counts)}"
            )
            if detail:
                print(
                    f"[TIMING] viewport={viewport_number} "
                    f"discover_ms={reader.last_scan_timings.discover_ms} "
                    f"read_row_ms={reader.last_scan_timings.read_row_ms} "
                    f"stabilize_ms={reader.last_scan_timings.stabilize_ms} "
                    f"wait_reason={reader.last_scan_timings.wait_reason}",
                    flush=True,
                )
                print(
                    f"[VIEWPORT {viewport_number}] status=PARTIAL code={error.code}",
                    flush=True,
                )
            if error.code.endswith("NOT_READY") and (
                seen_this_run or body_extracted or observed_rows
            ):
                counts.coverage_complete = False
                observed_rows += counts.observed_rows
                provenance_verified += counts.verified
                skipped_rows += counts.skipped_rows
                if detail:
                    _detail_stop("VIEWPORT_OBSERVATION_INCOMPLETE")
                return NurtureResult(
                    status="VIEWPORT_OBSERVATION_INCOMPLETE",
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
                    home_relevant_ratio=None if not seen_this_run else relevant / len(seen_this_run),
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
            if detail:
                _detail_stop(error.code)
            raise
        duplicates += reader.feed_duplicates - duplicate_count_before_scan
        scan_counts = reader.last_scan_counts
        if detail:
            print(
                f"[VIEWPORT {viewport_number}] observed={scan_counts.observed_rows} "
                f"verified={scan_counts.verified} body_read={scan_counts.body_read} "
                f"body_unknown={scan_counts.body_unknown} skipped={scan_counts.skipped} "
                "coverage="
                f"{str(scan_counts.coverage_complete).lower()}",
                flush=True,
            )
            print(
                f"[TIMING] viewport={viewport_number} "
                f"discover_ms={reader.last_scan_timings.discover_ms} "
                f"read_row_ms={reader.last_scan_timings.read_row_ms} "
                f"stabilize_ms={reader.last_scan_timings.stabilize_ms} "
                f"wait_reason={reader.last_scan_timings.wait_reason}",
                flush=True,
            )
        # Only use this immediate-viewport geometry/body comparison to suppress
        # duplicate overlap work; unverified rows receive no stable identity.
        previous_unverified_rows = current_unverified_rows
        observed_rows += scan_counts.observed_rows
        provenance_verified += scan_counts.verified
        skipped_rows += scan_counts.skipped_rows
        if not scan_counts.coverage_complete and not reader.last_viewport_deferred_only:
            viewport_coverage_complete = False
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
        if not scan_counts.coverage_complete and not reader.last_viewport_deferred_only:
            print(
                f"VIEWPORT_COVERAGE_INCOMPLETE viewport={viewport_number} "
                f"observed_rows={scan_counts.observed_rows} cap={min(max_posts, remaining)}"
            )
            if detail:
                print(
                    f"[VIEWPORT] verified_body_extracted={scan_counts.body_read} "
                    f"observed_body_extracted={scan_counts.body_extracted - scan_counts.body_read} "
                    "scan=NOT_COMMITTED"
                )
                _detail_stop("VIEWPORT_COVERAGE_INCOMPLETE")
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
        if unresolved_deferred_fingerprints or unresolved_deferred_without_identity:
            if reader.scroll_attempts < max_scrolls and await advance_feed():
                continue
            if detail:
                _detail_stop("VIEWPORT_COVERAGE_INCOMPLETE")
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
            viewport_relevant_targets.sort(
                key=lambda target: target.fingerprint in previously_seen
            )
            if max_scrolls > 0 and reader.scroll_attempts == 0:
                if await advance_feed(scan_after_scroll=True):
                    if len(seen_this_run) >= max_total_posts:
                        if (
                            reader.last_viewport_coverage_complete
                            and reader.last_viewport_observed_rows <= max_posts
                        ):
                            still_visible = [
                                target
                                for target in reader.last_scroll_targets
                                if target.fingerprint in pending_set
                                and decisions_by_fingerprint.get(target.fingerprint)
                                == "RELEVANT"
                                and classify_body(target.body, normalized_terms)
                                == "RELEVANT"
                            ]
                            still_visible.sort(
                                key=lambda target: target.fingerprint in previously_seen
                            )
                            if still_visible:
                                selected = still_visible[0]
                                break
                        exhaustion_reason = "MAX_TOTAL_POSTS"
                        break
                    continue
                if reader.last_scroll_stop_reason == "FEED_END_REACHED":
                    exhaustion_reason = "FEED_END_REACHED"
                    # The current viewport was fully verified and remains in
                    # view; use its eligible post when there is no next page.
            viewport_selected = viewport_relevant_targets[0]
        if viewport_selected is not None:
            selected = viewport_selected
            break
        if len(seen_this_run) >= max_total_posts:
            exhaustion_reason = "MAX_TOTAL_POSTS"
            if max_scrolls == 0 or reader.scroll_attempts > 0:
                select_verified_fallback(viewport_fallback_targets)
            break
        if reader.scroll_attempts >= max_scrolls:
            exhaustion_reason = "MAX_SCROLLS"
            select_verified_fallback(viewport_fallback_targets)
            break
        if not await advance_feed():
            exhaustion_reason = reader.last_scroll_stop_reason or "FEED_NOT_ADVANCED"
            if exhaustion_reason in {"FEED_END_REACHED", "FEED_NOT_SCROLLABLE"}:
                select_verified_fallback(viewport_fallback_targets)
            break

    detail_reads = 0
    if selected is None and not found_relevant:
        if exhaustion_reason in {"FEED_END_REACHED", "FEED_NOT_SCROLLABLE"}:
            print(
                f"SEARCH_STOP reason={exhaustion_reason} "
                f"viewports={viewport_number}"
            )
        else:
            print(
                f"SEARCH_BUDGET_EXHAUSTED reason={exhaustion_reason} "
                f"viewports={viewport_number}"
            )
    if selected is not None:
        selected_number = post_numbers[selected.fingerprint]
        selected_decision = decisions_by_fingerprint.get(
            selected.fingerprint,
            "UNKNOWN",
        )
        if detail:
            if selected_decision == "RELEVANT":
                print(
                    f"[SELECT] post={selected_number} decision=RELEVANT "
                    f"reason={'RELEVANT_PENDING' if selected.fingerprint in previously_seen else 'RELEVANT_NEW'}"
                )
            else:
                print(
                    f"[SELECT] post={selected_number} "
                    "decision=VERIFIED_FALLBACK reason=NO_RELEVANT_MATCH"
                )
        try:
            detail_target = await reader.read_detail(selected)
        except UiReadError as error:
            if detail:
                print(f"[DETAIL] post={selected_number} status=FAILED")
                _detail_stop(error.code)
            raise
        if detail:
            home_body = " ".join((selected.body or "").split())
            expanded_body = " ".join((detail_target.body or "").split())
            extent = (
                "EXPANDED"
                if len(expanded_body) > len(home_body)
                else "TRUNCATED"
                if detail_target.body_truncated
                else "FULL"
            )
            print(
                f"[DETAIL] post={selected_number} status=VERIFIED "
                f"provenance=VERIFIED extent={extent}"
            )
            _print_detail_body(
                "DETAIL",
                f"post={selected_number}",
                "VERIFIED",
                selected_decision,
                extent,
                detail_target.body,
            )
        print("DETAIL_VERIFIED status=PROVENANCE_VERIFIED")
        if detail:
            print(f"[BACK] post={selected_number} status=REQUESTED")
        try:
            await reader.return_to_home()
        except UiReadError as error:
            if detail:
                anchor_status = (
                    "CHANGED" if error.code == "RETURN_CONTEXT_CHANGED" else "UNVERIFIED"
                )
                print(
                    f"[BACK] post={selected_number} status=FAILED "
                    f"anchor={anchor_status} code={error.code}"
                )
                _detail_stop(error.code)
            raise
        if detail:
            print(f"[BACK] post={selected_number} status=RESTORED anchor=MATCHED")
        detail_reads = 1
        pending.remove(selected.fingerprint)
        pending_set.remove(selected.fingerprint)
        status = "COMPLETE"
    elif selected is None and unverified_relevant and not found_relevant:
        print("SEARCH_INCOMPLETE reason=UNVERIFIED_RELEVANCE")
        if detail:
            _detail_stop("UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION")
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
        if detail:
            _detail_stop(
                "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
                if unverified_relevant
                else "OBSERVED_UNVERIFIED_ONLY"
                if body_extracted
                else "OBSERVATION_UNCERTAIN_ONLY"
                if observed_rows
                else "NO_VERIFIED_POSTS_IN_BUDGET"
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
    elif pending_set or (
        found_relevant and exhaustion_reason == "FEED_END_REACHED"
    ):
        incomplete_status = (
            "FEED_END_REACHED"
            if exhaustion_reason == "FEED_END_REACHED"
            else "RELEVANT_POST_NOT_CURRENT"
        )
        print(f"SEARCH_INCOMPLETE reason={incomplete_status}")
        if detail:
            _detail_stop(incomplete_status)
        return NurtureResult(
            status=incomplete_status,
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
    elif exhaustion_reason == "FEED_END_REACHED":
        status = "FEED_END_REACHED"
    elif exhaustion_reason == "FEED_NOT_SCROLLABLE":
        status = "FEED_NOT_SCROLLABLE"
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
    try:
        checkpoint_store.save(updated)
    except BrowserDataError as error:
        if detail:
            _detail_stop(error.code)
        raise
    if detail:
        print(f"[CHECKPOINT] status=SAVED completed_runs={updated.completed_runs}")
        print(f"[CYCLE] status={status}")
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
    detail: bool = False,
) -> NurtureResult:
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")
    if detail and not _detail_terminal_ready():
        raise UiReadError("DETAIL_TTY_REQUIRED")
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
                    detail=detail,
                )
            finally:
                await context.close()
    except (UiReadError, BrowserDataError):
        raise
    except PlaywrightError:
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
    except (OSError, RuntimeError, ValueError):
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
