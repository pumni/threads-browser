"""Bounded read-only Home / For You workflow."""

from __future__ import annotations

import json
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
) -> NurtureResult:
    if not 1 <= max_posts <= 20:
        raise UiReadError("POST_LIMIT_INVALID")
    normalized_terms = canonical_terms(terms)
    fingerprint = policy_fingerprint(normalized_terms)
    checkpoint = checkpoint_store.load(account.account_id, fingerprint)
    reader = ThreadsHomeReader(page, account.expected_actor)
    await reader.open_home()
    targets = await reader.sample_and_scroll(max_posts)

    previously_seen = set(checkpoint.seen_fingerprints)
    seen_this_run: set[str] = set()
    new_targets = []
    duplicates = reader.feed_duplicates
    relevant = 0
    irrelevant = 0
    unknown = 0
    selected = None
    new_relevant = False
    for target in targets:
        is_new = target.fingerprint not in previously_seen and target.fingerprint not in seen_this_run
        if is_new:
            seen_this_run.add(target.fingerprint)
            new_targets.append(target)
        else:
            duplicates += 1
        decision = classify_body(target.body, normalized_terms)
        if decision == "RELEVANT":
            relevant += 1
            if is_new:
                new_relevant = True
            if is_new and target.visible_after_scroll and selected is None:
                selected = target
        elif decision == "IRRELEVANT":
            irrelevant += 1
        else:
            unknown += 1

    detail_reads = 0
    if selected is not None:
        await reader.read_detail(selected)
        await reader.return_to_home()
        detail_reads = 1
        status = "COMPLETE"
    elif new_relevant:
        status = "RELEVANT_POST_NOT_CURRENT"
    elif relevant:
        status = "NO_NEW_RELEVANT_POST"
    else:
        status = "NO_RELEVANT_POST"

    seen = (*checkpoint.seen_fingerprints, *seen_this_run)
    if len(seen) > _MAX_SEEN:
        seen = seen[-_MAX_SEEN:]
    updated = Checkpoint(
        account.account_id,
        fingerprint,
        completed_runs=checkpoint.completed_runs + 1,
        home_sampled=checkpoint.home_sampled + len(targets),
        home_new=checkpoint.home_new + len(new_targets),
        home_relevant=checkpoint.home_relevant + relevant,
        home_irrelevant=checkpoint.home_irrelevant + irrelevant,
        home_unknown=checkpoint.home_unknown + unknown,
        duplicates=checkpoint.duplicates + duplicates,
        detail_reads=checkpoint.detail_reads + detail_reads,
        scroll_attempts=checkpoint.scroll_attempts + reader.scroll_attempts,
        scrolls=checkpoint.scrolls + reader.scrolls,
        seen_fingerprints=tuple(seen),
    )
    checkpoint_store.save(updated)
    ratio = None if not targets else relevant / len(targets)
    return NurtureResult(
        status=status,
        home_sampled=len(targets),
        home_new=len(new_targets),
        home_relevant=relevant,
        home_irrelevant=irrelevant,
        home_unknown=unknown,
        duplicates=duplicates,
        detail_reads=detail_reads,
        scroll_attempts=reader.scroll_attempts,
        scrolls=reader.scrolls,
        checkpoint_runs=updated.completed_runs,
        home_relevant_ratio=ratio,
    )


async def run_browser_nurture(
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
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
                )
            finally:
                await context.close()
    except (UiReadError, BrowserDataError):
        raise
    except PlaywrightError:
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
    except (OSError, RuntimeError, ValueError):
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
