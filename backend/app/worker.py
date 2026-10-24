"""The worker (§15): asks the next approver when a wait runs out, expires decisions at their
deadline, and delivers callbacks. Any number of workers can run; rows are claimed with
FOR UPDATE SKIP LOCKED so no two handle the same one."""

import asyncio
import contextlib
import signal

import httpx
from sqlalchemy import select, update

from app.channels.base import Channel
from app.channels.twilio_sms import TwilioSms
from app.core.config import get_settings
from app.core.constants import CALLBACK_TIMEOUT_S, FIXED_WAIT, WORKER_BATCH, WORKER_TICK_S
from app.core.db import SessionLocal
from app.core.logging import log, setup_logging
from app.models import CallbackDelivery, Decision, utcnow
from app.services import callbacks, decisions, escalation


async def step_decisions(channel: Channel) -> int:
    done = 0
    for _ in range(WORKER_BATCH):
        async with SessionLocal() as session:
            decision = await session.scalar(
                select(Decision)
                .where(Decision.status == "pending", Decision.next_action_at <= utcnow())
                .order_by(Decision.next_action_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if decision is None:
                break
            decision_id = decision.id
            try:
                if decision.deadline_at <= utcnow():
                    await decisions.expire(session, decision, "timeout")
                else:
                    await escalation.advance(session, channel, decision)
            except Exception:
                log.exception("decision step failed", extra={"decision_id": decision_id})
                await session.rollback()
                # Push it back so one bad row can't starve the queue.
                await session.execute(
                    update(Decision)
                    .where(Decision.id == decision_id)
                    .values(next_action_at=utcnow() + FIXED_WAIT)
                )
                await session.commit()
        done += 1
    return done


async def deliver_callbacks(client: httpx.AsyncClient) -> int:
    done = 0
    for _ in range(WORKER_BATCH):
        async with SessionLocal() as session:
            delivery = await session.scalar(
                select(CallbackDelivery)
                .where(
                    CallbackDelivery.delivered_at.is_(None),
                    CallbackDelivery.next_attempt_at <= utcnow(),
                )
                .order_by(CallbackDelivery.next_attempt_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if delivery is None:
                break
            await callbacks.deliver(session, delivery, client)
        done += 1
    return done


async def tick(channel: Channel, client: httpx.AsyncClient) -> int:
    return await step_decisions(channel) + await deliver_callbacks(client)


async def run(stop: asyncio.Event) -> None:
    log.info("worker started")
    channel = TwilioSms(get_settings())
    async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_S) as client:
        while not stop.is_set():
            try:
                await tick(channel, client)
            except Exception:
                log.exception("worker tick failed")  # e.g. the database blinked; try again
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), WORKER_TICK_S)
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
