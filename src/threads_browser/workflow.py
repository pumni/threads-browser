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
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
) -> NurtureResult:
    if not 1 <= max_posts <= 20:
        raise UiReadError("POST_LIMIT_INVALID")
    normalized_terms = canonical_terms(terms)
    fingerprint = policy_fingerprint(normalized_terms)
    checkpoint = checkpoint_store.load(account.account_id, fingerprint)
    reader = ThreadsHomeReader(
        page,
        account.expected_actor,
        readiness_prompt=readiness_prompt,
    )
    await reader.open_home()
    sample = await reader.sample_and_scroll(max_posts)
    targets = sample.observed_targets

    previously_seen = set(checkpoint.seen_fingerprints)
    seen_this_run: set[str] = set()
    new_targets = []
    duplicates = reader.feed_duplicates
    relevant = 0
    irrelevant = 0
    unknown = 0
    decisions: dict[str, str] = {}
    selected = None
    new_relevant = False
    pending = list(checkpoint.pending_read_fingerprints)
    pending_set = set(pending)
    for target in targets:
        is_new = target.fingerprint not in previously_seen and target.fingerprint not in seen_this_run
        if is_new:
            seen_this_run.add(target.fingerprint)
            new_targets.append(target)
        else:
            duplicates += 1
        decision = classify_body(target.body, normalized_terms)
        decisions[target.fingerprint] = decision
        if decision == "RELEVANT":
            relevant += 1
            if is_new or target.fingerprint in pending_set:
                new_relevant = True
                if target.fingerprint not in pending_set:
                    pending.append(target.fingerprint)
                    pending_set.add(target.fingerprint)
        elif decision == "IRRELEVANT":
            irrelevant += 1
        else:
            unknown += 1

    for target in sample.targets:
        if (
            decisions[target.fingerprint] == "RELEVANT"
            and target.fingerprint in pending_set
        ):
            selected = target
            break

    detail_reads = 0
    if selected is not None:
        await reader.read_detail(selected)
        await reader.return_to_home()
        detail_reads = 1
        pending.remove(selected.fingerprint)
        pending_set.remove(selected.fingerprint)
        status = "COMPLETE"
    elif new_relevant or pending_set:
        status = "RELEVANT_POST_NOT_CURRENT"
    elif relevant:
        status = "NO_NEW_RELEVANT_POST"
    else:
        status = "NO_RELEVANT_POST"

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
        home_sampled=checkpoint.home_sampled + len(targets),
        home_new=checkpoint.home_new + len(new_targets),
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
