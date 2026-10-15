"""Privacy-aware terminal diagnostics for the nurture workflow."""

from __future__ import annotations

import unicodedata

from ..feed.models import DisplayObservation, VisibleBodySegment
from ..reader import ThreadsHomeReader, _FeedScanCounts


def _terminal_safe_post_text(body: str) -> str:
    safe: list[str] = []
    for character in body:
        codepoint = ord(character)
        if character == "\n":
            safe.append(character)
        elif character == "\t":
            safe.append("\\t")
        elif unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            safe.append(f"\\u{codepoint:04X}" if codepoint <= 0xFFFF else f"\\U{codepoint:08X}")
        else:
            safe.append(character)
    return "".join(safe)


def _print_post_text(viewport: int, decision: str, body: str | None) -> bool:
    if body is None or not body.strip():
        print(f"POST_TEXT viewport={viewport} status=UNKNOWN decision=UNKNOWN availability=UNKNOWN")
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
        f"<<< {stage}_BODY BEGIN status={status} decision={decision} extent={extent} >>>",
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
    structural_reasons = (
        ",".join(f"{reason}:{count}" for reason, count in sorted(counts.structural_reasons.items()))
        or "none"
    )
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
    shapes = (
        ";".join(
            f"{shape}:count={count}" for shape, count in sorted(counts.url_structure_counts.items())
        )
        or "none"
    )
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
