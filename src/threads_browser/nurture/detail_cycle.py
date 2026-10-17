"""One verified Detail read and safe return to the verified Home feed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..errors import UiReadError
from ..feed.models import PostTarget
from ..reader import ThreadsHomeReader
from .diagnostics import _detail_stop, _print_detail_body
from .state import DedupeLedger, NurtureRunState, WorkflowPhase

_DETAIL_READ_FAILURES = frozenset(
    {
        "DETAIL_RENDER_PENDING",
        "DETAIL_RENDER_UNAVAILABLE",
        "DETAIL_ROOT_UNVERIFIED",
        "DETAIL_ROOT_AMBIGUOUS",
        "DETAIL_BODY_UNVERIFIED",
        "DETAIL_PROVENANCE_UNVERIFIED",
    }
)


@dataclass(frozen=True, slots=True)
class DetailCycleOutcome:
    status: str
    detail_reads: int
    retry_pending: bool


class DetailBackCoordinator:
    """Own the fail-closed Detail → Back transition for the single-cycle API."""

    def __init__(
        self,
        *,
        page: Page,
        reader: ThreadsHomeReader,
        state: NurtureRunState,
        dedupe: DedupeLedger,
        console_output_policy: ConsoleOutputPolicy,
        detail: bool,
        map_playwright_error: Callable[..., UiReadError],
    ) -> None:
        self._page = page
        self._reader = reader
        self._state = state
        self._dedupe = dedupe
        self._console_output_policy = console_output_policy
        self._detail = detail
        self._map_playwright_error = map_playwright_error

    async def read_and_return(
        self,
        selected: PostTarget,
        *,
        selected_number: int,
        selected_decision: str,
    ) -> DetailCycleOutcome:
        self._state.transition(WorkflowPhase.SELECTING)
        if self._detail:
            if selected_decision == "RELEVANT":
                reason = (
                    "RELEVANT_PENDING"
                    if selected.fingerprint in self._dedupe.previously_seen
                    else "RELEVANT_NEW"
                )
                emit_console_diagnostic(
                    self._console_output_policy,
                    f"[SELECT] post={selected_number} decision=RELEVANT reason={reason}",
                )
            else:
                emit_console_diagnostic(
                    self._console_output_policy,
                    f"[SELECT] post={selected_number} "
                    "decision=VERIFIED_FALLBACK reason=NO_RELEVANT_MATCH",
                )

        self._state.transition(WorkflowPhase.DETAIL)
        self._reader.set_failure_context(self._state.phase.value, "DETAIL")
        retry_pending = False
        try:
            detail_target = await self._reader.read_detail(selected)
        except (UiReadError, PlaywrightError) as raw_error:
            error = (
                raw_error
                if isinstance(raw_error, UiReadError)
                else self._map_playwright_error(
                    self._page,
                    raw_error,
                    phase=self._state.phase.value,
                    operation="DETAIL",
                    console_output_policy=self._console_output_policy,
                )
            )
            if error.code not in _DETAIL_READ_FAILURES or not getattr(
                self._reader,
                "detail_route_verified",
                False,
            ):
                self._state.stop(error.code)
                if self._detail:
                    emit_console_diagnostic(
                        self._console_output_policy,
                        f"[DETAIL] post={selected_number} status=FAILED code={error.code}",
                    )
                    _detail_stop(error.code)
                raise error from None
            retry_pending = True
            if self._detail:
                emit_console_diagnostic(
                    self._console_output_policy,
                    f"[DETAIL] post={selected_number} status=UNAVAILABLE "
                    f"code={error.code} retry=PENDING",
                )

        if not retry_pending and self._detail:
            home_body = " ".join((selected.body or "").split())
            expanded_body = " ".join((detail_target.body or "").split())
            extent = (
                "EXPANDED"
                if len(expanded_body) > len(home_body)
                else "TRUNCATED"
                if detail_target.body_truncated
                else "FULL"
            )
            emit_console_diagnostic(
                self._console_output_policy,
                f"[DETAIL] post={selected_number} status=VERIFIED "
                f"provenance=VERIFIED extent={extent}",
            )
            _print_detail_body(
                "DETAIL",
                f"post={selected_number}",
                "VERIFIED",
                selected_decision,
                extent,
                detail_target.body,
            )
        if not retry_pending:
            emit_console_diagnostic(
                self._console_output_policy,
                "DETAIL_VERIFIED status=PROVENANCE_VERIFIED",
            )

        if self._detail:
            emit_console_diagnostic(
                self._console_output_policy,
                f"[BACK] post={selected_number} status=REQUESTED",
            )
        self._state.transition(WorkflowPhase.BACK)
        self._reader.set_failure_context(self._state.phase.value, "BACK")
        try:
            await self._reader.return_to_home()
        except (UiReadError, PlaywrightError) as raw_error:
            error = (
                raw_error
                if isinstance(raw_error, UiReadError)
                else self._map_playwright_error(
                    self._page,
                    raw_error,
                    phase=self._state.phase.value,
                    operation="BACK",
                    console_output_policy=self._console_output_policy,
                )
            )
            self._state.stop(error.code)
            if self._detail:
                anchor_status = (
                    "CHANGED" if error.code == "RETURN_CONTEXT_CHANGED" else "UNVERIFIED"
                )
                emit_console_diagnostic(
                    self._console_output_policy,
                    f"[BACK] post={selected_number} status=FAILED "
                    f"anchor={anchor_status} code={error.code}",
                )
            raise error from None
        if self._detail:
            emit_console_diagnostic(
                self._console_output_policy,
                f"[BACK] post={selected_number} status=RESTORED "
                "anchor=IDENTITY_VERIFIED",
            )

        if retry_pending:
            return DetailCycleOutcome("DETAIL_RETRY_PENDING", 0, True)
        self._dedupe.pending.remove(selected.fingerprint)
        self._dedupe.pending_set.remove(selected.fingerprint)
        return DetailCycleOutcome("COMPLETE", 1, False)
