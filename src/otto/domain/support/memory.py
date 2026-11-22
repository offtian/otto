"""
Long-term memory: what Otto has learned from the cases it resolved, and how
it gets that back. Three kinds, after waku-agent's split:

  semantic    Case notes — a de-identified "what was wrong, why, and what
              fixed it", distilled from a resolved conversation. One row
              each in ``memory_notes``, found by keyword with Postgres
              full-text search: no embeddings, no model call to search, and
              the whole memory can be read with one SELECT.
  episodic    When, and how often. Every note carries how many resolved cases
              have matched it and when the last one did. A second case with
              the same fix confirms the note instead of adding a copy, and a
              note nothing has confirmed for the retention window is
              forgotten.
  procedural  How to act — the runbooks, read on demand through the runbook
              tools (``agent``). Not stored here.

Memory goes in by consolidation (``consolidation``): on a confirmed
resolution the conversation is distilled into a note, or matched to one
already known. It comes out through the retrieval gate before a run
(``application.flows``) or the ``search_memory`` tool during one.

``MemoryStore`` is the port; ``PostgresMemoryStore`` is its one implementation
today. Nothing wired means "no memory here", and everything degrades to
running without it.

ponytail: keyword search only — a case worded with none of a note's words is
not found, and neither is a word that a note holds only inside a hostname, a
URL or a path, which Postgres indexes whole. Add a pgvector column and blend
its distance into the ranking when recall misses things a person would have
matched.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import attrs
import databases
import sqlalchemy as sa

from otto.data import models


# The most distinct words of a query that are searched for: enough for an
# opening message, few enough that a pasted log is not searched word by word.
_MAX_TERMS = 32


@attrs.frozen
class Memory:
    """
    One remembered case note, with its episodic side.
    """

    id: int
    note: str
    # How many resolved cases have matched this note, the first included.
    times_seen: int
    last_seen: datetime


@attrs.frozen
class CaseNote:
    """
    What consolidation made of one resolved conversation: a new note, or the
    known memory the case repeats.
    """

    note: str = ""
    # The id of the known memory this case confirms; None = the note is new.
    restates: int | None = None


def describe(memory: Memory) -> str:
    """
    Return a memory as an agent reads it: the note, headed by how often and
    how recently it held — a fix seen once a year ago and one seen five times
    this week deserve different weight.
    """
    seen = "seen once" if memory.times_seen == 1 else f"seen {memory.times_seen} times"
    return f"Past case ({seen}, last on {memory.last_seen:%Y-%m-%d}):\n{memory.note}"


def search_terms(text: str) -> list[str]:
    """
    Return the distinct words of ``text`` to search memory for, in order,
    lowercased. Words, not the text itself, so nothing in a request can be
    read as search syntax; an empty list means there is nothing to search
    for, and a store must answer that with no memories — never with "the
    most recent ones".
    """
    words = re.findall(r"[^\W_]{2,}", text.lower())
    return list(dict.fromkeys(words))[:_MAX_TERMS]


class MemoryStore(Protocol):
    """
    Case notes and their history. Every method may raise on a store outage;
    callers treat memory as an assist and carry on without it.
    """

    async def remember(self, *, note: str) -> int:
        """
        Keep a new case note, seen once, now. Return its id.
        """
        ...

    async def confirm(self, memory_id: int) -> None:
        """
        Record that another resolved case matched a known note: one more
        sighting, last seen now. An unknown id is ignored.
        """
        ...

    async def search(self, *, query: str, limit: int) -> list[Memory]:
        """
        Return up to ``limit`` notes sharing words with ``query``, best match
        first and the more recently seen first among equals. No shared word,
        or no searchable word in the query, is an empty list.
        """
        ...

    async def purge(self, *, cutoff: datetime) -> int:
        """
        Forget every note last seen before ``cutoff`` and return how many
        were forgotten.
        """
        ...


class CaseDistiller(Protocol):
    """
    Consolidation: decides what of a resolved conversation is worth keeping.
    """

    async def distill(self, *, transcript: str, known: Sequence[Memory]) -> CaseNote | None:
        """
        Return what to remember of a resolved conversation — a new
        de-identified case note, or which of the ``known`` memories it
        repeats — or None when it holds nothing reusable.

        Never raises: a failed distillation is also None, so nothing unvetted
        is ever remembered.
        """
        ...


# SQLModel exposes the SQLAlchemy Table at runtime; mypy needs the hint.
_TABLE: sa.Table = models.MemoryNoteRecord.__table__  # type: ignore[attr-defined]

# The text-search configuration, written into the SQL as a literal and not a
# bound parameter: the planner uses the full-text index only for the exact
# expression the index was built on, constant included.
_LANGUAGE: sa.ColumnElement[Any] = sa.literal_column("'english'")


@attrs.frozen
class PostgresMemoryStore:
    """
    ``MemoryStore`` over Postgres full-text search. A query is its words
    OR-ed together and ranked, so a note needs to share only some of them —
    and ``websearch_to_tsquery`` never raises on what it is given.
    """

    database: databases.Database

    async def remember(self, *, note: str) -> int:
        memory_id = await self.database.fetch_val(
            sa.insert(_TABLE)
            .values(note=note, times_seen=1, first_seen=sa.func.now(), last_seen=sa.func.now())
            .returning(_TABLE.c.id)
        )
        return int(memory_id)

    async def confirm(self, memory_id: int) -> None:
        await self.database.execute(
            sa.update(_TABLE)
            .where(_TABLE.c.id == memory_id)
            .values(times_seen=_TABLE.c.times_seen + 1, last_seen=sa.func.now())
        )

    async def search(self, *, query: str, limit: int) -> list[Memory]:
        terms = search_terms(query)
        if not terms:
            return []
        document = sa.func.to_tsvector(_LANGUAGE, _TABLE.c.note)
        wanted = sa.func.websearch_to_tsquery(_LANGUAGE, " or ".join(terms))
        rows = await self.database.fetch_all(
            sa.select(_TABLE)
            .where(document.op("@@")(wanted))
            .order_by(sa.func.ts_rank(document, wanted).desc(), _TABLE.c.last_seen.desc())
            .limit(limit)
        )
        return [
            Memory(
                id=row["id"],
                note=row["note"],
                times_seen=row["times_seen"],
                last_seen=row["last_seen"],
            )
            for row in rows
        ]

    async def purge(self, *, cutoff: datetime) -> int:
        rows = await self.database.fetch_all(
            sa.delete(_TABLE).where(_TABLE.c.last_seen < cutoff).returning(_TABLE.c.id)
        )
        return len(rows)
