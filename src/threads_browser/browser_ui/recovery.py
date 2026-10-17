"""Bounded retry and fail-closed recovery policy for reader UI stages."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..errors import UiReadError
from ..feed.urls import _same_home_origin
from .session import (
    classify_playwright_error_kind,
    classify_playwright_failure,
    emit_failure_diagnostic,
)
from .viewport_scan_models import _ScanFailureSite

_ReadinessResult = TypeVar("_ReadinessResult")


@dataclass(slots=True)
class ReaderFailureContext:
    phase: str = "INITIALIZING"
    operation: str = "SETTLE"


class ReaderRecoveryHost(Protocol):
    _page: Page
    _readiness_prompt: Callable[[str, int], Awaitable[bool]] | None
    _readiness_timeout_ms: int
    _automatic_readiness_windows: int
    _console_output_policy: ConsoleOutputPolicy
    _scan_viewport_index: int | None
    last_scan_retry_count: int
    _scan_failure_site: _ScanFailureSite | None
    _scan_stage: str | None
    _scan_operation: str
    _scan_candidate_index: int | None
    _body_text: object
    _viewport_scan: object

    @property
    def _failure_phase(self) -> str: ...
    @_failure_phase.setter
    def _failure_phase(self, value: str) -> None: ...
    @property
    def _failure_operation(self) -> str: ...
    @_failure_operation.setter
    def _failure_operation(self, value: str) -> None: ...

    def _readiness_deadline(self, timeout_ms: int) -> float: ...
    async def _revalidate_scan_failure(self) -> str: ...
    async def _dispose_active_scan_candidate_handles(self) -> None: ...


class ReaderRecovery:
    """Coordinate bounded readiness prompts, DOM retry, and failure diagnostics."""

    def __init__(self, host: ReaderRecoveryHost) -> None:
        self._host = host
        self.state = ReaderFailureContext()

    def set_context(self, phase: str, operation: str) -> None:
        self.state.phase = phase
        self.state.operation = operation

    async def run_stage(
        self,
        stage: str,
        timeout_code: str,
        attempt: Callable[[float], Awaitable[_ReadinessResult]],
        *,
        recoverable_codes: tuple[str, ...] | None = None,
        operation: str | None = None,
        cancel_code: str | None = None,
    ) -> _ReadinessResult:
        allowed_codes = recoverable_codes or (timeout_code,)
        window = 0
        final_code = timeout_code
        dom_change_retried = False
        detail_ui_retried = False
        failure_operation = operation or self._host._failure_operation
        while True:
            deadline = self._host._readiness_deadline(self._host._readiness_timeout_ms)
            try:
                result = await attempt(deadline)
            except PlaywrightError as error:
                failure = classify_playwright_failure(self._host._page, error)
                if failure_operation == "SCAN_ROW":
                    try:
                        row_identity = await self._host._revalidate_scan_failure()
                    except asyncio.CancelledError:
                        await self._host._dispose_active_scan_candidate_handles()
                        self._host._scan_failure_site = None
                        self._host._scan_stage = None
                        self._host._viewport_scan.clear_scan_state()
                        raise
                    failure_site = self._host._scan_failure_site
                    operation_name = self._host._scan_operation
                    if operation_name not in {
                        "DISCOVER_ROOTS",
                        "STRUCTURE",
                        "BOUNDING_BOX",
                        "READ_CARD",
                        "READ_BODY",
                        "STABILITY_CHECK",
                        "EMIT_ROW",
                        "FINALIZE",
                    }:
                        operation_name = "SCAN_ROW"
                    emit_failure_diagnostic(
                        phase=self._host._failure_phase,
                        operation=operation_name,
                        error_class=failure.error_class,
                        page_closed=failure.page_closed,
                        context_closed=failure.context_closed,
                        console_output_policy=self._host._console_output_policy,
                        stage=self._host._scan_stage,
                        viewport_index=self._host._scan_viewport_index,
                        candidate_index=self._host._scan_candidate_index,
                        error_kind=classify_playwright_error_kind(error),
                        row_identity=(
                            row_identity if row_identity in {"REUSED", "CHANGED"} else "UNKNOWN"
                        ),
                        body_substep=(
                            self._host._body_text.last_failure_substep
                            if operation_name == "READ_BODY"
                            else None
                        ),
                        identity_source=(
                            "NONE" if failure_site is None else failure_site.identity_source
                        ),
                        retry_eligibility=(
                            row_identity
                            if row_identity in {"REUSED", "CHANGED"}
                            else "INSUFFICIENT"
                        ),
                    )
                    if failure.error_class == "TARGET_CLOSED" or row_identity == "CLOSED":
                        await self._host._dispose_active_scan_candidate_handles()
                        self._host._scan_failure_site = None
                        self._host._scan_stage = None
                        self._host._viewport_scan.clear_scan_state()
                        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
                    if row_identity == "REUSED" and self._host.last_scan_retry_count < 1:
                        await self._host._dispose_active_scan_candidate_handles()
                        self._host._scan_failure_site = None
                        self._host._viewport_scan.clear_scan_state()
                        self._host.last_scan_retry_count += 1
                        window += 1
                        emit_console_diagnostic(
                            self._host._console_output_policy,
                            f"WAITING stage={stage} retry={window}",
                        )
                        continue
                    await self._host._dispose_active_scan_candidate_handles()
                    self._host._scan_failure_site = None
                    self._host._scan_stage = None
                    self._host._viewport_scan.clear_scan_state()
                    raise UiReadError("SCAN_ROW_UNVERIFIED") from None
                emit_failure_diagnostic(
                    phase=self._host._failure_phase,
                    operation=failure_operation,
                    error_class=failure.error_class,
                    page_closed=failure.page_closed,
                    context_closed=failure.context_closed,
                    console_output_policy=self._host._console_output_policy,
                    error_kind=classify_playwright_error_kind(error),
                )
                if failure.error_class == "TARGET_CLOSED":
                    raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
                if failure.error_class == "DOM_DETACHED" and failure_operation in {
                    "SCAN_ROW",
                    "SETTLE",
                    "DETAIL",
                }:
                    if failure_operation == "DETAIL" and detail_ui_retried:
                        raise UiReadError("DETAIL_ROOT_UNVERIFIED") from None
                    if failure_operation != "DETAIL" and dom_change_retried:
                        raise UiReadError("DOM_CHANGED_DURING_SCAN") from None
                    if failure_operation == "DETAIL":
                        detail_ui_retried = True
                    else:
                        dom_change_retried = True
                    window += 1
                    emit_console_diagnostic(
                        self._host._console_output_policy, f"WAITING stage={stage} retry={window}"
                    )
                    continue
                if (
                    failure_operation == "DETAIL"
                    and not detail_ui_retried
                    and _same_home_origin(self._host._page.url)
                ):
                    detail_ui_retried = True
                    window += 1
                    emit_console_diagnostic(
                        self._host._console_output_policy, f"WAITING stage={stage} retry={window}"
                    )
                    continue
                raise UiReadError("UI_READ_UNVERIFIED") from None
            except UiReadError as error:
                if failure_operation == "SCAN_ROW":
                    await self._host._dispose_active_scan_candidate_handles()
                    self._host._scan_failure_site = None
                    self._host._scan_stage = None
                    self._host._viewport_scan.clear_scan_state()
                if error.code not in allowed_codes:
                    raise
                final_code = error.code
            else:
                if window:
                    emit_console_diagnostic(
                        self._host._console_output_policy, f"RECOVERED stage={stage} retry={window}"
                    )
                return result

            window += 1
            emit_console_diagnostic(
                self._host._console_output_policy, f"WAITING stage={stage} retry={window}"
            )
            if window < self._host._automatic_readiness_windows:
                continue
            if self._host._readiness_prompt is None:
                raise UiReadError(final_code) from None
            if not await self._host._readiness_prompt(stage, window):
                raise UiReadError(cancel_code or final_code) from None
