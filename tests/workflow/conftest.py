import pytest

from threads_browser import reader as reader_module


@pytest.fixture(autouse=True)
def _short_offline_render_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 25)
    monkeypatch.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 20)
