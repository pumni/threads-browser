"""Small async Playwright reader for a bounded Home / For You observation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from hashlib import sha256
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    Locator,
    Page,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

THREADS_ORIGIN = "https://www.threads.com"
HOME_URL = f"{THREADS_ORIGIN}/"
_NAVIGATION_TIMEOUT_MS = 20_000
_DETAIL_TIMEOUT_MS = 15_000
_READINESS_TIMEOUT_MS = 8_000
_MAX_BODY_CODEPOINTS = 10_000
_USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
_POST_PATH = re.compile(r"/@(?P<author>[A-Za-z0-9._]{1,30})/post/(?P<post>[A-Za-z0-9_-]{1,120})")
_PROFILE_PATH = re.compile(r"/@(?P<author>[A-Za-z0-9._]{1,30})/?")
_FOR_YOU_NAME = re.compile(r"^for you$", re.IGNORECASE)
_PROFILE_NAME = re.compile(r"^(?:profile|your profile|account)$", re.IGNORECASE)
_MORE_NAME = re.compile(r"^(?:more|see more|read more)$", re.IGNORECASE)


class UiReadError(Exception):
    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class PostTarget:
    url: str = field(repr=False)
    author: str = field(repr=False)
    body: str | None = field(repr=False)
    fingerprint: str
    permalink: Locator = field(repr=False)
    visible_after_scroll: bool = field(default=False, repr=False)
    viewport_y: float = field(default=0.0, repr=False)

    def __repr__(self) -> str:
        return "PostTarget(<private>)"


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def canonical_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(dict.fromkeys(normalize_text(term) for term in terms if term.strip()))
    if not cleaned or any(len(term) > 100 for term in cleaned):
        raise UiReadError("TERMS_INVALID")
    return cleaned


def classify_body(body: str | None, terms: tuple[str, ...]) -> str:
    if body is None or not body.strip():
        return "UNKNOWN"
    normalized = normalize_text(body)
    return "RELEVANT" if any(term in normalized for term in terms) else "IRRELEVANT"


def _post_url(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(urljoin(HOME_URL, value))
    except ValueError:
        return None
    match = _POST_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.threads.com"
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        return None
    canonical = urlunsplit(("https", "www.threads.com", parsed.path, "", ""))
    return canonical, match.group("author").casefold()


def _profile_author(value: str) -> str | None:
    try:
        parsed = urlsplit(urljoin(HOME_URL, value))
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.threads.com"
        or parsed.query
        or parsed.fragment
    ):
        return None
    match = _PROFILE_PATH.fullmatch(parsed.path)
    return None if match is None else match.group("author").casefold()


def _same_home_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc.casefold() == "www.threads.com"


class ThreadsHomeReader:
    """Reads one Home / For You sample and at most one exact visible post."""

    def __init__(self, page: Page, expected_actor: str) -> None:
        if _USERNAME.fullmatch(expected_actor) is None:
            raise UiReadError("ACTOR_INVALID")
        self._page = page
        self._expected_actor = expected_actor.casefold()
        self._home_url = HOME_URL
        self.scroll_attempts = 0
        self.scrolls = 0
        self.feed_duplicates = 0

    async def open_home(self) -> None:
        try:
            await self._page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            raise UiReadError("HOME_NAVIGATION_TIMEOUT") from None
        await self._require_feed_url()
        control = await self._for_you_control()
        href = await control.get_attribute("href")
        destination = None if href is None else urljoin(self._page.url, href)
        if destination is not None and not _same_home_origin(destination):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        if not await self._for_you_is_active(control):
            try:
                await control.click(timeout=_NAVIGATION_TIMEOUT_MS)
                if destination is not None and destination != self._page.url:
                    await self._page.wait_for_url(
                        destination,
                        timeout=_NAVIGATION_TIMEOUT_MS,
                    )
            except PlaywrightTimeoutError:
                raise UiReadError("HOME_UI_UNVERIFIED") from None
        await self._require_feed_url()
        active = await self._for_you_is_active(control)
        if not active and not self._is_for_you_route(self._page.url):
            raise UiReadError("HOME_UI_UNVERIFIED")
        self._home_url = self._page.url
        await self.verify_actor()
    async def verify_actor(self) -> None:
        navigation = self._page.get_by_role("navigation")
        profile_links = navigation.get_by_role("link", name=_PROFILE_NAME)
        try:
            await profile_links.first.wait_for(
                state="visible",
                timeout=_READINESS_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            raise UiReadError("ACTOR_UNVERIFIED") from None
        matches: list[Locator] = []
        for index in range(await navigation.count()):
            links = navigation.nth(index).get_by_role("link", name=_PROFILE_NAME)
            for link_index in range(await links.count()):
                link = links.nth(link_index)
                if await link.is_visible():
                    matches.append(link)
        if len(matches) != 1:
            raise UiReadError("ACTOR_UNVERIFIED")
        href = await matches[0].get_attribute("href")
        actor = None if href is None else _profile_author(href)
        if actor is None:
            raise UiReadError("ACTOR_UNVERIFIED")
        if actor != self._expected_actor:
            raise UiReadError("ACTOR_MISMATCH")
    async def sample_and_scroll(self, max_posts: int) -> tuple[PostTarget, ...]:
        if not 1 <= max_posts <= 20:
            raise UiReadError("POST_LIMIT_INVALID")
        before, scroll_anchor = await self._visible_targets(max_posts)
        if not before or scroll_anchor is None:
            raise UiReadError("FEED_UI_UNVERIFIED")
        before_by_fingerprint = {target.fingerprint: target for target in before}
        self.feed_duplicates = len(before) - len(before_by_fingerprint)
        self.scroll_attempts = 0
        self.scrolls = 0
        try:
            await self._page.mouse.move(
                scroll_anchor["x"] + scroll_anchor["width"] / 2,
                scroll_anchor["y"] + scroll_anchor["height"] / 2,
            )
            self.scroll_attempts = 1
            await self._page.mouse.wheel(0, 850)
        except PlaywrightError:
            raise UiReadError("HOME_SCROLL_FAILED") from None
        after, _ = await self._visible_targets(max_posts, visible_after_scroll=True)
        await self._require_feed_url()
        after_by_fingerprint = {target.fingerprint: target for target in after}
        self.feed_duplicates += len(after) - len(after_by_fingerprint)
        self.scrolls = int(
            any(
                fingerprint not in before_by_fingerprint
                or abs(target.viewport_y - before_by_fingerprint[fingerprint].viewport_y) > 1
                for fingerprint, target in after_by_fingerprint.items()
            )
        )
        targets = dict(before_by_fingerprint)
        for fingerprint, target in after_by_fingerprint.items():
            if fingerprint in targets:
                self.feed_duplicates += 1
            targets[fingerprint] = target
        return tuple(list(targets.values())[:max_posts])

    async def _visible_targets(
        self,
        max_posts: int,
        *,
        visible_after_scroll: bool = False,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float] | None]:
        articles = self._page.get_by_role("article")
        viewport = self._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_UI_UNVERIFIED")
        targets: list[PostTarget] = []
        scroll_anchor: dict[str, float] | None = None
        article_count = await articles.count()
        scan_limit = min(article_count, max_posts + 20)
        visible_limit = max_posts
        for index in range(scan_limit):
            article = articles.nth(index)
            if not await article.is_visible():
                continue
            box = await article.bounding_box()
            if (
                box is None
                or box["x"] >= viewport["width"]
                or box["y"] >= viewport["height"]
                or box["x"] + box["width"] <= 0
                or box["y"] + box["height"] <= 0
            ):
                continue
            if scroll_anchor is None:
                scroll_anchor = box
            targets.append(
                await self._read_card(
                    article,
                    visible_after_scroll=visible_after_scroll,
                    viewport_y=box["y"],
                )
            )
            if len(targets) >= visible_limit:
                break
        return tuple(targets), scroll_anchor
    async def read_detail(self, target: PostTarget) -> PostTarget:
        if not target.visible_after_scroll:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        try:
            await target.permalink.click(timeout=_DETAIL_TIMEOUT_MS)
            await self._page.wait_for_url(target.url, timeout=_DETAIL_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
        if not _same_home_origin(self._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        await self._check_blocker()
        articles = self._page.get_by_role("article")
        matching: list[Locator] = []
        for index in range(await articles.count()):
            article = articles.nth(index)
            for link in await article.get_by_role("link").all():
                href = await link.get_attribute("href")
                parsed = None if href is None else _post_url(href)
                if parsed is not None and parsed[0] == target.url:
                    matching.append(article)
                    break
        if len(matching) != 1:
            raise UiReadError("DETAIL_ROOT_UNVERIFIED")
        detail = await self._read_card(matching[0])
        if detail.url != target.url or detail.author != target.author:
            raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
        if detail.body is None or target.body is None or detail.body != target.body:
            raise UiReadError("DETAIL_BODY_UNVERIFIED")
        return detail

    async def return_to_home(self) -> None:
        try:
            await self._page.go_back(
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            raise UiReadError("RETURN_UNVERIFIED") from None
        await self._require_feed_url()
        if self._page.url != self._home_url:
            raise UiReadError("RETURN_UNVERIFIED")
        await self.verify_actor()
    async def _for_you_control(self) -> Locator:
        matches: list[Locator] = []
        for role in ("link", "tab"):
            controls = self._page.get_by_role(role, name=_FOR_YOU_NAME)
            try:
                await controls.first.wait_for(
                    state="visible",
                    timeout=_READINESS_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                continue
            for index in range(await controls.count()):
                control = controls.nth(index)
                if await control.is_visible():
                    matches.append(control)
            if matches:
                break
        if len(matches) != 1:
            raise UiReadError("HOME_UI_UNVERIFIED")
        return matches[0]

    @staticmethod
    async def _for_you_is_active(control: Locator) -> bool:
        return (
            await control.get_attribute("aria-current") == "page"
            or await control.get_attribute("aria-selected") == "true"
        )

    @staticmethod
    def _is_for_you_route(value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.netloc.casefold() == "www.threads.com"
            and parsed.path == "/for_you"
            and not parsed.query
            and not parsed.fragment
        )

    async def _require_feed_url(self) -> None:
        if not _same_home_origin(self._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        try:
            parsed = urlsplit(self._page.url)
        except ValueError:
            raise UiReadError("HOME_UI_UNVERIFIED") from None
        if parsed.path not in {"/", "/home", "/for_you"} or parsed.query or parsed.fragment:
            path = parsed.path.casefold()
            if "challenge" in path or "checkpoint" in path:
                raise UiReadError("CHALLENGE_REQUIRED")
            if path in {"/login", "/accounts/login"}:
                raise UiReadError("LOGIN_REQUIRED")
            raise UiReadError("HOME_UI_UNVERIFIED")
        await self._check_blocker()

    async def _check_blocker(self) -> None:
        title = normalize_text(await self._page.title())
        headings = self._page.get_by_role("heading")
        parts = [title]
        for heading in await headings.all():
            if await heading.is_visible():
                parts.append(normalize_text(await heading.inner_text()))
        visible_labels = " ".join(parts)
        if any(marker in visible_labels for marker in ("log in", "sign in", "login")):
            raise UiReadError("LOGIN_REQUIRED")
        if any(
            marker in visible_labels
            for marker in ("challenge", "checkpoint", "verify your identity", "security check")
        ):
            raise UiReadError("CHALLENGE_REQUIRED")
        if any(
            marker in visible_labels
            for marker in ("rate limit", "too many requests", "try again later", "temporarily blocked")
        ):
            raise UiReadError("RATE_LIMITED")
    async def _read_card(
        self,
        article: Locator,
        *,
        visible_after_scroll: bool = False,
        viewport_y: float = 0.0,
    ) -> PostTarget:
        if not await article.is_visible():
            raise UiReadError("POST_NOT_VISIBLE")
        post_links: list[tuple[str, str, Locator]] = []
        authors: set[str] = set()
        for link in await article.get_by_role("link").all():
            href = await link.get_attribute("href")
            if href is None:
                continue
            parsed_post = _post_url(href)
            if parsed_post is not None:
                post_links.append((parsed_post[0], parsed_post[1], link))
                continue
            author = _profile_author(href)
            if author is not None:
                authors.add(author)
        unique_urls = {item[0] for item in post_links}
        unique_permalink_links = {
            (item[0], item[1]) for item in post_links
        }
        if len(unique_urls) != 1 or len(unique_permalink_links) != 1 or len(authors) != 1:
            raise UiReadError("POST_PROVENANCE_UNVERIFIED")
        url, permalink_author, link = post_links[0]
        author = next(iter(authors))
        if author != permalink_author:
            raise UiReadError("POST_AUTHOR_UNVERIFIED")

        body: str | None = None
        paragraphs = article.get_by_role("paragraph")
        if await paragraphs.count() == 1:
            paragraph = paragraphs.nth(0)
            if await paragraph.is_visible():
                candidate = (await paragraph.inner_text()).strip()
                more_buttons = article.get_by_role("button", name=_MORE_NAME)
                if (
                    candidate
                    and len(candidate) <= _MAX_BODY_CODEPOINTS
                    and await more_buttons.count() == 0
                ):
                    body = candidate
        fingerprint = sha256(f"{url}\n{author}".encode()).hexdigest()
        return PostTarget(
            url,
            author,
            body,
            fingerprint,
            link,
            visible_after_scroll,
            viewport_y,
        )
