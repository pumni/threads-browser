"""Bounded hydration and provenance recovery for one candidate row."""

from __future__ import annotations

import secrets
import time

from ..feed.models import BodyReadEvidence, DisplayObservation, PostTarget
from .body_text import _body_progress_state
from .card_provenance import _CardDeferredOffscreen
from .feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from .loading import CardNotReady as _CardNotReady
from .viewport_row_models import CandidateWork, ViewportRowScanHost


class ViewportRowRecovery:
    """Own the row-specific wait and ambiguous-provenance fallback rules."""

    def __init__(self, host: ViewportRowScanHost) -> None:
        self._host = host

    async def handle_pending(
        self, work: CandidateWork, pending: _CardNotReady
    ) -> None:
        """Observe or retry a card whose permalink, author or body is pending."""
        state = work.state
        row_index = work.row_index
        root = work.root
        structure = work.structure
        diagnostic = work.diagnostic
        box = work.box
        read_started = work.read_started
        emit_row = work.emit_row
        request = state.request
        currently_visible = request.currently_visible
        prefer_new_fingerprints = request.prefer_new_fingerprints
        counts = state.counts
        candidate_dom_handles = state.candidate_dom_handles
        candidate_prior_fingerprints = state.candidate_prior_fingerprints
        display_observations = state.display_observations
        pending_signals = state.pending_signals
        targets = state.targets
        if pending.reason_code == "POST_ROOT_DOM_IDENTITY_CHANGED":
            replacement_handle = await root.element_handle()
            previous_handle = candidate_dom_handles.get(row_index)
            if previous_handle is not None:
                await previous_handle.dispose()
            if replacement_handle is None:
                candidate_dom_handles.pop(row_index, None)
            else:
                candidate_dom_handles[row_index] = replacement_handle
            candidate_prior_fingerprints[row_index] = None
            diagnostic.observation_id = secrets.token_hex(8)
            diagnostic.identity_fingerprint = None
            diagnostic.author_fingerprint = None
        self._host.last_scan_timings.read_row_ms += round(
            (time.perf_counter() - read_started) * 1000
        )
        counts.card_not_ready += 1
        body, body_signals, read_evidence = await self._host._observe_display_body(root)
        diagnostic.read_evidence = read_evidence
        diagnostic.has_readable_body = bool(body)
        should_wait_for_permalink = (
            pending.reason_code == "POST_PERMALINK_PENDING"
            and diagnostic.root_kind in {"ARTICLE", "ROLE_ARTICLE", "PRESSABLE"}
        )
        should_wait_for_missing_author = pending.reason_code == "POST_AUTHOR_PENDING"
        signals = (
            pending.signals
            if should_wait_for_permalink or should_wait_for_missing_author
            else body_signals
        )
        if signals:
            pending_signals.extend(signals)
        signal_is_visible = False
        for signal in signals:
            if await self._host._is_in_visible_scrollports(signal.locator):
                signal_is_visible = True
                break
            if await signal.locator.count() == 0 and await self._host._is_in_visible_scrollports(
                root
            ):
                signal_is_visible = True
                break
        if signals and not signal_is_visible:
            self._host.last_scan_timings.wait_reason = "DEFERRED_IDENTITY"
            if should_wait_for_permalink or should_wait_for_missing_author:
                diagnostic.state = "DEFERRED_IDENTITY"
                diagnostic.reason = "READINESS_SIGNAL_OUTSIDE_VISIBLE_SCROLLPORT"
            else:
                diagnostic.state, diagnostic.reason = _body_progress_state(
                    read_evidence,
                    has_readable_body=bool(body),
                )
            counts.deferred_offscreen += 1
            counts.skipped_rows += 1
            display = DisplayObservation(
                body,
                "OBSERVATION_UNCERTAIN" if body is None else "OBSERVED_UNVERIFIED",
                box["y"],
                read_evidence=read_evidence,
            )
            if body:
                counts.body_extracted += 1
            else:
                counts.body_unknown += 1
            display_observations.append(display)
            await emit_row(diagnostic, display=display)
            return
        if should_wait_for_permalink or should_wait_for_missing_author:
            diagnostic.state = "PENDING_HYDRATION"
            diagnostic.reason = pending.reason_code
            counts.skipped_rows += 1
            counts.uncertain_rows += 1
            counts.body_unknown += int(not body)
            if body:
                counts.body_extracted += 1
        elif body:
            diagnostic.state = "OBSERVED_UNVERIFIED"
            diagnostic.reason = pending.reason_code
            counts.body_extracted += 1
        elif body_signals:
            diagnostic.state = "PENDING_HYDRATION"
            diagnostic.reason = "POST_BODY_PENDING"
            counts.body_unknown += 1
            counts.skipped_rows += 1
            counts.uncertain_rows += 1
        else:
            diagnostic.state, diagnostic.reason = _body_progress_state(
                read_evidence,
                has_readable_body=False,
            )
            counts.body_unknown += 1
            counts.skipped_rows += 1
            counts.uncertain_rows += 1
        if body:
            display_observations.append(
                DisplayObservation(
                    body,
                    viewport_y=box["y"],
                    read_evidence=read_evidence,
                )
            )
        else:
            display_observations.append(
                DisplayObservation(
                    None,
                    "OBSERVATION_UNCERTAIN",
                    box["y"],
                    read_evidence=read_evidence,
                )
            )
        wait_started = time.perf_counter()
        if signal_is_visible:
            self._host.last_scan_timings.wait_reason = "CARD_HYDRATION"
            changed = await self._host._wait_for_pending_card(
                _CardNotReady(signals=tuple(signals)),
                self._host._readiness_deadline(state.hooks.card_hydration_wait_ms),
                max_wait_ms=state.hooks.card_hydration_wait_ms,
            )
            self._host.last_scan_timings.stabilize_ms += round(
                (time.perf_counter() - wait_started) * 1000
            )
            if not changed:
                self._host.last_scan_timings.wait_reason = "CARD_HYDRATION_TIMEOUT"
            if changed:
                self._host.last_scan_timings.wait_reason = "CARD_HYDRATION"
                if currently_visible:
                    quiet_started = time.perf_counter()
                    body_quiet = await self._host._wait_for_stable_card_dom(root)
                    self._host.last_scan_timings.stabilize_ms += round(
                        (time.perf_counter() - quiet_started) * 1000
                    )
                    if not body_quiet:
                        self._host.last_scan_timings.wait_reason = "CARD_BODY_NOT_STABLE"
                        await emit_row(
                            diagnostic,
                            display=display_observations[-1]
                            if display_observations
                            else None,
                        )
                        return
                try:
                    if currently_visible:
                        target, body_stable = await self._host._read_stable_visible_card(
                            root,
                            viewport_y=box["y"],
                            cached_body_fingerprints=(prefer_new_fingerprints),
                            expected_root_handle=candidate_dom_handles.get(row_index),
                            allow_hydration_retry=(
                                structure.readable_body_candidates == 0
                            ),
                        )
                        if not body_stable:
                            self._host.last_scan_timings.wait_reason = "CARD_BODY_NOT_STABLE"
                            await emit_row(
                                diagnostic,
                                display=display_observations[-1]
                                if display_observations
                                else None,
                            )
                            return
                    else:
                        target = await self._host._read_card(
                            root,
                            currently_visible=False,
                            viewport_y=box["y"],
                            cached_body_fingerprints=prefer_new_fingerprints,
                        )
                except (_CardNotReady, _UnverifiedFeedCandidate, _CardDeferredOffscreen):
                    pass
                else:
                    counts.skipped_rows = max(0, counts.skipped_rows - 1)
                    counts.uncertain_rows = max(0, counts.uncertain_rows - 1)
                    if body:
                        counts.body_extracted = max(
                            0,
                            counts.body_extracted - 1,
                        )
                    else:
                        counts.body_unknown = max(0, counts.body_unknown - 1)
                    if display_observations:
                        display_observations.pop()
                    diagnostic.identity_fingerprint = target.fingerprint
                    if candidate_prior_fingerprints.get(row_index) not in {
                        None,
                        target.fingerprint,
                    }:
                        diagnostic.observation_id = secrets.token_hex(8)
                    candidate_prior_fingerprints[row_index] = target.fingerprint
                    diagnostic.read_evidence = target.read_evidence
                    diagnostic.has_readable_body = bool(target.body and target.body.strip())
                    if target.body_complete:
                        diagnostic.state = "VERIFIED"
                        diagnostic.reason = "PROVENANCE_VERIFIED"
                    else:
                        diagnostic.state, diagnostic.reason = _body_progress_state(
                            target.read_evidence,
                            has_readable_body=diagnostic.has_readable_body,
                        )
                    counts.identity_verified += 1
                    counts.verified += int(target.body_complete)
                    counts.partial_body_rows += int(not target.body_complete)
                    counts.body_read += int(
                        diagnostic.has_readable_body and target.body_complete
                    )
                    counts.body_extracted += int(diagnostic.has_readable_body)
                    if not diagnostic.has_readable_body:
                        counts.body_unknown += 1
                        counts.uncertain_rows += 1
                        counts.skipped_rows += 1
                        if diagnostic.state == "UNSUPPORTED_STRUCTURE":
                            self._host._record_structure_reason("POST_BODY_STRUCTURE_UNSUPPORTED")
                        elif diagnostic.state == "DEFERRED_TEXT_ABOVE":
                            counts.deferred_above += 1
                        elif diagnostic.state == "DEFERRED_TEXT_BELOW":
                            counts.deferred_below += 1
                    elif target.body_complete:
                        targets.append(target)
                    await emit_row(diagnostic, target=target)
                    return
        await emit_row(
            diagnostic,
            display=display_observations[-1],
        )
        return

    async def handle_unverified(
        self, work: CandidateWork, error: _UnverifiedFeedCandidate
    ) -> tuple[PostTarget, bool] | None:
        """Recover only a visibly ambiguous wrapper with matching row identity."""
        state = work.state
        row_index = work.row_index
        root = work.root
        diagnostic = work.diagnostic
        root_handle = work.root_handle
        box = work.box
        read_started = work.read_started
        emit_row = work.emit_row
        request = state.request
        currently_visible = request.currently_visible
        prefer_new_fingerprints = request.prefer_new_fingerprints
        stage = request.stage
        counts = state.counts
        candidate_prior_fingerprints = state.candidate_prior_fingerprints
        display_observations = state.display_observations
        pending_signals = state.pending_signals
        if (
            error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS"
            and currently_visible
        ):
            try:
                target, body_stable = await self._host._recover_ambiguous_visible_card(
                    root,
                    root_handle,
                    viewport_y=box["y"],
                    cached_body_fingerprints=prefer_new_fingerprints,
                )
            except _CardNotReady as pending:
                self._host._record_scan_skip(stage, error)
                identity_changed = (
                    pending.reason_code == "POST_ROOT_DOM_IDENTITY_CHANGED"
                    or not await self._host._same_dom_element(root, root_handle)
                )
                if identity_changed:
                    diagnostic.observation_id = secrets.token_hex(8)
                    diagnostic.identity_fingerprint = None
                    diagnostic.author_fingerprint = None
                    candidate_prior_fingerprints[row_index] = None
                    diagnostic.state = "REJECTED"
                    diagnostic.reason = "POST_ROOT_IDENTITY_CHANGED"
                    read_evidence = BodyReadEvidence(state="NO_BODY_NODES")
                    body = None
                else:
                    observed = await self._host._read_visible_body_progress(root, root_handle)
                    read_evidence = observed.read_evidence
                    body = observed.body if observed.has_rendered_text else None
                    diagnostic.state = (
                        "OBSERVED_UNVERIFIED" if body else "REJECTED"
                    )
                    diagnostic.reason = "ROW_PROVENANCE_UNRESOLVED"
                    self._host._record_structure_reason("ROW_PROVENANCE_UNRESOLVED")
                diagnostic.read_evidence = read_evidence
                diagnostic.has_readable_body = bool(body)
                counts.coverage_complete = False
                counts.uncertain_rows += 1
                counts.skipped_rows += 1
                if body:
                    counts.body_extracted += 1
                else:
                    counts.body_unknown += 1
                display = DisplayObservation(
                    body,
                    "OBSERVATION_UNCERTAIN"
                    if body is None
                    else "OBSERVED_UNVERIFIED",
                    box["y"],
                    read_evidence=read_evidence,
                )
                display_observations.append(display)
                self._host.last_scan_timings.read_row_ms += round(
                    (time.perf_counter() - read_started) * 1000
                )
                await emit_row(diagnostic, display=display)
                return None
            except (_CardDeferredOffscreen, _UnverifiedFeedCandidate):
                self._host._record_scan_skip(stage, error)
                identity_changed = not await self._host._same_dom_element(root, root_handle)
                if identity_changed:
                    diagnostic.observation_id = secrets.token_hex(8)
                    diagnostic.identity_fingerprint = None
                    diagnostic.author_fingerprint = None
                    candidate_prior_fingerprints[row_index] = None
                    diagnostic.state = "REJECTED"
                    diagnostic.reason = "POST_ROOT_IDENTITY_CHANGED"
                    read_evidence = BodyReadEvidence(state="NO_BODY_NODES")
                    body = None
                else:
                    observed = await self._host._read_visible_body_progress(root, root_handle)
                    read_evidence = observed.read_evidence
                    body = observed.body if observed.has_rendered_text else None
                    diagnostic.state = (
                        "OBSERVED_UNVERIFIED" if body else "REJECTED"
                    )
                    diagnostic.reason = "ROW_PROVENANCE_UNRESOLVED"
                    self._host._record_structure_reason("ROW_PROVENANCE_UNRESOLVED")
                diagnostic.read_evidence = read_evidence
                diagnostic.has_readable_body = bool(body)
                counts.coverage_complete = False
                counts.uncertain_rows += 1
                counts.skipped_rows += 1
                if body:
                    counts.body_extracted += 1
                else:
                    counts.body_unknown += 1
                display = DisplayObservation(
                    body,
                    "OBSERVATION_UNCERTAIN"
                    if body is None
                    else "OBSERVED_UNVERIFIED",
                    box["y"],
                    read_evidence=read_evidence,
                )
                display_observations.append(display)
                self._host.last_scan_timings.read_row_ms += round(
                    (time.perf_counter() - read_started) * 1000
                )
                await emit_row(diagnostic, display=display)
                return None
            if not body_stable:
                diagnostic.identity_fingerprint = target.fingerprint
                candidate_prior_fingerprints[row_index] = target.fingerprint
                diagnostic.read_evidence = target.read_evidence
                diagnostic.has_readable_body = bool(target.body and target.body.strip())
                diagnostic.state = "OBSERVED_UNVERIFIED"
                diagnostic.reason = "ROW_CONTENT_CHANGED_DURING_READ"
                counts.identity_verified += 1
                counts.uncertain_rows += 1
                counts.skipped_rows += 1
                counts.coverage_complete = False
                if target.body:
                    counts.body_extracted += 1
                else:
                    counts.body_unknown += 1
                display = DisplayObservation(
                    target.body,
                    "OBSERVATION_UNCERTAIN"
                    if not target.body
                    else "OBSERVED_UNVERIFIED",
                    box["y"],
                    read_evidence=target.read_evidence,
                )
                display_observations.append(display)
                await emit_row(
                    diagnostic,
                    target=target,
                    display=display,
                    fingerprint=target.fingerprint,
                )
                return None
        else:
            self._host._record_scan_skip(stage, error)
            body, body_signals, read_evidence = await self._host._observe_display_body(root)
            diagnostic.read_evidence = read_evidence
            diagnostic.has_readable_body = bool(body)
            if body:
                counts.body_extracted += 1
                diagnostic.state = "OBSERVED_UNVERIFIED"
                diagnostic.reason = (
                    "ROW_PROVENANCE_UNRESOLVED"
                    if error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS"
                    else error.diagnostic_reason or error.reason
                )
                if error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS":
                    counts.coverage_complete = False
                    counts.uncertain_rows += 1
                    counts.skipped_rows += 1
                    self._host._record_structure_reason("ROW_PROVENANCE_UNRESOLVED")
                display_observations.append(
                    DisplayObservation(
                        body,
                        viewport_y=box["y"],
                        read_evidence=read_evidence,
                    )
                )
            else:
                counts.body_unknown += 1
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                if error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS":
                    counts.coverage_complete = False
                    self._host._record_structure_reason("ROW_PROVENANCE_UNRESOLVED")
                if body_signals:
                    diagnostic.state = "PENDING_HYDRATION"
                    diagnostic.reason = "POST_BODY_PENDING"
                else:
                    diagnostic.state, diagnostic.reason = _body_progress_state(
                        read_evidence,
                        has_readable_body=False,
                    )
                    if diagnostic.state == "PENDING_HYDRATION":
                        diagnostic.state = "REJECTED"
                        diagnostic.reason = error.diagnostic_reason or error.reason
                pending_signals.extend(body_signals)
                display_observations.append(
                    DisplayObservation(
                        None,
                        "OBSERVATION_UNCERTAIN",
                        box["y"],
                        read_evidence=read_evidence,
                    )
                )
            self._host.last_scan_timings.read_row_ms += round(
                (time.perf_counter() - read_started) * 1000
            )
            await emit_row(diagnostic, display=display_observations[-1])
            return None
        if (
            error.diagnostic_reason != "CANDIDATE_WRAPPER_AMBIGUOUS"
            or not currently_visible
        ):
            self._host.last_scan_timings.read_row_ms += round(
                (time.perf_counter() - read_started) * 1000
            )
            await emit_row(diagnostic, display=display_observations[-1])
            return None
        return target, body_stable
