"""Helpers for subprocess groups and graceful service shutdown."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from typing import Any


def subprocess_group_kwargs() -> dict[str, Any]:
    """Return kwargs that start a subprocess in its own process group when supported."""
    if hasattr(os, "setsid"):
        return {"start_new_session": True}
    return {}


async def terminate_process_group(process: Any, *, timeout: float = 5.0) -> None:
    """Terminate a subprocess and its process group without touching the parent group."""
    if process is None or process.returncode is not None:
        return

    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return
    except asyncio.TimeoutError:
        pass

    if process.returncode is not None:
        return

    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def terminate_process(process: Any, *, timeout: float = 5.0) -> None:
    """Terminate a direct subprocess without signaling its process group."""
    if process is None or process.returncode is not None:
        return

    try:
        process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return
    except asyncio.TimeoutError:
        pass

    if process.returncode is not None:
        return

    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()


def install_shutdown_signal_handlers(
    stop_event: asyncio.Event,
    *,
    on_signal: Callable[[signal.Signals], None] | None = None,
) -> None:
    """Set an asyncio event on SIGTERM/SIGINT when the platform allows it."""
    loop = asyncio.get_running_loop()

    def notify(sig: signal.Signals) -> None:
        if on_signal is not None:
            on_signal(sig)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, notify, sig)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(
                    sig,
                    lambda _signum, _frame, s=sig: loop.call_soon_threadsafe(notify, s),
                )
            except ValueError:
                pass
