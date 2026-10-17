"""Detail compatibility methods retained by the reader facade."""

from __future__ import annotations

from playwright.async_api import (
    Locator,
)

from ..browser_ui.body_text import (
    _MAX_BODY_CODEPOINTS,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body_block,  # noqa: F401 - retained reader-module compatibility name
)
from ..browser_ui.feed_rows import (
    _MAX_CARD_LINKS,  # noqa: F401 - retained reader-module compatibility name
)
from ..feed.models import (
    PostTarget,
)
from ..feed.urls import (
    _rejected_post_link_blocks_candidate,  # noqa: F401 - retained reader-module compatibility name
)


class ReaderDetailCompatibility:
    """Compatibility delegates grouped by detail responsibility."""

    async def read_detail(self, target: PostTarget) -> PostTarget:
        return await self._read_detail(target)

    @property
    def detail_route_verified(self) -> bool:
        return self._detail_navigator.route_verified

    async def actionable_now(self, target: PostTarget) -> PostTarget:
        return await self._detail_navigator.actionable_now(target)

    async def _revalidate_action_target(self, target: PostTarget) -> PostTarget:
        return await self._detail_navigator._revalidate_action_target(target)

    async def _detail_click_point(self, permalink: Locator) -> tuple[float, float]:
        return await self._detail_navigator._detail_click_point(permalink)

    async def _read_detail(self, target: PostTarget) -> PostTarget:
        return await self._detail_navigator._read_detail(target)

    async def _detail_permalink_card(self, target: PostTarget) -> Locator:
        return await self._detail_navigator._detail_permalink_card(target)

    async def return_to_home(self) -> None:
        await self._detail_navigator.return_to_home()
