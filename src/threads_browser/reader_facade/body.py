"""Body compatibility methods retained by the reader facade."""

from __future__ import annotations

from hashlib import sha256

from playwright.async_api import (
    JSHandle,
    Locator,
)

from ..browser_ui.body_text import (
    _MAX_BODY_CODEPOINTS,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body_block,  # noqa: F401 - retained reader-module compatibility name
    _VisibleBodyRead,
)
from ..browser_ui.feed_rows import (
    _BODY_TEXT_SELECTOR,
    _MAX_CARD_LINKS,  # noqa: F401 - retained reader-module compatibility name
    _MAX_STRUCTURE_ELEMENTS,
)
from ..browser_ui.loading import (
    CardNotReady as _CardNotReady,
)
from ..browser_ui.loading import (
    CardWaitSignal as _CardWaitSignal,
)
from ..feed.models import (
    BodyReadEvidence,
    PostTarget,
)
from ..feed.urls import (
    _rejected_post_link_blocks_candidate,  # noqa: F401 - retained reader-module compatibility name
)


class ReaderBodyCompatibility:
    """Compatibility delegates grouped by body responsibility."""

    async def _extract_card_body(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        require_body: bool,
        isolated_scope: bool = False,
    ) -> tuple[str | None, bool, bool, tuple[_CardWaitSignal, ...]]:
        return await self._card_provenance.extract_card_body(
            root,
            root_handle,
            require_body=require_body,
            isolated_scope=isolated_scope,
        )

    async def _observe_display_body(
        self,
        root: Locator,
    ) -> tuple[str | None, tuple[_CardWaitSignal, ...], BodyReadEvidence]:
        root_handle = await root.element_handle()
        if root_handle is None:
            return None, (), BodyReadEvidence(state="NO_BODY_NODES")
        observed = await self._read_visible_body_progress(root, root_handle)
        if observed.has_rendered_text:
            return observed.body, (), observed.read_evidence
        candidates = root.locator(_BODY_TEXT_SELECTOR)
        signals = tuple(
            _CardWaitSignal(candidates.nth(index), "nonempty_text")
            for index in range(min(await candidates.count(), _MAX_STRUCTURE_ELEMENTS))
            if await self._safe_body_candidate(candidates.nth(index), root, root_handle)
        )
        return None, signals, observed.read_evidence

    async def _read_visible_body_progress(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        fingerprint: str | None = None,
        isolated_scope: bool = False,
    ) -> _VisibleBodyRead:
        return await self._body_text._read_visible_body_progress(
            root,
            root_handle,
            fingerprint=fingerprint,
            scan_counts=self.last_scan_counts,
            isolated_scope=isolated_scope,
        )

    async def _read_detail_body(
        self,
        root: Locator,
        fingerprint: str,
    ) -> _VisibleBodyRead:
        root_handle = await root.element_handle()
        if root_handle is None:
            raise _CardNotReady(root, reason_code="POST_ROOT_DOM_IDENTITY_CHANGED")
        detail_fingerprint = sha256(f"detail:{fingerprint}".encode()).hexdigest()
        return await self._read_visible_body_progress(
            root,
            root_handle,
            fingerprint=detail_fingerprint,
        )

    async def _read_card(
        self,
        root: Locator,
        *,
        currently_visible: bool = False,
        viewport_y: float = 0.0,
        require_body: bool = False,
        cached_body_fingerprints: set[str] | None = None,
        visible_text_progress: bool = False,
        expected_root_handle: JSHandle | None = None,
        isolated_scope: bool = False,
    ) -> PostTarget:
        return await self._card_provenance.read_card(
            root,
            currently_visible=currently_visible,
            viewport_y=viewport_y,
            require_body=require_body,
            cached_body_fingerprints=cached_body_fingerprints,
            visible_text_progress=visible_text_progress,
            expected_root_handle=expected_root_handle,
            isolated_scope=isolated_scope,
            read_visible_body_progress=self._read_visible_body_progress,
            scan_operation=(self._set_scan_operation if self._scan_stage is not None else None),
        )

    async def _wait_for_stable_card_dom(self, root: Locator) -> bool:
        return await self._card_provenance.wait_for_stable_card_dom(
            root,
            body_stability_interval_ms=self._body_stability_interval_ms,
            card_hydration_wait_ms=self._card_hydration_wait_ms,
        )

    async def _read_stable_visible_card(
        self,
        root: Locator,
        *,
        viewport_y: float,
        cached_body_fingerprints: set[str] | None,
        expected_root_handle: JSHandle | None = None,
        allow_hydration_retry: bool = False,
        isolated_scope: bool = False,
    ) -> tuple[PostTarget, bool]:
        return await self._card_provenance.read_stable_visible_card(
            root,
            viewport_y=viewport_y,
            cached_body_fingerprints=cached_body_fingerprints,
            expected_root_handle=expected_root_handle,
            allow_hydration_retry=allow_hydration_retry,
            isolated_scope=isolated_scope,
            read_card=self._read_card,
            wait_for_stable_dom=self._wait_for_stable_card_dom,
            scan_operation=(self._set_scan_operation if self._scan_stage is not None else None),
            body_stability_interval_ms=self._body_stability_interval_ms,
            card_hydration_wait_ms=self._card_hydration_wait_ms,
        )

    async def _visible_body_candidate_state(
        self,
        root: Locator,
        *,
        fingerprint: str | None = None,
    ) -> tuple[bool, bool]:
        return await self._card_provenance.visible_body_candidate_state(
            root, fingerprint=fingerprint
        )
