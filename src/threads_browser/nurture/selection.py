"""Fail-closed selection of currently actionable, verified Home targets."""

from __future__ import annotations

from collections.abc import Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..errors import UiReadError
from ..feed.models import PostTarget
from ..reader import ThreadsHomeReader
from .state import CoverageLedger, DedupeLedger, NurtureRunState, WorkflowStats


class VerifiedTargetSelector:
    """Select only a target revalidated against the current Home viewport."""

    def __init__(
        self,
        *,
        page: Page,
        reader: ThreadsHomeReader,
        state: NurtureRunState,
        stats: WorkflowStats,
        dedupe: DedupeLedger,
        coverage: CoverageLedger,
        console_output_policy: ConsoleOutputPolicy,
        detail: bool,
        map_playwright_error: Callable[..., UiReadError],
    ) -> None:
        self._page = page
        self._reader = reader
        self._state = state
        self._stats = stats
        self._dedupe = dedupe
        self._coverage = coverage
        self._console_output_policy = console_output_policy
        self._detail = detail
        self._map_playwright_error = map_playwright_error
        self.rejected_codes: set[str] = set()

    async def first_actionable(self, targets: list[PostTarget]) -> PostTarget | None:
        for target in targets:
            if target.fingerprint in self._dedupe.detail_attempted_this_session:
                continue
            try:
                current = await self._reader.actionable_now(target)
            except (UiReadError, PlaywrightError) as raw_error:
                error = (
                    raw_error
                    if isinstance(raw_error, UiReadError)
                    else self._map_playwright_error(
                        self._page,
                        raw_error,
                        phase=self._state.phase.value,
                        operation="SCAN_ROW",
                        console_output_policy=self._console_output_policy,
                    )
                )
                if error.code not in {
                    "DETAIL_NAVIGATION_UNVERIFIED",
                    "DETAIL_PROVENANCE_UNVERIFIED",
                    "POST_AUTHOR_UNVERIFIED",
                    "POST_PROVENANCE_UNVERIFIED",
                }:
                    raise error from None
                self.rejected_codes.add(error.code)
                if self._detail:
                    emit_console_diagnostic(
                        self._console_output_policy,
                        f"[ACTIONABILITY] status=REJECTED code={error.code}",
                    )
                continue
            if self._detail:
                emit_console_diagnostic(
                    self._console_output_policy,
                    "[ACTIONABILITY] status=CONFIRMED",
                )
            return current
        return None

    async def select_fallback(self, targets: list[PostTarget]) -> PostTarget | None:
        if self._stats.found_relevant or self._stats.unverified_relevant or not targets:
            return None
        pending_order = {
            fingerprint: index for index, fingerprint in enumerate(self._dedupe.pending)
        }
        pending_targets = [
            target
            for target in targets
            if target.fingerprint in self._dedupe.pending_set
            and target.fingerprint not in self._dedupe.detail_attempted_this_session
        ]
        if self._dedupe.pending_set:
            ordered_pending = sorted(
                pending_targets,
                key=lambda target: pending_order[target.fingerprint],
            )
            selected = await self.first_actionable(ordered_pending)
            if selected is not None:
                return selected
            # Incomplete coverage keeps existing pending entries untouched.
            if self._coverage.has_skips or not self._stats.viewport_coverage_complete:
                return None
        ordered = [
            target
            for target in targets
            if target.fingerprint not in self._dedupe.checkpoint_seen
            and target.fingerprint not in self._dedupe.pending_set
            and target.fingerprint not in self._dedupe.detail_attempted_this_session
        ]
        if not ordered:
            return None
        selected = await self.first_actionable(ordered)
        if selected is None:
            return None
        if selected.fingerprint not in self._dedupe.pending_set:
            # Persist pending provenance before Detail so a safe retry can resume it.
            self._dedupe.pending.append(selected.fingerprint)
            self._dedupe.pending_set.add(selected.fingerprint)
        return selected
