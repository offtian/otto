"""
The durable inbox: every inbound request is recorded before its channel is
told "received" and removed once its handler has finished. A row that
survives a restart is therefore a request somebody is still waiting on —
startup replays it (``application.dispatch.unfinished_requests``) instead of
dropping it silently (FR8).

Rows exist only while a request is in flight: the request text is
conversation content and is not kept at rest once the request is handled.
"""

from datetime import datetime

import attrs
import databases
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from otto.data import models
from otto.domain.support import entities


# SQLModel exposes the SQLAlchemy Table at runtime; mypy needs the hint.
_TABLE: sa.Table = models.InboundRequestRecord.__table__  # type: ignore[attr-defined]


@attrs.frozen
class PostgresInbox:
    """
    Inbound requests in flight, over Postgres. The primary key is the
    channel's own delivery id, so ``accept`` is an atomic insert-if-absent:
    a redelivery of an in-flight request is refused on every replica and
    across restarts.
    """

    database: databases.Database

    async def accept(self, request: entities.SupportRequest) -> bool:
        """
        Record the request and return True, or return False when it is
        already recorded — a redelivery of a request still being handled.
        """
        inserted = await self.database.fetch_one(
            pg.insert(_TABLE)
            .values(
                id=request.id,
                requester_id=request.user_id,
                origin=entities.origin_to_json(request.origin),
                request_text=request.text,
                received_at=sa.func.now(),
            )
            .on_conflict_do_nothing(index_elements=[_TABLE.c.id])
            .returning(_TABLE.c.id)
        )
        return inserted is not None

    async def finish(self, request_id: str) -> None:
        """
        Remove a request whose handler has finished.
        """
        await self.database.execute(sa.delete(_TABLE).where(_TABLE.c.id == request_id))

    async def discard_stale(self, *, cutoff: datetime) -> int:
        """
        Remove the unfinished requests received before ``cutoff`` — too old
        to answer usefully — and return how many were removed.
        """
        rows = await self.database.fetch_all(
            sa.delete(_TABLE).where(_TABLE.c.received_at < cutoff).returning(_TABLE.c.id)
        )
        return len(rows)

    async def list_unfinished(self) -> list[entities.SupportRequest]:
        """
        Return the requests that were accepted but never finished, oldest
        first.
        """
        rows = await self.database.fetch_all(sa.select(_TABLE).order_by(_TABLE.c.received_at))
        return [
            entities.SupportRequest(
                id=row["id"],
                user_id=row["requester_id"],
                text=row["request_text"],
                origin=entities.origin_from_json(row["origin"]),
            )
            for row in rows
        ]
