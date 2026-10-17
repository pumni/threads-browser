"""Persisted schema keys and validation bounds."""

from __future__ import annotations

import re

_DATA_ROOT_ENV = "THREADS_BROWSER_DATA_ROOT"
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ACTOR = re.compile(r"[A-Za-z0-9._]{1,30}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_ACCOUNT_KEYS = {"schema_version", "account_id", "alias", "expected_actor", "profile_directory"}
_CHECKPOINT_KEYS = {
    "schema_version", "account_id", "policy_fingerprint", "completed_runs",
    "home_sampled", "home_new", "home_relevant", "home_irrelevant", "home_unknown",
    "duplicates", "detail_reads", "scroll_attempts", "scrolls",
    "pending_read_fingerprints", "seen_fingerprints",
}
_MAX_DOCUMENT_BYTES = 256 * 1024
_MAX_COUNTER = 1_000_000_000_000
_MAX_SEEN = 5_000
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_OPERATION_STATUS_KEYS = {
    "schema_version", "started_at", "finished_at", "duration_seconds", "status",
    "cycle_status", "stop_reason", "exit_code", "checkpoint_runs", "counters",
}
_OPERATION_COUNTERS = {
    "home_sampled", "home_new", "detail_reads", "scroll_attempts", "scrolls",
    "skipped_unverified", "unique_verified_posts",
}
_ERROR_CODES = {
    "ACCOUNT_BUSY", "ACCOUNT_INVALID", "ACCOUNT_NOT_FOUND", "ACCOUNT_EXISTS",
    "ACCOUNT_NOT_VERIFIED", "TERMS_INVALID", "ACTOR_MISMATCH", "DATA_ROOT_INVALID",
    "DATA_ROOT_UNAVAILABLE", "PROFILE_UNAVAILABLE", "PROFILE_EXISTS", "LOCK_UNAVAILABLE",
    "STATE_INVALID", "STATE_PATH_INVALID",
}
