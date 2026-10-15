"""Value objects for post and viewport observations; no navigation side effects."""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import Locator


@dataclass(frozen=True, slots=True, repr=False)
class PostTarget:
    url: str = field(repr=False)
    author: str = field(repr=False)
    body: str | None = field(repr=False)
    fingerprint: str
    permalink: Locator = field(repr=False)
    body_available: bool = field(default=False, repr=False)
    currently_visible: bool = field(default=False, repr=False)
    viewport_y: float = field(default=0.0, repr=False)
    body_truncated: bool = field(default=False, repr=False)

    def __repr__(self) -> str:
        return "PostTarget(<private>)"


@dataclass(frozen=True, slots=True)
class HomeSample:
    targets: tuple[PostTarget, ...]
    observed_targets: tuple[PostTarget, ...]
    skipped_unverified: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class DisplayObservation:
    """Locally read card text without verified post identity."""

    body: str | None = field(default=None, repr=False)
    status: str = "OBSERVED_UNVERIFIED"
    viewport_y: float = field(default=0.0, repr=False)

    def __repr__(self) -> str:
        return f"DisplayObservation(status={self.status!r}, body=<private>)"
