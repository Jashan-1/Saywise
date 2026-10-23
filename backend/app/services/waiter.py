"""Long-poll waiting (§15): one LISTEN connection per process wakes the requests waiting on a
decision id."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
from sqlalchemy import make_url

from app.core.constants import WAIT_CHANNEL


class Waiter:
    def __init__(self) -> None:
        self._conn: Any = None
        self._futures: dict[str, set[asyncio.Future[None]]] = {}

    async def start(self, database_url: str) -> None:
        dsn = make_url(database_url).set(drivername="postgresql")
        # shortcut: no reconnect; if this connection drops, waits time out and callers poll.
        self._conn = await asyncpg.connect(dsn.render_as_string(hide_password=False))
        await self._conn.add_listener(WAIT_CHANNEL, self._on_notify)

    async def stop(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _on_notify(self, conn: Any, pid: int, channel: str, payload: str) -> None:
        for future in self._futures.get(payload, ()):
            if not future.done():
                future.set_result(None)

    async def wait(
        self, decision_id: str, timeout_s: float, settled: Callable[[], Awaitable[bool]]
    ) -> None:
        """Return when `decision_id` is notified or `timeout_s` passes. `settled` is checked after
        subscribing, which closes the race with an answer that landed first."""
        future = asyncio.get_running_loop().create_future()
        self._futures.setdefault(decision_id, set()).add(future)
        try:
            if not await settled():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(future, timeout_s)
        finally:
            waiting = self._futures[decision_id]
            waiting.discard(future)
            if not waiting:
                del self._futures[decision_id]


waiter = Waiter()
