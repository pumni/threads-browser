"""Private persisted value objects and fixed error codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from .schema import _ERROR_CODES


class BrowserDataError(Exception):
    code: str

    def __init__(self, code: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("unsupported browser data error")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class BrowserAccount:
    account_id: UUID
    alias: str
    expected_actor: str | None = field(repr=False)
    profile_directory: Path = field(repr=False)

    def __repr__(self) -> str:
        return "BrowserAccount(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class Checkpoint:
    account_id: UUID
    policy_fingerprint: str
    completed_runs: int = 0
    home_sampled: int = 0
    home_new: int = 0
    home_relevant: int = 0
    home_irrelevant: int = 0
    home_unknown: int = 0
    duplicates: int = 0
    detail_reads: int = 0
    scroll_attempts: int = 0
    scrolls: int = 0
    pending_read_fingerprints: tuple[str, ...] = field(default=(), repr=False)
    seen_fingerprints: tuple[str, ...] = field(default=(), repr=False)
    schema_version: int = 3

    def __repr__(self) -> str:
        return (
            "Checkpoint("
            f"completed_runs={self.completed_runs}, home_sampled={self.home_sampled}, "
            f"detail_reads={self.detail_reads}, seen_count={len(self.seen_fingerprints)})"
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "account_id": str(self.account_id),
            "policy_fingerprint": self.policy_fingerprint,
            "completed_runs": self.completed_runs,
            "home_sampled": self.home_sampled,
            "home_new": self.home_new,
            "home_relevant": self.home_relevant,
            "home_irrelevant": self.home_irrelevant,
            "home_unknown": self.home_unknown,
            "duplicates": self.duplicates,
            "detail_reads": self.detail_reads,
            "scroll_attempts": self.scroll_attempts,
            "scrolls": self.scrolls,
            "pending_read_fingerprints": list(self.pending_read_fingerprints),
            "seen_fingerprints": list(self.seen_fingerprints),
        }
