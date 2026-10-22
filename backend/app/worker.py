import asyncio
import contextlib
import signal

from app.core.config import get_settings
from app.core.logging import log, setup_logging

TICK_S = 1.0


async def run(stop: asyncio.Event) -> None:
    log.info("worker started")
    while not stop.is_set():
        # Phase 4 claims due decisions here (FOR UPDATE SKIP LOCKED) and calls advance()/expire().
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), TICK_S)
    log.info("worker stopped")


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(stop)


if __name__ == "__main__":
    setup_logging(get_settings().log_level)
    asyncio.run(main())
