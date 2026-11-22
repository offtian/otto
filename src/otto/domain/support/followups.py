"""
Follow-ups: a request that arrives later. An agent that needs time to pass —
a build to finish, a sync to run, an approval elsewhere — schedules one with
a note to itself; when it is due, the always-on loop turns it into an inbound
request on the same conversation and the agent reads the note as the reason
it woke.

  pending    waiting for its time
  fired      turned into a request; the note is gone
  cancelled  called off before its time; the note is gone

Two caps keep an agent from waking itself forever: a conversation may hold
only a few pending follow-ups at once, and only so many in any 24 hours —
the second is what bounds a follow-up that schedules the next one.

``FollowUpStore`` is the port the agent tools and the always-on loop talk to;
``PostgresFollowUpStore`` is its one implementation today.
"""

from datetime import datetime, timedelta
from typing import Protocol

import attrs
import databases
import sqlalchemy as sa

from otto.data import models
from otto.domain.support import entities


PENDING = "pending"
FIRED = "fired"
CANCELLED = "cancelled"

# Follow-ups one conversation may have waiting at the same time.
MAX_PENDING = 3
# Follow-ups one conversation may schedule in any 24 hours, fired or not.
MAX_PER_DAY = 10


class FollowUpLimitReached(Exception):
    """
    Raised when a conversation already holds as many follow-ups as allowed.
    """


@attrs.frozen
class FollowUp:
    """
    One due follow-up: where to wake, on whose behalf, and why.
    """

    id: int
    origin: entities.Origin
    requester_id: str
    note: str


class FollowUpStore(Protocol):
    """
    Follow-ups agents have scheduled. Every method may raise on a store
    outage.
    """

    async def schedule(
        self, *, origin: entities.Origin, requester_id: str, note: str, delay: timedelta
    ) -> None:
        """
        Schedule a follow-up on the conversation, due after ``delay``.

        :raises FollowUpLimitReached: if the conversation already has
            ``MAX_PENDING`` waiting, or scheduled ``MAX_PER_DAY`` in the last
            24 hours.
        """
        ...

    async def due(self, *, now: datetime) -> list[FollowUp]:
        """
        Return the pending follow-ups whose time has come, earliest first.
        Reading does not claim them — ``mark_fired`` does.
        """
        ...

    async def mark_fired(self, follow_up_id: int) -> None:
        """
        Record that a follow-up was turned into a request. Its note is
        dropped — the request carries it from here.
        """
        ...

    async def cancel_pending(self, *, origin: entities.Origin) -> int:
        """
        Cancel every follow-up still waiting on the conversation and return
        how many there were.
        """
        ...

    async def purge(self, *, cutoff: datetime) -> int:
        """
        Remove the fired and cancelled follow-ups scheduled before ``cutoff``
        and return how many were removed. Pending ones are never purged.
        """
        ...


# SQLModel exposes the SQLAlchemy Table at runtime; mypy needs the hint.
_TABLE: sa.Table = models.FollowUpRecord.__table__  # type: ignore[attr-defined]


@attrs.frozen
class PostgresFollowUpStore:
    """
    ``FollowUpStore`` over Postgres. Due times are stamped by the database
    clock, so no caller has to carry one.

    ponytail: the caps are a count followed by an insert, not one statement —
    two schedules racing on one conversation could both pass. They come from
    one agent run at a time, so the race needs a second replica to exist.
    """

    database: databases.Database

    async def schedule(
        self, *, origin: entities.Origin, requester_id: str, note: str, delay: timedelta
    ) -> None:
        origin_json = entities.origin_to_json(origin)
        counts = await self.database.fetch_one(
            sa.select(
                sa.func.count().filter(_TABLE.c.status == PENDING).label("pending"),
                sa.func.count()
                .filter(_TABLE.c.created_at > sa.func.now() - _interval(timedelta(hours=24)))
                .label("recent"),
            ).where(_TABLE.c.origin == origin_json)
        )
        if counts is not None and (
            counts["pending"] >= MAX_PENDING or counts["recent"] >= MAX_PER_DAY
        ):
            raise FollowUpLimitReached("the conversation is at its follow-up limit")
        await self.database.execute(
            sa.insert(_TABLE).values(
                origin=origin_json,
                requester_id=requester_id,
                note=note,
                due_at=sa.func.now() + _interval(delay),
                status=PENDING,
                created_at=sa.func.now(),
            )
        )

    async def due(self, *, now: datetime) -> list[FollowUp]:
        rows = await self.database.fetch_all(
            sa.select(_TABLE)
            .where(sa.and_(_TABLE.c.status == PENDING, _TABLE.c.due_at <= now))
            .order_by(_TABLE.c.due_at)
        )
        return [
            FollowUp(
                id=row["id"],
                origin=entities.origin_from_json(row["origin"]),
                requester_id=row["requester_id"],
                note=row["note"],
            )
            for row in rows
        ]

    async def mark_fired(self, follow_up_id: int) -> None:
        await self.database.execute(
            sa.update(_TABLE).where(_TABLE.c.id == follow_up_id).values(status=FIRED, note="")
        )

    async def cancel_pending(self, *, origin: entities.Origin) -> int:
        rows = await self.database.fetch_all(
            sa.update(_TABLE)
            .where(
                sa.and_(
                    _TABLE.c.origin == entities.origin_to_json(origin),
                    _TABLE.c.status == PENDING,
                )
            )
            .values(status=CANCELLED, note="")
            .returning(_TABLE.c.id)
        )
        return len(rows)

    async def purge(self, *, cutoff: datetime) -> int:
        rows = await self.database.fetch_all(
            sa.delete(_TABLE)
            .where(sa.and_(_TABLE.c.status != PENDING, _TABLE.c.created_at < cutoff))
            .returning(_TABLE.c.id)
        )
        return len(rows)


def _interval(delta: timedelta) -> sa.ColumnElement[timedelta]:
    """
    Return ``delta`` as an explicitly typed SQL interval. Left untyped,
    Postgres reads the parameter in ``now() - $1`` as a timestamp — the
    subtraction then yields an interval and the comparison fails.
    """
    return sa.cast(delta, sa.Interval)
