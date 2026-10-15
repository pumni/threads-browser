"""Shared typed failures for browser UI verification."""

from __future__ import annotations


class UiReadError(Exception):
    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
