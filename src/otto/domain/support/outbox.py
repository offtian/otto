"""
The outbox: every reply Otto owes a conversation is recorded before it is
sent, so a channel outage (a Slack rate limit, a Jira 5xx) delays the reply
instead of losing an answer that was already paid for. Delivery is tried at
once and, on failure, again after 10 seconds, 1 minute, 10 minutes and 1
hour; the fifth failure marks the message ``dead``.

The idempotency key is unique — one reply per run — so a run replayed after a
crash that answers again writes nothing, and the conversation hears once.
Delivery is at-least-once: a send that reached the channel just before the
process died is sent again, because the row was not yet marked ``sent``.
"""

from datetime import datetime, timedelta

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

# SQLModel exposes the SQLAlchemy Table at runtime; mypy needs the hint.
_TABLE: sa.Table = models.OutboundMessageRecord.__table__  # type: ignore[attr-defined]


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


@attrs.frozen
class PostgresOutbox:
    """
    Replies awaiting delivery, over Postgres. ``enqueue`` is an atomic
    insert-if-absent on the idempotency key; ``claim_due`` hides what it
    returns for ``_HOLD``, so a slow send is never picked up a second time.
    """

    database: databases.Database

    async def enqueue(
        self, *, key: str, origin: entities.Origin, text: str, vote: bool
    ) -> OutboundMessage | None:
        """
        Record a reply and return it — held for the caller's own immediate
        attempt — or return None when the key already has one.
        """
        row = await self.database.fetch_one(
            pg.insert(_TABLE)
            .values(
                idempotency_key=key,
                origin=entities.origin_to_json(origin),
                body=text,
                vote=vote,
                status=PENDING,
                attempts=0,
                next_attempt_at=sa.func.now() + _HOLD,
                last_error="",
                created_at=sa.func.now(),
            )
            .on_conflict_do_nothing(index_elements=[_TABLE.c.idempotency_key])
            .returning(*_TABLE.c)
        )
        return _from_row(row) if row is not None else None

    async def claim_due(self, *, now: datetime) -> list[OutboundMessage]:
        """
        Return the pending replies whose next attempt time has passed, oldest
        first, hiding each from the next claim while it is being sent.
        """
        rows = await self.database.fetch_all(
            sa.update(_TABLE)
            .where(sa.and_(_TABLE.c.status == PENDING, _TABLE.c.next_attempt_at <= now))
            .values(next_attempt_at=now + _HOLD)
            .returning(*_TABLE.c)
        )
        return sorted((_from_row(row) for row in rows), key=lambda message: message.id)

    async def mark_sent(self, message_id: int) -> None:
        """
        Record a delivered reply. Its text is blanked — only the key stays,
        as the guard against answering the same run twice.
        """
        await self.database.execute(
            sa.update(_TABLE)
            .where(_TABLE.c.id == message_id)
            .values(status=SENT, body="", last_error="")
        )

    async def record_failure(
        self, message_id: int, *, error: str, retry_in: timedelta | None
    ) -> None:
        """
        Count one failed delivery: retry after ``retry_in``, or mark the
        reply dead when it is None.
        """
        values: dict[str, object] = {"attempts": _TABLE.c.attempts + 1, "last_error": error}
        if retry_in is None:
            values["status"] = DEAD
        else:
            values["next_attempt_at"] = sa.func.now() + retry_in
        await self.database.execute(
            sa.update(_TABLE).where(_TABLE.c.id == message_id).values(**values)
        )

    async def purge(self, *, cutoff: datetime) -> int:
        """
        Remove the delivered and dead replies recorded before ``cutoff`` and
        return how many were removed.
        """
        rows = await self.database.fetch_all(
            sa.delete(_TABLE)
            .where(sa.and_(_TABLE.c.status != PENDING, _TABLE.c.created_at < cutoff))
            .returning(_TABLE.c.id)
        )
        return len(rows)


def _from_row(row: databases.interfaces.Record) -> OutboundMessage:
    return OutboundMessage(
        id=row["id"],
        key=row["idempotency_key"],
        origin=entities.origin_from_json(row["origin"]),
        text=row["body"],
        vote=row["vote"],
        attempts=row["attempts"],
    )
