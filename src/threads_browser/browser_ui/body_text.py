"""Visible body text progress and hydration evidence for feed rows."""

from __future__ import annotations

from playwright.async_api import JSHandle, Locator, Page

from .body_progress import (
    BodyProgressTracker,
    _body_progress_state,  # noqa: F401 - retained module compatibility name
    _BodyTextProgress,
    _canonicalize_body,  # noqa: F401 - retained module compatibility name
    _canonicalize_body_block,  # noqa: F401 - retained module compatibility name
    _TextFragment,
    _VisibleBodyRead,
)
from .body_snapshot import capture_body_snapshot
from .body_visibility import BodyVisibilityCoverage
from .feed_rows import _FeedScanCounts

_MAX_BODY_CODEPOINTS = 10_000
_MAX_BODY_TEXT_NODES = 256
_MAX_BODY_RANGE_CHECKS = 40_000
_MAX_BODY_RANGE_DEPTH = 32

class BodyTextReader:
    """Track visible text fragments without retaining data outside the run."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._progress = BodyProgressTracker()
        self._visibility = BodyVisibilityCoverage()
        self.last_failure_substep: str | None = None

    @property
    def _body_text_progress(self) -> dict[str, dict[str, _BodyTextProgress]]:
        return self._progress._body_text_progress

    @property
    def _body_progress_salt(self) -> bytes:
        return self._progress._body_progress_salt

    @classmethod
    def _fragment_text(
        cls,
        fragments: list[_TextFragment],
        start: int,
        end: int,
    ) -> str | None:
        return BodyProgressTracker._fragment_text(fragments, start, end)

    @staticmethod
    def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return BodyProgressTracker._merge_ranges(ranges)

    async def _read_visible_body_progress(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        fingerprint: str | None = None,
        scan_counts: _FeedScanCounts,
        isolated_scope: bool = False,
    ) -> _VisibleBodyRead:
        self.last_failure_substep = "BODY_RANGE_EVALUATE"
        observation = await capture_body_snapshot(
            root,
            isolated_scope=isolated_scope,
            max_chars=_MAX_BODY_CODEPOINTS,
            max_nodes=_MAX_BODY_TEXT_NODES,
            max_range_checks=_MAX_BODY_RANGE_CHECKS,
            max_range_depth=_MAX_BODY_RANGE_DEPTH,
        )
        self.last_failure_substep = None

        return self._progress.consume(
            observation,
            fingerprint=fingerprint,
            scan_counts=scan_counts,
        )

    @staticmethod
    def _subtract_ranges(
        start: int,
        end: int,
        covered: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        return BodyProgressTracker._subtract_ranges(start, end, covered)

    @staticmethod
    def _utf16_slice(value: str, start: int, end: int) -> str:
        return BodyProgressTracker._utf16_slice(value, start, end)

    async def _visible_body_candidate_state(
        self,
        root: Locator,
        *,
        fingerprint: str | None = None,
    ) -> tuple[bool, bool]:
        return await self._visibility.inspect(root, fingerprint=fingerprint)
