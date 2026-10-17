from __future__ import annotations

import asyncio
import ctypes
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures.browser import _offline_playwright_manager, create_local_account
from threads_browser import workflow
from threads_browser.storage import CheckpointStore


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _windows_chromium_processes() -> set[int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        processes: list[tuple[int, str]] = []
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                processes.append((entry.th32ProcessID, entry.szExeFile.casefold()))
                entry.dwSize = ctypes.sizeof(entry)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    return {
        process_id
        for process_id, image_name in processes
        if "chrome" in image_name or "chromium" in image_name
    }


def _assert_no_new_chromium_processes(baseline: set[int]) -> None:
    deadline = time.monotonic() + 3
    while True:
        if not (_windows_chromium_processes() - baseline):
            return
        if time.monotonic() >= deadline:
            pytest.fail("managed browser child process remained after cleanup")
        time.sleep(0.05)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup gate")
def test_windows_headed_cleanup_stress_closes_chromium_children_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = create_local_account(tmp_path / "data")
    store = CheckpointStore(tmp_path / "data")

    async def deny_network(route) -> None:
        await route.abort()

    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(deny_network, headless=False),
    )

    async def successful_cycle(*_args, **_kwargs):
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "_run_page_workflow_locked", successful_cycle)
    baseline = _windows_chromium_processes()

    async def run_cycle() -> None:
        await asyncio.wait_for(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("synthetic",),
            ),
            timeout=30,
        )

    for _ in range(3):
        asyncio.run(run_cycle())
        _assert_no_new_chromium_processes(baseline)
        with store.profile_lock(account):
            pass
