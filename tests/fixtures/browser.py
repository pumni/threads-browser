from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Page, Route
from playwright.async_api import async_playwright as real_async_playwright

from threads_browser import workflow
from threads_browser.reader import HOME_URL, canonical_terms
from threads_browser.storage import (
    AccountStore,
    BrowserAccount,
    Checkpoint,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


@dataclass(frozen=True, slots=True)
class SyntheticPost:
    slug: str
    body: str
    author: str = "candidate"
    timestamp: str = "now"
    row_id: str | None = None


def synthetic_post_card(post: SyntheticPost) -> str:
    row_id = f' id="{escape(post.row_id, quote=True)}"' if post.row_id else ""
    slug = escape(post.slug, quote=True)
    author = escape(post.author, quote=True)
    timestamp = escape(post.timestamp)
    body = escape(post.body)
    return (
        f'<article{row_id}><a href="/@{author}/post/{slug}">'
        f'<time>{timestamp}</time></a><a href="/@{author}">{author}</a>'
        f"<p>{body}</p></article>"
    )


def synthetic_home_html(actor: str, posts: Sequence[SyntheticPost]) -> str:
    actor_href = escape(actor, quote=True)
    cards = "\n".join(synthetic_post_card(post) for post in posts)
    return (
        "<!doctype html><html><body>"
        '<nav aria-label="Main" role="navigation">'
        '<a aria-current="page" aria-label="For you" href="/">For you</a>'
        f'<a aria-label="Profile" href="/@{actor_href}">Profile</a></nav>'
        f"<main>{cards}</main></body></html>"
    )


def synthetic_detail_html(body_fragments: Sequence[str], *, landmark: bool = False) -> str:
    root = "main" if landmark else "div"
    body = "".join(f"<p>{escape(fragment)}</p>" for fragment in body_fragments)
    return f"<!doctype html><html><body><{root}>{body}</{root}></body></html>"


def create_local_account(
    data_root: Path,
    *,
    alias: str = "offline",
    actor: str = "offline.operator",
) -> BrowserAccount:
    accounts = AccountStore(data_root)
    account = accounts.add(alias)
    return accounts.bind_actor(account, actor)


def _is_offline_profile_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.netloc == "www.threads.com"
        and re.fullmatch(r"/@[A-Za-z0-9._]{1,30}/?", parsed.path) is not None
        and not parsed.query
        and not parsed.fragment
    )


def run_offline_pages(
    pages: Mapping[str, str],
    exercise: Callable[[Page], Awaitable[None]],
    *,
    headless: bool = True,
) -> None:
    """Run a callback in Chromium with every request answered from local fixtures."""

    async def run() -> None:
        async with real_async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            context = await browser.new_context(viewport={"width": 1280, "height": 720})

            async def respond(route: Route) -> None:
                url = route.request.url
                if url in pages:
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=pages[url],
                    )
                elif _is_offline_profile_url(url):
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<!doctype html><html><body><h1>Local profile</h1></body></html>",
                    )
                else:
                    await route.abort()

            await context.route("**/*", respond)
            page = await context.new_page()
            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded")
                await exercise(page)
            finally:
                await context.close()
                await browser.close()

    asyncio.run(run())


def _run_offline_page(
    html: str,
    exercise: Callable[[Page], Awaitable[None]],
    *,
    headless: bool = True,
) -> None:
    run_offline_pages({HOME_URL: html}, exercise, headless=headless)


async def _assert_bounded_wheel_gestures(page: Page, scrolls: int) -> None:
    wheel_count = int(await page.evaluate("Number(sessionStorage.getItem('wheel-count') || '0')"))
    assert wheel_count >= scrolls * 2


def _offline_playwright_manager(
    respond: Callable[[Route], Awaitable[None]],
    *,
    headless: bool = True,
    on_launch: Callable[[], None] | None = None,
):
    class OfflinePlaywrightManager:
        """Persistent Chromium test manager with all requests fulfilled locally."""

        async def __aenter__(self) -> SimpleNamespace:
            self.manager = real_async_playwright()
            self.playwright = await self.manager.start()
            chromium = self.playwright.chromium

            class Chromium:
                async def launch_persistent_context(self, **kwargs):
                    options = dict(kwargs)
                    options["headless"] = headless
                    context = await chromium.launch_persistent_context(**options)
                    if on_launch is not None:
                        on_launch()
                    await context.route("**/*", respond)
                    return context

            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_: object) -> None:
            await self.playwright.stop()

    return OfflinePlaywrightManager()


def _mock_persistent_browser(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    state = {"launches": 0}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self) -> None:
            pass

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            state["launches"] += 1
            return Context()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    return state


async def _run_page_workflow_for_test(page, account, checkpoint_store, **kwargs):
    with checkpoint_store.profile_lock(account):
        return await workflow._run_page_workflow_locked(page, account, checkpoint_store, **kwargs)


def _post_fingerprint(url: str, author: str = "candidate") -> str:
    return sha256(f"{url}\n{author}".encode()).hexdigest()


def _seed_pending_checkpoint(
    store: CheckpointStore,
    account: BrowserAccount,
    terms: tuple[str, ...],
    pending_url: str,
    *,
    completed_urls: tuple[str, ...] = (),
) -> tuple[str, str, tuple[str, ...]]:
    policy = policy_fingerprint(canonical_terms(terms))
    pending_fingerprint = _post_fingerprint(pending_url)
    completed_fingerprints = tuple(_post_fingerprint(url) for url in completed_urls)
    seen_fingerprints = (pending_fingerprint, *completed_fingerprints)
    store.save(
        Checkpoint(
            account.account_id,
            policy,
            completed_runs=len(seen_fingerprints),
            home_sampled=len(seen_fingerprints),
            home_new=len(seen_fingerprints),
            home_irrelevant=len(seen_fingerprints),
            detail_reads=len(completed_fingerprints),
            pending_read_fingerprints=(pending_fingerprint,),
            seen_fingerprints=seen_fingerprints,
        )
    )
    return policy, pending_fingerprint, completed_fingerprints


def _synthetic_three_card_feed(actor: str) -> str:
    posts = tuple(
        SyntheticPost(
            slug=f"scan-row-{index}",
            body=f"SYNTHETIC_PRIVATE_ROW_{index} weather update",
            row_id=f"scan-row-{index}",
        )
        for index in range(1, 4)
    )
    return synthetic_home_html(actor, posts)
