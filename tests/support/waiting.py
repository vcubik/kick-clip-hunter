"""Bounded waiting for things that happen on another thread or task.

Tests never sleep for a fixed time and hope: they poll a condition and fail
with a clear message if it doesn't become true within a generous deadline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

DEFAULT_TIMEOUT = 10.0


def wait_until(condition: Callable[[], object], what: str, timeout: float = DEFAULT_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        time.sleep(0.01)


async def async_wait_until(condition: Callable[[], object], what: str, timeout: float = DEFAULT_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        await asyncio.sleep(0.01)
