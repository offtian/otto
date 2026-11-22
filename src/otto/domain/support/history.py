"""
Per-requester history: which resolved cases a requester has had, and when.

Long-term memory (``memory``) is deliberately about nobody: a case note says
what was wrong and what fixed it, never who had the problem. This module
keeps the one identified fact beside it — a case of requester X was filed
under note N on a given day — so that a repeat can be recognised: "you had
this last week; did the fix stop holding?"

What is kept is the least that does that: the requester's id, the id of a
de-identified note, and a timestamp. No text from the conversation, and
nothing about a requester whose case taught memory nothing. A row lasts
``MEMORY_HISTORY_DAYS`` (the maintenance sweep), goes when its note is
forgotten, and one requester's rows can be deleted on request
(``just memory-forget-requester``).

Off unless ``MEMORY_HISTORY_ENABLED``: keeping it is a decision about people,
not a default.

``CaseHistory`` is the port; ``PostgresCaseHistory`` is its one
implementation. Rows are written by consolidation on a confirmed resolution
(``application.support``) and read at the retrieval gate, beside the keyword
recall (``application.flows``).
"""

from datetime import datetime, timedelta
from typing import Protocol

import attrs
import databases
import sqlalchemy as sa

from otto.data import models


@attrs.frozen
class PastCase:
    """
    A note that a requester's resolved cases were filed under, within the
    window asked for.
    """

    memory_id: int
    note: str
    # How many of their cases, and when the latest one was.
    times: int
    last_at: datetime


def describe(case: PastCase) -> str:
    """
    Return a requester's past case as an agent reads it: the note, headed by
    how often and how recently this requester had it.
    """
    often = "once" if case.times == 1 else f"{case.times} times"
    return f"This requester had this case {often}, last on {case.last_at:%Y-%m-%d}:\n{case.note}"


class CaseHistory(Protocol):
    """
    Which resolved cases each requester has had. Every method may raise on a
    store outage; callers treat the history as an assist and carry on
    without it.
    """

    async def record(self, *, requester_id: str, memory_id: int) -> None:
        """
        Record that a case of this requester's was resolved, now, and filed
        under this note.
        """
        ...

    async def recent(self, *, requester_id: str, window: timedelta, limit: int) -> list[PastCase]:
        """
        Return up to ``limit`` notes this requester's cases were filed under
        within the last ``window``, the most recently had first — each once,
        however many of their cases it covers. Nobody else's cases, ever.
        """
        ...

    async def purge(self, *, cutoff: datetime) -> int:
        """
        Delete every record older than ``cutoff`` and return how many went.
        """
        ...


# SQLModel exposes the SQLAlchemy Table at runtime; mypy needs the hint.
_CASES: sa.Table = models.RequesterCaseRecord.__table__  # type: ignore[attr-defined]
_NOTES: sa.Table = models.MemoryNoteRecord.__table__  # type: ignore[attr-defined]


@attrs.frozen
class PostgresCaseHistory:
    """
    ``CaseHistory`` in Postgres: one row per resolved case, joined to the
    note it was filed under when read.
    """

    database: databases.Database

    async def record(self, *, requester_id: str, memory_id: int) -> None:
        await self.database.execute(
            sa.insert(_CASES).values(
                requester_id=requester_id, memory_id=memory_id, happened_at=sa.func.now()
            )
        )

    async def recent(self, *, requester_id: str, window: timedelta, limit: int) -> list[PastCase]:
        last_at = sa.func.max(_CASES.c.happened_at).label("last_at")
        rows = await self.database.fetch_all(
            sa.select(_CASES.c.memory_id, _NOTES.c.note, sa.func.count().label("times"), last_at)
            .select_from(_CASES.join(_NOTES, _NOTES.c.id == _CASES.c.memory_id))
            .where(
                _CASES.c.requester_id == requester_id,
                # Cast: untyped, Postgres reads now() - $1 as timestamp minus
                # timestamp.
                _CASES.c.happened_at > sa.func.now() - sa.cast(window, sa.Interval),
            )
            .group_by(_CASES.c.memory_id, _NOTES.c.note)
            .order_by(last_at.desc())
            .limit(limit)
        )
        return [
            PastCase(
                memory_id=row["memory_id"],
                note=row["note"],
                times=row["times"],
                last_at=row["last_at"],
            )
            for row in rows
        ]

    async def purge(self, *, cutoff: datetime) -> int:
        rows = await self.database.fetch_all(
            sa.delete(_CASES).where(_CASES.c.happened_at < cutoff).returning(_CASES.c.id)
        )
        return len(rows)
