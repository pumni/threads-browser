"""Reader timing and selector limits shared by its UI collaborators."""

from __future__ import annotations

import re

_NAVIGATION_TIMEOUT_MS = 20_000
_DETAIL_TIMEOUT_MS = 15_000
_READINESS_TIMEOUT_MS = 8_000
_AUTOMATIC_READINESS_WINDOWS = 2
_READINESS_RECHECK_INTERVAL_MS = 200
_CARD_HYDRATION_WAIT_MS = 800
_BODY_STABILITY_INTERVAL_MS = 180
_RENDER_MIN_DWELL_MS = 600
_RENDER_SETTLE_INTERVAL_MS = 300
_RENDER_STABLE_OBSERVATIONS = 2
_UNSUPPORTED_CONFIRMATION_SCANS = 3
_PROFILE_NAME = re.compile(r"^(?:profile|your profile|account)$", re.IGNORECASE)
_MORE_NAME = re.compile(r"^(?:more|see more|read more)$", re.IGNORECASE)
