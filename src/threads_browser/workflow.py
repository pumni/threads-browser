"""Bounded read-only Home / For You workflow."""

from __future__ import annotations

import json
import secrets
import sys
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .feed.models import (
    BodyReadEvidence,
    DisplayObservation,
    PostTarget,
    VisibleBodySegment,
)
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


def _print_visible_segments(
    reference: str,
    segments: tuple[VisibleBodySegment, ...],
) -> int:
    emitted = 0
    for segment in segments:
        print(
            f"[TEXT_SEGMENT {reference}] y={segment.viewport_y:.1f} "
            f"content_y={segment.content_y:.1f} chars={len(segment.text)}",
            flush=True,
        )
        for line in _terminal_safe_post_text(segment.text).split("\n"):
            print(f"| {line}", flush=True)
        emitted += 1
    return emitted


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


def _print_url_structure_diagnostics(counts: _FeedScanCounts) -> None:
    shapes = ";".join(
        f"{shape}:count={count}"
        for shape, count in sorted(counts.url_structure_counts.items())
    ) or "none"
    print(
        "[URL_STRUCTURE] "
        f"timestamp_primary={counts.url_timestamp_links} "
        f"secondary_post_links={counts.url_secondary_links} "
        f"valid_canonical_count={counts.url_canonical_links} "
        f"rejected_count={counts.url_rejected_links} shapes={shapes}",
        flush=True,
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
    viewport_observations: int = 0
    unique_provenance_identities: int = 0
    unique_verified_posts: int = 0
    unique_deferred_posts: int = 0
    unresolved_gaps: int = 0
    visible_text_segments: int = 0
    stop_reason: str = "UNKNOWN"


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
    provenance_verified_fingerprints: set[str] = set()
    classified = 0
    observation_unknown = 0
    unverified_relevant = 0
    unsettled_unverified_relevance = 0
    unresolved_hydration_observation_ids: set[str] = set()
    skipped_rows = 0
    viewport_coverage_complete = True
    decisions_by_fingerprint: dict[str, str] = {}
    previous_unverified_rows: list[tuple[str, float]] = []
    display_dedupe_salt = secrets.token_bytes(16)
    viewport_number = 0
    post_numbers: dict[str, int] = {}
    next_post_number = 0
    exhaustion_reason = "MAX_SCROLLS"
    streamed_home_fingerprints: set[str] = set()
    unresolved_deferred_fingerprints: set[str] = set()
    unresolved_observation_ids: set[str] = set()
    unresolved_unrepresented_gaps: set[str] = set()
    unique_deferred_fingerprints: set[str] = set()
    fully_read_fingerprints: set[str] = set()
    checkpoint_overlap_fingerprints: set[str] = set()
    streamed_segment_ids: set[str] = set()
    observed_body_identities: set[str] = set()
    visible_text_segments = 0
    consecutive_moved_no_content = 0
    pending_scroll_baseline: dict[str, object] | None = None

    def reliability_metrics() -> dict[str, int]:
        return {
            "viewport_observations": observed_rows,
            "unique_provenance_identities": len(provenance_verified_fingerprints),
            "unique_verified_posts": len(fully_read_fingerprints),
            "unique_deferred_posts": len(unique_deferred_fingerprints),
            "unresolved_gaps": (
                len(unresolved_deferred_fingerprints)
                + len(unresolved_observation_ids)
                + len(unresolved_unrepresented_gaps)
            ),
            "visible_text_segments": visible_text_segments,
        }

    async def advance_feed(*, scan_after_scroll: bool = False) -> bool:
        nonlocal pending_scroll_baseline
        attempt = reader.scroll_attempts + 1
        baseline = {
            "visible_text_segments": visible_text_segments,
            "unique_verified_posts": len(fully_read_fingerprints),
            "body_extracted": body_extracted,
            "observed_bodies": len(observed_body_identities),
            "rows": {
                diagnostic.observation_id: diagnostic.read_evidence
                for diagnostic in reader.last_candidate_diagnostics
                if diagnostic.observation_id is not None
                and diagnostic.read_evidence is not None
            },
        }
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
            gesture = reader.last_scroll_gesture
            wheel_steps = 0 if gesture is None else len(gesture.wheel_deltas)
            gesture_ms = 0 if gesture is None else gesture.duration_ms
            if advanced:
                print(
                    f"[SCROLL] attempt={reader.scroll_attempts} status=ADVANCED "
                    f"observed=1 delta_px={reader.last_scroll_delta:g} "
                    f"wheel_steps={wheel_steps} gesture_ms={gesture_ms}"
                )
            else:
                print(
                    f"[SCROLL] attempt={attempt} status=STOPPED "
                    f"reason={reader.last_scroll_stop_reason or 'NOT_ADVANCED'}"
                )
        if advanced:
            pending_scroll_baseline = baseline
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
        row_target_progress_ids: set[str] = set()
        async def observe_row(
            row: _HomeRowObservation,
            *,
            viewport_number: int = viewport_number,
            viewport_relevant_targets: list[PostTarget] = viewport_relevant_targets,
            viewport_fallback_targets: list[PostTarget] = viewport_fallback_targets,
            current_unverified_rows: list[tuple[str, float]] = current_unverified_rows,
            previous_unverified_rows: list[tuple[str, float]] = previous_unverified_rows,
            matched_overlap_rows: set[int] = matched_overlap_rows,
            row_target_progress_ids: set[str] = row_target_progress_ids,
            pending_scroll_baseline: dict[str, object] | None = pending_scroll_baseline,
        ) -> None:
            nonlocal body_extracted, classified, duplicates, found_relevant
            nonlocal irrelevant, next_post_number, observation_unknown, previewed_bodies
            nonlocal relevant, unknown
            nonlocal provenance_verified
            nonlocal unsettled_unverified_relevance
            nonlocal unverified_relevant, viewport_classified, viewport_irrelevant
            nonlocal viewport_observation_unknown, viewport_relevant
            nonlocal viewport_unverified_relevant, viewport_unknown
            nonlocal visible_text_segments

            diagnostic = row.diagnostic
            evidence = diagnostic.read_evidence
            previous_evidence = None
            if pending_scroll_baseline is not None and diagnostic.observation_id:
                previous_rows = pending_scroll_baseline.get("rows", {})
                if isinstance(previous_rows, dict):
                    candidate_evidence = previous_rows.get(diagnostic.observation_id)
                    if isinstance(candidate_evidence, BodyReadEvidence):
                        previous_evidence = candidate_evidence
            if evidence is not None and previous_evidence is not None:
                same_scroller = (
                    evidence.scroll_surface_key
                    == previous_evidence.scroll_surface_key
                    and evidence.scroller_scroll_top is not None
                    and previous_evidence.scroller_scroll_top is not None
                )
                if same_scroller:
                    scroll_top_delta = (
                        evidence.scroller_scroll_top
                        - previous_evidence.scroller_scroll_top
                    )
                    diagnostic.same_scroller_scroll_top_delta = scroll_top_delta
                    scroller_moved = abs(scroll_top_delta) > 1
                    wheel_surface_matches = (
                        reader.last_scroll_surface_key is not None
                        and evidence.scroll_surface_key
                        == reader.last_scroll_surface_key
                    )
                    diagnostic.wheel_surface_matches = wheel_surface_matches
                    diagnostic.row_top_before_px = previous_evidence.row_top_px
                    diagnostic.row_bottom_before_px = previous_evidence.row_bottom_px
                    diagnostic.clip_top_before_px = previous_evidence.clip_top_px
                    diagnostic.clip_bottom_before_px = previous_evidence.clip_bottom_px
                    diagnostic.text_top_before_px = (
                        previous_evidence.text_rect_top_px
                    )
                    diagnostic.text_bottom_before_px = (
                        previous_evidence.text_rect_bottom_px
                    )
                    flags: list[str] = []
                    if not wheel_surface_matches:
                        flags.append("SCROLLER_MISMATCH")
                    row_delta: float | None = None
                    if (
                        previous_evidence.row_top_px is not None
                        and previous_evidence.clip_top_px is not None
                        and evidence.row_top_px is not None
                        and evidence.clip_top_px is not None
                    ):
                        row_delta = (
                            evidence.row_top_px - evidence.clip_top_px
                        ) - (
                            previous_evidence.row_top_px
                            - previous_evidence.clip_top_px
                        )
                        diagnostic.row_movement_px = row_delta
                    distance_decreased = bool(
                        previous_evidence.closest_text_side == "BELOW"
                        and evidence.closest_text_side == "BELOW"
                        and previous_evidence.closest_text_distance_px is not None
                        and evidence.closest_text_distance_px is not None
                        and evidence.closest_text_distance_px
                        < previous_evidence.closest_text_distance_px - 1
                    )
                    if (
                        previous_evidence.closest_text_distance_px is not None
                        and evidence.closest_text_distance_px is not None
                    ):
                        diagnostic.text_distance_delta_px = (
                            evidence.closest_text_distance_px
                            - previous_evidence.closest_text_distance_px
                        )
                    row_moved_toward_view = bool(
                        wheel_surface_matches
                        and scroller_moved
                        and row_delta is not None
                        and row_delta < -1
                        and distance_decreased
                    )
                    if row_moved_toward_view:
                        flags.append("ROW_MOVED_TOWARD_VIEW")
                    same_row_position = row_delta is not None and abs(row_delta) <= 1
                    same_text_position = (
                        previous_evidence.text_rect_top_px is not None
                        and evidence.text_rect_top_px is not None
                        and abs(
                            previous_evidence.text_rect_top_px
                            - evidence.text_rect_top_px
                        ) <= 1
                    )
                    if scroller_moved and same_row_position and same_text_position:
                        flags.append("SCROLLTOP_MOVED_ROW_STATIC")
                    scroll_geometry = reader.last_scroll_geometry or {}
                    height_delta = scroll_geometry.get("scroll_height_delta")
                    expected_row_delta = -scroll_top_delta
                    if (
                        row_delta is not None
                        and abs(row_delta - expected_row_delta)
                        > max(24, abs(scroll_top_delta) * 0.25)
                    ) or (isinstance(height_delta, (int, float)) and abs(height_delta) > 1):
                        flags.append("LAYOUT_SHIFT")
                    if flags:
                        diagnostic.scroll_reason_flags = tuple(flags)
                    if row_moved_toward_view:
                        row_target_progress_ids.add(diagnostic.observation_id)
                elif reader.last_scroll_surface_key is not None:
                    diagnostic.wheel_surface_matches = False
                    diagnostic.scroll_reason_flags = ("SCROLLER_MISMATCH",)

            if (
                (row.target is not None and row.target.body)
                or (row.display is not None and row.display.body)
            ):
                observed_body_identities.add(
                    row.fingerprint or diagnostic.observation_id or
                    f"viewport:{viewport_number}:row:{diagnostic.row_index}"
                )

            hydration_id = diagnostic.observation_id or (
                f"row:{viewport_number}:{diagnostic.row_index}"
            )
            if diagnostic.state == "PENDING_HYDRATION":
                unresolved_hydration_observation_ids.add(hydration_id)
            elif diagnostic.state in {"VERIFIED", "NON_POST"}:
                unresolved_hydration_observation_ids.discard(hydration_id)

            if diagnostic.state == "VERIFIED":
                if row.fingerprint is not None:
                    unresolved_deferred_fingerprints.discard(row.fingerprint)
                if diagnostic.observation_id is not None:
                    unresolved_observation_ids.discard(diagnostic.observation_id)
            elif diagnostic.state != "NON_POST":
                if row.fingerprint is not None:
                    if diagnostic.observation_id is not None:
                        unresolved_observation_ids.discard(
                            diagnostic.observation_id
                        )
                    unresolved_deferred_fingerprints.add(row.fingerprint)
                    unique_deferred_fingerprints.add(row.fingerprint)
                elif diagnostic.observation_id is not None:
                    unresolved_observation_ids.add(diagnostic.observation_id)
                else:
                    unresolved_unrepresented_gaps.add(
                        f"row:{viewport_number}:{diagnostic.row_index}"
                    )
            if detail:
                observation_emitted = bool(
                    (row.target and (row.target.body or row.target.visible_segments))
                    or (row.display and (row.display.body or row.display.visible_segments))
                )
                metrics = evidence
                def metric_count(value: int | None) -> str:
                    return "UNKNOWN" if value is None else str(value)
                print(
                    f"[ROW row_id={diagnostic.observation_id or 'UNTRACKED'} "
                    f"viewport_row={diagnostic.row_index}] root={diagnostic.root_kind} "
                    f"has_visible_time={str(diagnostic.has_visible_time).lower()} "
                    f"has_post_permalink={str(diagnostic.has_post_permalink).lower()} "
                    f"has_profile_author={str(diagnostic.has_profile_author).lower()} "
                    f"has_readable_body={str(diagnostic.has_readable_body).lower()} "
                    f"root_intersects={'UNKNOWN' if metrics is None or metrics.root_intersects is None else str(metrics.root_intersects).lower()} "
                    f"body_text_nodes_present={metric_count(None if metrics is None else metrics.body_text_nodes_present)} "
                    f"render_rect_count={metric_count(None if metrics is None else metrics.render_rect_count)} "
                    f"visible_text_rect_count={metric_count(None if metrics is None else metrics.visible_text_rect_count)} "
                    f"deferred_above_rect_count={metric_count(None if metrics is None else metrics.deferred_above_rect_count)} "
                    f"deferred_below_rect_count={metric_count(None if metrics is None else metrics.deferred_below_rect_count)} "
                    f"closest_text_distance_px={'UNKNOWN' if metrics is None or metrics.closest_text_distance_px is None else f'{metrics.closest_text_distance_px:.1f}'} "
                    f"root_position_band={'UNKNOWN' if metrics is None else metrics.root_position_band} "
                    f"same_scroller_scrollTop_delta={'UNKNOWN' if diagnostic.same_scroller_scroll_top_delta is None else f'{diagnostic.same_scroller_scroll_top_delta:.1f}'} "
                    f"scroll_surface={'UNKNOWN' if metrics is None else metrics.scroll_surface_key} "
                    f"body_evidence={'UNKNOWN' if metrics is None else metrics.state} "
                    f"scroll_flags={','.join(diagnostic.scroll_reason_flags) or 'NONE'} "
                    f"state={diagnostic.state} reason={diagnostic.reason} "
                    f"observation={'EMITTED' if observation_emitted else 'RECORDED'} "
                    "checkpoint=PENDING",
                    flush=True,
                )
            target = row.target
            if target is not None:
                if target.fingerprint not in provenance_verified_fingerprints:
                    provenance_verified_fingerprints.add(target.fingerprint)
                    provenance_verified += 1
                if diagnostic.state == "PARTIAL_BODY":
                    new_segments = tuple(
                        segment
                        for segment in target.visible_segments
                        if segment.segment_id not in streamed_segment_ids
                    )
                    visible_text_segments += len(new_segments)
                    streamed_segment_ids.update(
                        segment.segment_id for segment in new_segments
                    )
                    if detail or show_post_text:
                        _print_visible_segments(
                            f"viewport={viewport_number} row={diagnostic.row_index}",
                            new_segments,
                        )
                        if new_segments:
                            streamed_home_fingerprints.add(target.fingerprint)
                    return
                if diagnostic.state != "VERIFIED" or not target.body or not target.body_complete:
                    return
                unresolved_deferred_fingerprints.discard(target.fingerprint)
                if target.fingerprint not in fully_read_fingerprints:
                    fully_read_fingerprints.add(target.fingerprint)
                    reader.last_scan_counts.new_verified_rows += 1
                new_segments = tuple(
                    segment
                    for segment in target.visible_segments
                    if segment.segment_id not in streamed_segment_ids
                )
                visible_text_segments += len(new_segments)
                streamed_segment_ids.update(
                    segment.segment_id for segment in new_segments
                )
                if (
                    new_segments
                    and target.fingerprint in streamed_home_fingerprints
                    and (detail or show_post_text)
                ):
                    _print_visible_segments(
                        f"viewport={viewport_number} row={diagnostic.row_index}",
                        new_segments,
                    )
                if target.fingerprint in seen_this_run:
                    if (
                        target.fingerprint in previously_seen
                        and target.fingerprint in checkpoint_overlap_fingerprints
                    ):
                        duplicates += 1
                    elif target.fingerprint in previously_seen:
                        checkpoint_overlap_fingerprints.add(target.fingerprint)
                    if detail and target.fingerprint not in streamed_home_fingerprints:
                        print(
                            f"[POST post={post_numbers[target.fingerprint]}] "
                            "status=VERIFIED decision=OVERLAP_DUPLICATE HOME_BODY=OMITTED",
                            flush=True,
                        )
                    if (
                        decisions_by_fingerprint.get(target.fingerprint) == "RELEVANT"
                        and target.fingerprint in pending_set
                        and target.currently_visible
                    ):
                        viewport_relevant_targets.append(target)
                    elif (
                        target.body.strip()
                        and decisions_by_fingerprint.get(target.fingerprint) != "RELEVANT"
                        and target.currently_visible
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
                    else:
                        print(
                            f"[POST post={post_numbers[target.fingerprint]}] "
                            f"status=VERIFIED decision={decision} "
                            f"extent={'TRUNCATED' if target.body_truncated else 'COMPLETE'} "
                            f"chars={len(target.body)} text=STREAMED",
                            flush=True,
                        )
                elif (
                    show_post_text
                    and target.fingerprint not in streamed_home_fingerprints
                    and _print_post_text(
                    viewport_number, decision, target.body
                    )
                ):
                    previewed_bodies += 1
                if decision == "RELEVANT":
                    relevant += 1
                    viewport_relevant += 1
                    found_relevant = True
                    if is_new and target.fingerprint not in pending_set:
                        pending.append(target.fingerprint)
                        pending_set.add(target.fingerprint)
                    if target.fingerprint in pending_set and target.currently_visible:
                        viewport_relevant_targets.append(target)
                elif decision == "IRRELEVANT":
                    irrelevant += 1
                    viewport_irrelevant += 1
                    if target.currently_visible:
                        viewport_fallback_targets.append(target)
                else:
                    unknown += 1
                    viewport_unknown += 1
                    if target.currently_visible:
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
            body_digest = sha256(
                display_dedupe_salt + observation.body.encode("utf-8")
            ).hexdigest()
            overlap_duplicate = False
            for previous_index, (previous_digest, previous_y) in enumerate(
                previous_unverified_rows
            ):
                if matched_overlap_rows and previous_index in matched_overlap_rows:
                    continue
                if previous_digest != body_digest:
                    continue
                if abs((previous_y - reader.last_scroll_delta) - observation.viewport_y) <= 48:
                    matched_overlap_rows.add(previous_index)
                    overlap_duplicate = True
                    break
            if not overlap_duplicate:
                overlap_duplicate = any(
                    current_digest == body_digest
                    and abs(current_y - observation.viewport_y) <= 2
                    for current_digest, current_y in current_unverified_rows
                )
            if overlap_duplicate:
                if detail:
                    print(
                        f"[POST viewport={viewport_number} row={diagnostic.row_index}] "
                        "status=OBSERVED_UNVERIFIED decision=OVERLAP_DUPLICATE "
                        "HOME_BODY=OMITTED",
                        flush=True,
                    )
                return
            current_unverified_rows.append((body_digest, observation.viewport_y))
            body_extracted += 1
            decision = classify_body(observation.body, normalized_terms)
            classified += 1
            viewport_classified += 1
            if decision == "RELEVANT":
                decision = "RELEVANT_UNVERIFIED"
                unverified_relevant += 1
                if diagnostic.state != "OBSERVED_UNVERIFIED":
                    unsettled_unverified_relevance += 1
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
                scanned_fingerprints=seen_this_run,
                on_row=observe_row,
            )
        except UiReadError as error:
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
                _print_url_structure_diagnostics(counts)
                print(
                    f"[VIEWPORT {viewport_number}] status=PARTIAL code={error.code}",
                    flush=True,
                )
            if error.code.endswith("NOT_READY") and (
                seen_this_run or body_extracted or observed_rows
            ):
                counts.coverage_complete = False
                observed_rows += counts.observed_rows
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
                    **reliability_metrics(),
                )
            if detail:
                _detail_stop(error.code)
            raise
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
            _print_url_structure_diagnostics(scan_counts)
        # Only use this immediate-viewport geometry/body comparison to suppress
        # duplicate overlap work; unverified rows receive no stable identity.
        previous_unverified_rows = current_unverified_rows
        observed_rows += scan_counts.observed_rows
        skipped_rows += scan_counts.skipped_rows
        unresolved_rows = [
            row
            for row in reader.last_candidate_diagnostics
            if row.state not in {"NON_POST", "VERIFIED"}
        ]
        if scan_counts.roots_truncated or (
            not scan_counts.coverage_complete and not unresolved_rows
        ):
            unresolved_unrepresented_gaps.add(f"viewport:{viewport_number}")
        viewport_coverage_complete = not (
            unresolved_deferred_fingerprints
            or unresolved_observation_ids
            or unresolved_unrepresented_gaps
        )

        if pending_scroll_baseline is not None:
            baseline_segments = int(
                pending_scroll_baseline.get("visible_text_segments", visible_text_segments)
            )
            baseline_verified = int(
                pending_scroll_baseline.get(
                    "unique_verified_posts", len(fully_read_fingerprints)
                )
            )
            baseline_bodies = int(
                pending_scroll_baseline.get(
                    "observed_bodies",
                    len(observed_body_identities),
                )
            )
            new_segments = max(0, visible_text_segments - baseline_segments)
            new_observed_bodies = max(
                0,
                len(observed_body_identities) - baseline_bodies,
            )
            new_verified = max(
                0,
                len(fully_read_fingerprints) - baseline_verified,
            )
            approach_deltas = [
                -diagnostic.text_distance_delta_px
                for diagnostic in reader.last_candidate_diagnostics
                if "ROW_MOVED_TOWARD_VIEW" in diagnostic.scroll_reason_flags
                and diagnostic.text_distance_delta_px is not None
            ]
            text_approach_delta = max(approach_deltas, default=None)
            per_row_approach = bool(row_target_progress_ids)
            made_progress = bool(
                new_segments or new_observed_bodies or new_verified or per_row_approach
            )
            consecutive_moved_no_content = (
                0 if made_progress else consecutive_moved_no_content + 1
            )
            for diagnostic in reader.last_candidate_diagnostics:
                metrics = diagnostic.read_evidence
                if metrics is None:
                    continue
                def px(value: float | None) -> str:
                    return "UNKNOWN" if value is None else f"{value:.1f}"

                def count(value: int | None) -> str:
                    return "UNKNOWN" if value is None else str(value)

                print(
                    f"[SCROLL_ROW_PROGRESS] attempt={reader.scrolls} "
                    f"row_id={diagnostic.observation_id or 'UNTRACKED'} "
                    f"root_intersects={'UNKNOWN' if metrics.root_intersects is None else str(metrics.root_intersects).lower()} "
                    f"body_text_nodes_present={count(metrics.body_text_nodes_present)} "
                    f"render_rect_count={count(metrics.render_rect_count)} "
                    f"visible_text_rect_count={count(metrics.visible_text_rect_count)} "
                    f"deferred_above_rect_count={count(metrics.deferred_above_rect_count)} "
                    f"deferred_below_rect_count={count(metrics.deferred_below_rect_count)} "
                    f"closest_text_distance_px={px(metrics.closest_text_distance_px)} "
                    f"root_position_band={metrics.root_position_band} "
                    f"same_scroller_scrollTop_delta={'UNKNOWN' if diagnostic.same_scroller_scroll_top_delta is None else f'{diagnostic.same_scroller_scroll_top_delta:.1f}'} "
                    f"target_approached={str(diagnostic.observation_id in row_target_progress_ids).lower()} "
                    f"scroll_surface_key={metrics.scroll_surface_key or 'UNKNOWN'} "
                    f"wheel_surface_ancestor={'UNKNOWN' if diagnostic.wheel_surface_matches is None else str(diagnostic.wheel_surface_matches).lower()} "
                    f"row_top_before_px={px(diagnostic.row_top_before_px)} "
                    f"row_bottom_before_px={px(diagnostic.row_bottom_before_px)} "
                    f"clip_top_before_px={px(diagnostic.clip_top_before_px)} "
                    f"clip_bottom_before_px={px(diagnostic.clip_bottom_before_px)} "
                    f"text_top_before_px={px(diagnostic.text_top_before_px)} "
                    f"text_bottom_before_px={px(diagnostic.text_bottom_before_px)} "
                    f"row_top_px={px(metrics.row_top_px)} "
                    f"row_bottom_px={px(metrics.row_bottom_px)} "
                    f"clip_top_px={px(metrics.clip_top_px)} "
                    f"clip_bottom_px={px(metrics.clip_bottom_px)} "
                    f"text_top_px={px(metrics.text_rect_top_px)} "
                    f"text_bottom_px={px(metrics.text_rect_bottom_px)} "
                    f"row_movement_px={px(diagnostic.row_movement_px)} "
                    f"text_distance_delta_px={px(diagnostic.text_distance_delta_px)} "
                    f"scroll_flags={','.join(diagnostic.scroll_reason_flags) or 'NONE'} "
                    f"body_evidence={metrics.state}",
                    flush=True,
                )
            geometry = reader.last_scroll_geometry or {}
            if geometry:
                print(
                    f"[SCROLL_GEOMETRY] surface_key={geometry.get('surface_key', 'UNKNOWN')} "
                    f"surface_tag={geometry.get('surface_tag', 'UNKNOWN')} "
                    f"overflow_y={geometry.get('overflow_y', 'UNKNOWN')} "
                    "hit_test=VALIDATED "
                    f"hit_test_tag={geometry.get('hit_test_tag', 'UNKNOWN')} "
                    f"scrollTop_before={geometry.get('scroll_top_before', 'UNKNOWN')} "
                    f"scrollTop_after={geometry.get('scroll_top_after', 'UNKNOWN')} "
                    f"scrollTop_delta={geometry.get('scroll_top_delta', 'UNKNOWN')} "
                    f"clip_top_before={geometry.get('clip_top_before', 'UNKNOWN')} "
                    f"clip_top_after={geometry.get('clip_top_after', 'UNKNOWN')} "
                    f"clip_bottom_before={geometry.get('clip_bottom_before', 'UNKNOWN')} "
                    f"clip_bottom_after={geometry.get('clip_bottom_after', 'UNKNOWN')} "
                    f"scroll_height_before={geometry.get('scroll_height_before', 'UNKNOWN')} "
                    f"scroll_height_after={geometry.get('scroll_height_after', 'UNKNOWN')} "
                    f"scroll_height_delta={geometry.get('scroll_height_delta', 'UNKNOWN')}",
                    flush=True,
                )
            print(
                f"[SCROLL_PROGRESS] attempt={reader.scrolls} "
                f"state={'ADVANCED_WITH_CONTENT' if made_progress else 'MOVED_NO_NEW_CONTENT'} "
                f"new_segments={new_segments} "
                f"new_observed_bodies={new_observed_bodies} "
                f"new_verified_posts={new_verified} "
                f"tracked_rows_approaching={len(row_target_progress_ids)} "
                f"text_approach_delta_px={'UNKNOWN' if text_approach_delta is None else f'{text_approach_delta:.1f}'} "
                f"deferred_above_rects={scan_counts.deferred_above} "
                f"deferred_below_rects={scan_counts.deferred_below} "
                f"viewport_row_appearances={observed_rows} "
                f"unique_provenance_identities={len(provenance_verified_fingerprints)} "
                f"unique_verified_posts={len(fully_read_fingerprints)} "
                f"unresolved_gaps={len(unresolved_deferred_fingerprints) + len(unresolved_observation_ids) + len(unresolved_unrepresented_gaps)} "
                f"band={reader.last_scroll_band} "
                f"end_proximity={'NEAR' if reader.last_scroll_near_end else 'FAR'} "
                f"end_distance_px={'UNKNOWN' if reader.last_scroll_end_distance is None else f'{reader.last_scroll_end_distance:.1f}'} "
                f"no_new_content_streak={consecutive_moved_no_content}",
                flush=True,
            )
            pending_scroll_baseline = None
            if consecutive_moved_no_content >= 3:
                if (
                    reader.last_scroll_end_distance is not None
                    and reader.last_scroll_end_distance <= 1
                    and viewport_coverage_complete
                ):
                    exhaustion_reason = "FEED_END_REACHED"
                else:
                    exhaustion_reason = "READ_PROGRESS_STALLED"
                    unresolved_unrepresented_gaps.add(
                        f"stalled:{reader.scrolls}"
                    )
                    viewport_coverage_complete = False
                print(
                    f"[SCROLL_PROGRESS] status=STOPPED reason={exhaustion_reason} "
                    "coverage=INCOMPLETE "
                    f"unresolved_gaps={len(unresolved_deferred_fingerprints) + len(unresolved_observation_ids) + len(unresolved_unrepresented_gaps)}",
                    flush=True,
                )
                break
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
            f"provenance_verified_current={scan_counts.identity_verified} "
            f"body_complete_current={scan_counts.verified} "
            f"unique_provenance_identities={len(provenance_verified_fingerprints)} "
            f"unique_verified_posts={len(fully_read_fingerprints)} "
            f"viewport_row_appearances={observed_rows} "
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
                    f"scan={'PARTIAL' if reader.last_viewport_can_wheel else 'NOT_COMMITTED'} "
                    f"wheel={'ALLOWED' if reader.last_viewport_can_wheel else 'BLOCKED'}"
                )
            if not reader.last_viewport_can_wheel:
                if detail:
                    _detail_stop("VIEWPORT_COVERAGE_INCOMPLETE")
                return NurtureResult(
                    status="VIEWPORT_COVERAGE_INCOMPLETE",
                    stop_reason=exhaustion_reason,
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
                    **reliability_metrics(),
                )
        if (
            unresolved_deferred_fingerprints
            or unresolved_observation_ids
            or unresolved_unrepresented_gaps
        ) and not (unverified_relevant and not found_relevant):
            if reader.scroll_attempts < max_scrolls and await advance_feed():
                continue
            if reader.scroll_attempts >= max_scrolls:
                exhaustion_reason = "SCROLL_BUDGET_EXHAUSTED_WITH_GAP"
                print(
                    "COVERAGE_STOP reason=SCROLL_BUDGET_EXHAUSTED_WITH_GAP "
                    f"unresolved_gaps={len(unresolved_deferred_fingerprints) + len(unresolved_observation_ids) + len(unresolved_unrepresented_gaps)}",
                    flush=True,
                )
            if detail:
                _detail_stop("VIEWPORT_COVERAGE_INCOMPLETE")
            return NurtureResult(
                status="VIEWPORT_COVERAGE_INCOMPLETE",
                stop_reason=exhaustion_reason,
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
                **reliability_metrics(),
            )
        if not viewport_coverage_complete:
            can_continue_observing = bool(
                scan_counts.coverage_complete
                or reader.last_viewport_can_wheel
                or reader.last_viewport_deferred_only
            )
            if (
                can_continue_observing
                and reader.scroll_attempts < max_scrolls
                and await advance_feed()
            ):
                continue
            exhaustion_reason = (
                reader.last_scroll_stop_reason or "VIEWPORT_COVERAGE_INCOMPLETE"
            )
            break
        if viewport_relevant_targets:
            viewport_relevant_targets.sort(
                key=lambda target: target.fingerprint in previously_seen
            )
            if max_scrolls > 0 and reader.scroll_attempts == 0:
                if await advance_feed(
                    scan_after_scroll=len(seen_this_run) >= max_total_posts
                ):
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
            exhaustion_reason = (
                "SCROLL_BUDGET_EXHAUSTED_WITH_GAP"
                if (
                    unresolved_deferred_fingerprints
                    or unresolved_observation_ids
                    or unresolved_unrepresented_gaps
                )
                else "MAX_SCROLLS"
            )
            select_verified_fallback(viewport_fallback_targets)
            break
        if not await advance_feed():
            exhaustion_reason = reader.last_scroll_stop_reason or "FEED_NOT_ADVANCED"
            if exhaustion_reason in {"FEED_END_REACHED", "FEED_NOT_SCROLLABLE"}:
                select_verified_fallback(viewport_fallback_targets)
            break

    if exhaustion_reason == "SCROLL_BUDGET_EXHAUSTED_WITH_GAP":
        print(
            "COVERAGE_STOP reason=SCROLL_BUDGET_EXHAUSTED_WITH_GAP "
            f"unresolved_gaps={len(unresolved_deferred_fingerprints) + len(unresolved_observation_ids) + len(unresolved_unrepresented_gaps)}",
            flush=True,
        )

    detail_reads = 0
    unverified_relevance_only = bool(
        selected is None
        and unverified_relevant
        and not found_relevant
        and reader.skipped_unverified == 0
        and unsettled_unverified_relevance == 0
        and not unresolved_hydration_observation_ids
    )
    if (
        not viewport_coverage_complete
        or unresolved_deferred_fingerprints
        or unresolved_observation_ids
        or unresolved_unrepresented_gaps
    ) and not unverified_relevance_only:
        if detail:
            _detail_stop("VIEWPORT_COVERAGE_INCOMPLETE")
        else:
            print("CHECKPOINT status=NOT_SAVED reason=VIEWPORT_COVERAGE_INCOMPLETE")
        return NurtureResult(
            status="VIEWPORT_COVERAGE_INCOMPLETE",
            stop_reason=exhaustion_reason,
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
            **reliability_metrics(),
        )
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
            stop_reason="UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
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
            **reliability_metrics(),
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
            stop_reason=(
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
            **reliability_metrics(),
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
            stop_reason=incomplete_status,
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
            **reliability_metrics(),
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
        stop_reason=exhaustion_reason,
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
        viewport_observations=observed_rows,
        unique_verified_posts=len(fully_read_fingerprints),
        unique_deferred_posts=len(unique_deferred_fingerprints),
        unresolved_gaps=(
            len(unresolved_deferred_fingerprints)
            + len(unresolved_observation_ids)
            + len(unresolved_unrepresented_gaps)
        ),
        unique_provenance_identities=len(provenance_verified_fingerprints),
        visible_text_segments=visible_text_segments,
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
