from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Route
from playwright.async_api import async_playwright as real_async_playwright

from threads_browser import workflow
from threads_browser.cli import main
from threads_browser.reader import HOME_URL, canonical_terms, classify_body
from threads_browser.storage import AccountStore, CheckpointStore
from threads_browser.workflow import policy_fingerprint


def test_relevance_classifier_is_conservative() -> None:
    terms = canonical_terms(("tuyển dụng", "engineer"))
    assert classify_body("We are hiring an engineer", terms) == "RELEVANT"
    assert classify_body("A weather update", terms) == "IRRELEVANT"
    assert classify_body(None, terms) == "UNKNOWN"
    assert classify_body("   ", terms) == "UNKNOWN"


def test_offline_chromium_cli_reads_once_and_resumes_without_double_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/one"
    body = "Hiring a frontend engineer in Vietnam"
    feed_html = f"""<!doctype html>
<html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<article>
  <a href="/@candidate/post/one"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>{body}</p>
</article>
</body></html>"""
    account_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(account_root).add("offline", actor, profile)
    store = CheckpointStore(account_root)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(account_root))
    requests: list[str] = []

    async def respond(route: Route) -> None:
        url = route.request.url
        requests.append(url)
        if url == HOME_URL or url == target_url:
            html = feed_html
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=html)

    class OfflinePlaywrightManager:
        """Real persistent Chromium with all page requests fulfilled locally."""

        async def __aenter__(self) -> SimpleNamespace:
            self.manager = real_async_playwright()
            self.playwright = await self.manager.start()
            chromium = self.playwright.chromium

            class Chromium:
                async def launch_persistent_context(self, **kwargs):
                    options = dict(kwargs)
                    options["headless"] = True
                    context = await chromium.launch_persistent_context(**options)
                    await context.route("**/*", respond)
                    return context

            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_: object) -> None:
            await self.playwright.stop()

    monkeypatch.setattr(workflow, "async_playwright", OfflinePlaywrightManager)
    command = [
        "nurture",
        "run",
        "offline",
        "--headed",
        "--max-posts",
        "5",
        "--term",
        "hiring",
        "--term",
        "engineer",
    ]

    first_code = main(command)
    first = capsys.readouterr()
    second_code = main(command)
    second = capsys.readouterr()

    assert first_code == 0, first.out + first.err
    assert "NURTURE status=COMPLETE source=HOME_FOR_YOU" in first.out
    assert "ui=UNVERIFIED_LIVE_UI" in first.out
    assert "home_sampled=1 home_relevant=1" in first.out
    assert "home_relevant_ratio=1.00" in first.out
    assert "detail_reads=1 scrolls=1 checkpoint_runs=1" in first.out
    assert second_code == 0, second.out + second.err
    assert "NURTURE status=NO_NEW_RELEVANT_POST source=HOME_FOR_YOU" in second.out
    assert "home_sampled=0" in second.out
    assert "duplicates=1 detail_reads=0" in second.out
    assert all(
        value not in first.out + first.err + second.out + second.err
        for value in (str(profile), actor, body, target_url)
    )

    assert requests
    assert all(urlsplit(url).netloc == "www.threads.com" for url in requests)
    assert {HOME_URL, target_url}.issubset(requests)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 2
    assert checkpoint.home_sampled == 1
    assert checkpoint.home_relevant == 1
    assert checkpoint.detail_reads == 1
    assert len(checkpoint.seen_fingerprints) == 1
    checkpoint_path = (
        account_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    )
    persisted = checkpoint_path.read_text(encoding="utf-8")
    assert body not in persisted
    assert target_url not in persisted
    assert actor not in persisted
