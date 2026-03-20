from __future__ import annotations

import aiohttp

from container_up.settings import SEND_MSG_TIMEOUT


_dispatch_session: aiohttp.ClientSession | None = None


def init_dispatch_session() -> aiohttp.ClientSession:
    global _dispatch_session
    _dispatch_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=SEND_MSG_TIMEOUT)
    )
    return _dispatch_session


def get_dispatch_session() -> aiohttp.ClientSession:
    if _dispatch_session is None:
        raise RuntimeError("dispatch session is not initialized")
    return _dispatch_session


async def close_dispatch_session() -> None:
    global _dispatch_session
    if _dispatch_session is None:
        return
    await _dispatch_session.close()
    _dispatch_session = None
