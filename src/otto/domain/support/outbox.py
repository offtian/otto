"""
The outbox: every reply Otto owes a conversation is recorded before it is
sent, so a channel outage (a Slack rate limit, a Jira 5xx) delays the reply
instead of losing an answer that was already paid for. Delivery is tried at
once and, on failure, again after 10 seconds, 1 minute, 10 minutes and 1
hour; the fifth failure marks the message ``dead``.

  pending   waiting for its next attempt
  sent      delivered; the text is gone, the key stays
  dead      failed five times; nobody retries it

The idempotency key is unique — one reply per run — so a run replayed after a
crash that answers again writes nothing, and the conversation hears once.
Delivery is at-least-once: a send that reached the channel just before the
process died is sent again, because the row was not yet marked ``sent``.

``Outbox`` is the port the application talks to (``application.delivery`` is
its only caller); ``PostgresOutbox`` is its one implementation today.
"""

from datetime import datetime, timedelta
from typing import Any, Protocol

import attrs
import databases
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from otto.data import models
from otto.domain.support import entities


PENDING = "pending"
SENT = "sent"
DEAD = "dead"

# The wait after the first, second, third and fourth failed delivery.
_BACKOFF = (
    timedelta(seconds=10),
    timedelta(minutes=1),
    timedelta(minutes=10),
    timedelta(hours=1),
)

# How long a message stays hidden once claimed for an attempt — longer than
# any channel send can take, so a message being sent is never claimed twice.
_HOLD = timedelta(seconds=60)


def retry_delay(*, attempts: int) -> timedelta | None:
    """
    Return how long to wait before the next delivery attempt after
    ``attempts`` failures, or None to give up — the fifth failure is final.
    """
    return _BACKOFF[attempts - 1] if attempts <= len(_BACKOFF) else None


@attrs.frozen
class OutboundMessage:
    """
    One reply awaiting delivery at its origin.
    """

    id: int
    key: str
    origin: entities.Origin
    text: str
    # Carry the "Did this help?" vote where the channel supports one.
    vote: bool
    # Failed deliveries so far.
    attempts: int


class Outbox(Protocol):
    """
    Replies awaiting delivery. Every method may raise on a store outage.
    """

    async def enqueue(
        self, *, key: str, origin: entities.Origin, text: str, vote: bool
    ) -> OutboundMessage | None:
        """
        Record a reply and return it, or return None when ``key`` already has
        one — sent or not. The returned message is hidden from ``claim_due``
        for a moment: the caller makes the first attempt itself.
        """
        ...

    async def claim_due(self, *, now: datetime) -> list[OutboundMessage]:
        """
        Return the pending replies whose next attempt time has passed, oldest
        first, hiding each from the next claim while it is being sent — so
        two claimers can never send the same reply.
        """
        ...

    async def mark_sent(self, message_id: int) -> None:
        """
        Record a delivered reply. Its text is dropped; only the key stays, as
        the guard against answering the same run twice.
        """
        ...

    async def record_failure(
        self, message_id: int, *, error: str, retry_in: timedelta | None
    ) -> None:
        """
        Count one failed delivery: the reply is due again after ``retry_in``,
        or dead when it is None. ``error`` is a short label, never a message
        that could carry conversation content.
        """
        ...

    async def purge(self, *, cutoff: datetime) -> int:
        """
        Remove the delivered and dead replies recorded before ``cutoff`` and
        return how many were removed. Pending replies are never purged.
        """
        ...


# SQLModel exposes the SQLAlchemy Table at runtime; mypy needs the hint.
_TABLE: sa.Table = models.OutboundMessageRecord.__table__  # type: ignore[attr-defined]


@attrs.frozen
class PostgresOutbox:
    """
    ``Outbox`` over Postgres. ``enqueue`` is one insert-if-absent on the
    unique key, and ``claim_due`` one UPDATE that pushes each claimed row's
    next attempt out by ``_HOLD`` — the claim is its own lock.
    """

    database: databases.Database

    async def enqueue(
        self, *, key: str, origin: entities.Origin, text: str, vote: bool
    ) -> OutboundMessage | None:
        row = await self.database.fetch_one(
            pg.insert(_TABLE)
            .values(
                idempotency_key=key,
                origin=entities.origin_to_json(origin),
                body=text,
                vote=vote,
                status=PENDING,
                attempts=0,
                next_attempt_at=sa.func.now() + _interval(_HOLD),
                last_error="",
                created_at=sa.func.now(),
            )
            .on_conflict_do_nothing(index_elements=[_TABLE.c.idempotency_key])
            .returning(*_TABLE.c)
        )
        return _from_row(row) if row is not None else None

    async def claim_due(self, *, now: datetime) -> list[OutboundMessage]:
        rows = await self.database.fetch_all(
            sa.update(_TABLE)
            .where(sa.and_(_TABLE.c.status == PENDING, _TABLE.c.next_attempt_at <= now))
            .values(next_attempt_at=now + _HOLD)
            .returning(*_TABLE.c)
        )
        return sorted((_from_row(row) for row in rows), key=lambda message: message.id)

    async def mark_sent(self, message_id: int) -> None:
        await self.database.execute(
            sa.update(_TABLE)
            .where(_TABLE.c.id == message_id)
            .values(status=SENT, body="", last_error="")
        )

    async def record_failure(
        self, message_id: int, *, error: str, retry_in: timedelta | None
    ) -> None:
        values: dict[str, object] = {"attempts": _TABLE.c.attempts + 1, "last_error": error}
        if retry_in is None:
            values["status"] = DEAD
        else:
            values["next_attempt_at"] = sa.func.now() + _interval(retry_in)
        await self.database.execute(
            sa.update(_TABLE).where(_TABLE.c.id == message_id).values(**values)
        )

    async def purge(self, *, cutoff: datetime) -> int:
        rows = await self.database.fetch_all(
            sa.delete(_TABLE)
            .where(sa.and_(_TABLE.c.status != PENDING, _TABLE.c.created_at < cutoff))
            .returning(_TABLE.c.id)
        )
        return len(rows)


def _interval(delta: timedelta) -> sa.ColumnElement[timedelta]:
    """
    Return ``delta`` as an explicitly typed SQL interval, so Postgres never
    has to guess what an untyped parameter next to ``now()`` is.
    """
    return sa.cast(delta, sa.Interval)


def _from_row(row: Any) -> OutboundMessage:
    return OutboundMessage(
        id=row["id"],
        key=row["idempotency_key"],
        origin=entities.origin_from_json(row["origin"]),
        text=row["body"],
        vote=row["vote"],
        attempts=row["attempts"],
    )
