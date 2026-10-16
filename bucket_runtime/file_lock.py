"""Share the user's publication lock with native shell executions via flock."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path


@asynccontextmanager
async def publication_lock(path: Path):
    import fcntl

    with path.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
