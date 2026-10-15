"""Bounded read-only Home / For You workflow."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .reader import (
    ThreadsHomeReader,
    UiReadError,
    canonical_terms,
    classify_body,
)
from .storage import (
    BrowserAccount,
    BrowserDataError,
    Checkpoint,
    CheckpointStore,
    ensure_safe_browser_profile,
)

_MAX_SEEN = 5_000


@dataclass(frozen=True, slots=True)
class NurtureResult:
    status: str
    home_sampled: int
    home_new: int
    home_relevant: int
    home_irrelevant: int
    home_unknown: int
    duplicates: int
    detail_reads: int
    scroll_attempts: int
    scrolls: int
    checkpoint_runs: int
    home_relevant_ratio: float | None
    skipped_unverified: int = 0
    ui_status: str = "UNVERIFIED_LIVE_UI"


def policy_fingerprint(terms: tuple[str, ...]) -> str:
    payload = json.dumps(tuple(sorted(terms)), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


async def run_page_workflow(
    page: Page,
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int = 8,
    max_total_posts: int = 40,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
) -> NurtureResult:
    if not 1 <= max_posts <= 20:
        raise UiReadError("POST_LIMIT_INVALID")
    if not 0 <= max_scrolls <= 20:
        raise UiReadError("SCROLL_LIMIT_INVALID")
    if not 1 <= max_total_posts <= 400:
        raise UiReadError("TOTAL_SCAN_LIMIT_INVALID")
    normalized_terms = canonical_terms(terms)
    fingerprint = policy_fingerprint(normalized_terms)
    checkpoint = checkpoint_store.load(account.account_id, fingerprint)
    reader = ThreadsHomeReader(
        page,
        account.expected_actor,
        readiness_prompt=readiness_prompt,
    )
    await reader.open_home()
    previously_seen = set(checkpoint.seen_fingerprints)
    seen_this_run: set[str] = set()
    new_fingerprints: set[str] = set()
    duplicates = 0
    relevant = 0
    irrelevant = 0
    unknown = 0
    selected = None
    pending = list(checkpoint.pending_read_fingerprints)
    pending_set = set(pending)
    found_relevant = False
    viewport_number = 0
    exhaustion_reason = "MAX_SCROLLS"
    while True:
        remaining = max_total_posts - len(seen_this_run)
        if remaining <= 0:
            exhaustion_reason = "MAX_TOTAL_POSTS"
            break
        viewport_number += 1
        duplicate_count_before_scan = reader.feed_duplicates
        targets = await reader.scan_viewport(
            min(max_posts, remaining),
            scanned_fingerprints=seen_this_run,
        )
        duplicates += reader.feed_duplicates - duplicate_count_before_scan
        viewport_verified = 0
        viewport_relevant = 0
        viewport_selected = None
        for target in targets:
            if target.fingerprint in seen_this_run:
                continue
            seen_this_run.add(target.fingerprint)
            viewport_verified += 1
            is_new = target.fingerprint not in previously_seen
            if is_new:
                new_fingerprints.add(target.fingerprint)
            else:
                duplicates += 1
            decision = classify_body(target.body, normalized_terms)
            if decision == "RELEVANT":
                relevant += 1
                viewport_relevant += 1
                found_relevant = True
                if is_new and target.fingerprint not in pending_set:
                    pending.append(target.fingerprint)
                    pending_set.add(target.fingerprint)
                if (
                    viewport_selected is None
                    and target.fingerprint in pending_set
                ):
                    viewport_selected = target
            elif decision == "IRRELEVANT":
                irrelevant += 1
            else:
                unknown += 1
        print(
            f"SCANNING viewport={viewport_number} verified={viewport_verified} "
            f"relevant={viewport_relevant} skipped={reader.skipped_unverified}"
        )
        if viewport_selected is not None:
            selected = viewport_selected
            break
        if len(seen_this_run) >= max_total_posts:
            exhaustion_reason = "MAX_TOTAL_POSTS"
            break
        if reader.scroll_attempts >= max_scrolls:
            exhaustion_reason = "MAX_SCROLLS"
            break
        await reader.advance_feed()

    detail_reads = 0
    if selected is None and not found_relevant:
        print(
            f"SEARCH_BUDGET_EXHAUSTED reason={exhaustion_reason} "
            f"viewports={viewport_number}"
        )
    if selected is not None:
        await reader.read_detail(selected)
        await reader.return_to_home()
        detail_reads = 1
        pending.remove(selected.fingerprint)
        pending_set.remove(selected.fingerprint)
        status = "COMPLETE"
    elif len(seen_this_run) == 0:
        return NurtureResult(
            status="NO_VERIFIED_POSTS_IN_BUDGET",
            home_sampled=0,
            home_new=0,
            home_relevant=0,
            home_irrelevant=0,
            home_unknown=0,
            duplicates=duplicates,
            detail_reads=0,
            scroll_attempts=reader.scroll_attempts,
            scrolls=reader.scrolls,
            checkpoint_runs=checkpoint.completed_runs,
            home_relevant_ratio=None,
            skipped_unverified=reader.skipped_unverified,
        )
    elif pending_set:
        status = "RELEVANT_POST_NOT_CURRENT"
    elif found_relevant or relevant:
        status = "NO_NEW_RELEVANT_POST"
    else:
        status = "NO_RELEVANT_POST_IN_BUDGET"

    if len(pending) > _MAX_SEEN:
        raise BrowserDataError("STATE_INVALID")
    seen = list(dict.fromkeys((*checkpoint.seen_fingerprints, *seen_this_run)))
    if len(seen) > _MAX_SEEN:
        retained_pending = [value for value in seen if value in pending_set]
        recent = [value for value in seen if value not in pending_set]
        available = _MAX_SEEN - len(retained_pending)
        seen = retained_pending + (recent[-available:] if available else [])
    updated = Checkpoint(
        account.account_id,
        fingerprint,
        completed_runs=checkpoint.completed_runs + 1,
        home_sampled=checkpoint.home_sampled + len(seen_this_run),
        home_new=checkpoint.home_new + len(new_fingerprints),
        home_relevant=checkpoint.home_relevant + relevant,
        home_irrelevant=checkpoint.home_irrelevant + irrelevant,
        home_unknown=checkpoint.home_unknown + unknown,
        duplicates=checkpoint.duplicates + duplicates,
        detail_reads=checkpoint.detail_reads + detail_reads,
        scroll_attempts=checkpoint.scroll_attempts + reader.scroll_attempts,
        scrolls=checkpoint.scrolls + reader.scrolls,
        pending_read_fingerprints=tuple(pending),
        seen_fingerprints=tuple(seen),
    )
    checkpoint_store.save(updated)
    ratio = None if not seen_this_run else relevant / len(seen_this_run)
    return NurtureResult(
        status=status,
        home_sampled=len(seen_this_run),
        home_new=len(new_fingerprints),
        home_relevant=relevant,
        home_irrelevant=irrelevant,
        home_unknown=unknown,
        duplicates=duplicates,
        detail_reads=detail_reads,
        scroll_attempts=reader.scroll_attempts,
        scrolls=reader.scrolls,
        checkpoint_runs=updated.completed_runs,
        home_relevant_ratio=ratio,
        skipped_unverified=reader.skipped_unverified,
    )


async def run_browser_nurture(
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int = 8,
    max_total_posts: int = 40,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
) -> NurtureResult:
    ensure_safe_browser_profile(account.profile_directory)
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(account.profile_directory),
                headless=False,
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                return await run_page_workflow(
                    page,
                    account,
                    checkpoint_store,
                    max_posts=max_posts,
                    terms=terms,
                    max_scrolls=max_scrolls,
                    max_total_posts=max_total_posts,
                    readiness_prompt=readiness_prompt,
                )
            finally:
                await context.close()
    except (UiReadError, BrowserDataError):
        raise
    except PlaywrightError:
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
    except (OSError, RuntimeError, ValueError):
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
