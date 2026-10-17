"""Turn DOM structure evidence into bounded candidate rows."""

from __future__ import annotations

import secrets
from typing import Protocol

from playwright.async_api import Locator

from .feed_rows import _CandidateDiagnostic, _CandidateStructure
from .viewport_scan_models import _TrackedRowIdentity, _ViewportScanState


class CandidateDiscoveryHost(Protocol):
    _scan_candidate_index: int | None

    def _set_scan_operation(self, operation: str) -> None: ...
    async def _is_carried_viewport_candidate(
        self, root: Locator
    ) -> _TrackedRowIdentity | None: ...
    def _record_structure_reason(self, reason: str) -> None: ...


class ViewportCandidateProcessor:
    """Classify row cues before any permalink or body read is attempted."""

    def __init__(self, host: CandidateDiscoveryHost) -> None:
        self._host = host

    async def collect(
        self,
        state: _ViewportScanState,
        roots: list[Locator],
        structures: dict[int, _CandidateStructure],
    ) -> None:
        for index, root in enumerate(roots):
            self._host._scan_candidate_index = index + 1
            self._host._set_scan_operation("STRUCTURE")
            structure = structures[index]
            diagnostic = _CandidateDiagnostic(
                row_index=index + 1,
                root_kind=structure.root_kind,
                has_visible_time=structure.visible_times > 0,
                has_post_permalink=structure.visible_post_links > 0,
                has_profile_author=structure.profile_anchor_candidates > 0,
                has_readable_body=structure.readable_body_candidates > 0,
            )
            state.diagnostics.append(diagnostic)
            has_row_cues = (structure.visible_times or structure.offscreen_times) and (
                structure.profile_anchor_candidates
                or structure.visible_post_links
                or structure.body_candidates
                or structure.offscreen_post_links
                or structure.offscreen_profile_anchors
                or structure.offscreen_body_candidates
            )
            semantic_evidence = (
                structure.body_candidates or structure.offscreen_body_candidates
            ) and (
                structure.visible_times
                or structure.offscreen_times
                or structure.profile_anchor_candidates
                or structure.visible_post_links
                or structure.offscreen_profile_anchors
                or structure.offscreen_post_links
            )
            semantic_row = (
                semantic_evidence
                and (
                    structure.root_kind != "PRESSABLE"
                    or structure.visible_times
                    or structure.visible_post_links
                )
                and structure.root_kind in {"ARTICLE", "ROLE_ARTICLE", "PRESSABLE"}
            )
            unresolved_post_root = bool(
                (structure.visible_post_links and not structure.canonical_post_links)
                or (structure.canonical_post_links and not structure.profile_anchor_candidates)
            )
            carried_identity = await self._host._is_carried_viewport_candidate(root)
            if carried_identity is not None:
                state.matched_previous_identity_handles.add(id(carried_identity.dom_handle))
                diagnostic.identity_fingerprint = carried_identity.fingerprint
                diagnostic.author_fingerprint = carried_identity.author_fingerprint
            if has_row_cues or semantic_row or carried_identity is not None:
                root_handle = await root.element_handle()
                if root_handle is not None:
                    state.candidate_dom_handles[index] = root_handle
                state.candidate_prior_fingerprints[index] = (
                    None if carried_identity is None else carried_identity.fingerprint
                )
                diagnostic.state = "PENDING"
                diagnostic.reason = "POST_ROW_REQUIRES_VERIFICATION"
                diagnostic.observation_id = (
                    secrets.token_hex(8)
                    if carried_identity is None
                    else carried_identity.observation_id
                )
                state.candidate_rows.append(
                    (
                        index,
                        root,
                        structure,
                        carried_identity,
                        diagnostic,
                        diagnostic.observation_id,
                    )
                )
            elif unresolved_post_root:
                diagnostic.state = "DEFERRED_IDENTITY"
                diagnostic.reason = "POST_ROOT_UNRESOLVED"
                diagnostic.observation_id = secrets.token_hex(8)
                self._host._record_structure_reason("POST_ROOT_UNRESOLVED")
