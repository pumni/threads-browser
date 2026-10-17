"""Explicit console-output modes for browser workflows."""

from __future__ import annotations

from enum import Enum


class ConsoleOutputPolicy(Enum):
    INTERACTIVE = "interactive"
    SCHEDULED = "scheduled"


def emit_console_diagnostic(
    policy: ConsoleOutputPolicy,
    *values: object,
    **options: object,
) -> None:
    """Emit scan diagnostics only for operator-attended console runs."""
    if policy is ConsoleOutputPolicy.INTERACTIVE:
        print(*values, **options)
