"""
Long-term memory: what Otto has learned from the cases it resolved, and how
it gets that back. Three kinds, after waku-agent's split:

  semantic    Case notes — a de-identified "what was wrong, why, and what
              fixed it", distilled from a resolved conversation. One row
              each in ``memory_notes``, found by keyword with Postgres
              full-text search — no model call, and the whole memory can be
              read with one SELECT — and, when an embedding model is
              configured, also by meaning.
  episodic    When, and how often. Every note carries how many resolved cases
              have matched it and when the last one did. A second case with
              the same fix confirms the note instead of adding a copy, and a
              note nothing has confirmed for the retention window is
              forgotten. Whose cases they were is kept apart, and only if
              asked for (``history``).
  procedural  How to act — the runbooks, read on demand through the runbook
              tools (``agent``); ``runbooks`` tells the agent which ones a
              request looks to be about. Not stored here.

Memory goes in by consolidation (``consolidation``): on a confirmed
resolution the conversation is distilled into a note, or matched to one
already known. It comes out through the retrieval gate before a run
(``application.flows``) or the ``search_memory`` tool during one.

Search has two halves. Keyword search always runs. Search by meaning runs
only with an ``Embedder`` wired: the query is embedded, the notes nearest to
it are found with pgvector, and the two rankings are fused. Meaning is an
assist to keywords, never a replacement: without an embedder, when the model
cannot be reached, or on a database without pgvector, search is keyword
search and nothing else changes.

``MemoryStore`` is the port; ``PostgresMemoryStore`` is its one implementation
today. Nothing wired means "no memory here", and everything degrades to
running without it.

ponytail: nearest notes are found by comparing the query with every embedded
note. Give the column a fixed length and an HNSW index when there are more
notes than that is quick for (tens of thousands).
"""

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import attrs
import databases
import sqlalchemy as sa

from otto.data import models
from otto.utils import logs


# The most distinct words of a query that are searched for: enough for an
# opening message, few enough that a pasted log is not searched word by word.
_MAX_TERMS = 32
# Reciprocal rank fusion's constant — how much a first place outweighs a
# tenth. 60 is the value the method was published with.
_FUSION_CONSTANT = 60


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


def fuse(*rankings: Sequence[Memory], limit: int) -> list[Memory]:
    """
    Return the best ``limit`` memories of several rankings as one ranking,
    by reciprocal rank fusion: a memory scores ``1 / (60 + its place)`` in
    every ranking it appears in. One found two ways outranks one found a
    single way, and a keyword rank is never compared with a distance — only
    places are.
    """
    scores: dict[int, float] = {}
    found: dict[int, Memory] = {}
    for ranking in rankings:
        for place, memory in enumerate(ranking, start=1):
            scores[memory.id] = scores.get(memory.id, 0.0) + 1 / (_FUSION_CONSTANT + place)
            found.setdefault(memory.id, memory)
    best = sorted(found, key=lambda memory_id: -scores[memory_id])
    return [found[memory_id] for memory_id in best[:limit]]


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
        Return up to ``limit`` notes that match ``query``, best first: those
        sharing its words and, where the store can tell, those meaning the
        same. A query with nothing to search for, or that nothing matches,
        is an empty list — never "the most recent ones".
        """
        ...

    async def purge(self, *, cutoff: datetime) -> int:
        """
        Forget every note last seen before ``cutoff`` and return how many
        were forgotten.
        """
        ...

    async def reindex(self, *, limit: int) -> int:
        """
        Bring up to ``limit`` notes that are missing from the store's search
        by meaning into it, and return how many were. A store with nothing
        to catch up on returns 0.
        """
        ...


class Embedder(Protocol):
    """
    Turns text into vectors that are close together when the texts mean
    similar things (an embedding model on the gateway, ``vendors/llm``).
    """

    async def embed(self, *, texts: Sequence[str]) -> list[list[float]]:
        """
        Return one vector per text, in the order given, all of one length.
        May raise or take a while — the store treats it as an assist.
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

# What a search hands back — never the embedding itself.
_COLUMNS = (_TABLE.c.id, _TABLE.c.note, _TABLE.c.times_seen, _TABLE.c.last_seen)

# The text-search configuration, written into the SQL as a literal and not a
# bound parameter: the planner uses the full-text index only for the exact
# expression the index was built on, constant included.
_LANGUAGE: sa.ColumnElement[Any] = sa.literal_column("'english'")


@attrs.frozen
class PostgresMemoryStore:
    """
    ``MemoryStore`` in Postgres. By keyword it is full-text search: a query
    is its words OR-ed together and ranked, so a note needs to share only
    some of them, and ``websearch_to_tsquery`` never raises on what it is
    given. By meaning — only with an ``embedder`` — it is pgvector's cosine
    distance between the query and each note.
    """

    database: databases.Database
    # Adds search by meaning to search by keyword. None = keyword only.
    embedder: Embedder | None = None
    # How far (cosine distance: 0 the same meaning, 1 unrelated) a note may
    # be from a query and still be recalled on meaning alone. Without it the
    # nearest note would always come back, however far. Depends on the
    # embedding model: calibrate it.
    max_distance: float = 0.5

    async def remember(self, *, note: str) -> int:
        values: dict[str, Any] = {
            "note": note,
            "times_seen": 1,
            "first_seen": sa.func.now(),
            "last_seen": sa.func.now(),
        }
        # A note is kept even when it cannot be embedded right now: it is
        # found by keyword at once, and ``reindex`` embeds it later.
        vector = await self._embedded(note)
        if vector is not None:
            values["embedding"] = _as_vector(vector)
        memory_id = await self.database.fetch_val(
            sa.insert(_TABLE).values(**values).returning(_TABLE.c.id)
        )
        return int(memory_id)

    async def confirm(self, memory_id: int) -> None:
        await self.database.execute(
            sa.update(_TABLE)
            .where(_TABLE.c.id == memory_id)
            .values(times_seen=_TABLE.c.times_seen + 1, last_seen=sa.func.now())
        )

    async def search(self, *, query: str, limit: int) -> list[Memory]:
        if not search_terms(query):
            return []
        by_keyword = await self._by_keyword(query=query, limit=limit)
        by_meaning = await self._by_meaning(query=query, limit=limit)
        return fuse(by_keyword, by_meaning, limit=limit)

    async def purge(self, *, cutoff: datetime) -> int:
        rows = await self.database.fetch_all(
            sa.delete(_TABLE).where(_TABLE.c.last_seen < cutoff).returning(_TABLE.c.id)
        )
        return len(rows)

    async def reindex(self, *, limit: int) -> int:
        if self.embedder is None:
            return 0
        rows = await self.database.fetch_all(
            sa.select(_TABLE.c.id, _TABLE.c.note)
            .where(_TABLE.c.embedding.is_(None))
            .order_by(_TABLE.c.id)
            .limit(limit)
        )
        if not rows:
            return 0
        vectors = await self.embedder.embed(texts=[row["note"] for row in rows])
        for row, vector in zip(rows, vectors, strict=True):
            await self.database.execute(
                sa.update(_TABLE)
                .where(_TABLE.c.id == row["id"])
                .values(embedding=_as_vector(vector))
            )
        return len(rows)

    async def _by_keyword(self, *, query: str, limit: int) -> list[Memory]:
        document = sa.func.to_tsvector(_LANGUAGE, _TABLE.c.note)
        wanted = sa.func.websearch_to_tsquery(_LANGUAGE, " or ".join(search_terms(query)))
        rows = await self.database.fetch_all(
            sa.select(*_COLUMNS)
            .where(document.op("@@")(wanted))
            .order_by(sa.func.ts_rank(document, wanted).desc(), _TABLE.c.last_seen.desc())
            .limit(limit)
        )
        return [_memory(row) for row in rows]

    async def _by_meaning(self, *, query: str, limit: int) -> list[Memory]:
        """
        Return the notes nearest in meaning to the query, within
        ``max_distance`` — none without an embedder, and none when this half
        of the search fails for any reason: keyword search still answers.
        """
        vector = await self._embedded(query)
        if vector is None:
            return []
        # Inside a CASE, so a note embedded by another model (a different
        # length, which pgvector refuses to compare) is skipped, not an error.
        distance = sa.case(
            (
                sa.func.vector_dims(_TABLE.c.embedding) == len(vector),
                _TABLE.c.embedding.op("<=>")(_as_vector(vector)),
            )
        ).label("distance")
        embedded = sa.select(*_COLUMNS, distance).where(_TABLE.c.embedding.is_not(None)).subquery()
        try:
            rows = await self.database.fetch_all(
                sa.select(
                    embedded.c.id, embedded.c.note, embedded.c.times_seen, embedded.c.last_seen
                )
                .where(embedded.c.distance <= self.max_distance)
                .order_by(embedded.c.distance)
                .limit(limit)
            )
        except Exception as exc:
            logs.log_exception(exc, params={"job": "memory_search_by_meaning"})
            return []
        return [_memory(row) for row in rows]

    async def _embedded(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            (vector,) = await self.embedder.embed(texts=[text])
        except Exception as exc:
            logs.log_exception(exc, params={"job": "memory_embedding"})
            return None
        return vector


def _memory(row: Any) -> Memory:
    return Memory(
        id=row["id"], note=row["note"], times_seen=row["times_seen"], last_seen=row["last_seen"]
    )


def _as_vector(vector: Sequence[float]) -> sa.ColumnElement[Any]:
    """
    Return a vector as the SQL value pgvector reads: its text form, sent as
    text and cast in the statement — so the driver needs to know nothing
    about the type.
    """
    text = "[" + ",".join(repr(float(number)) for number in vector) + "]"
    return sa.cast(sa.cast(text, sa.Text), models.Vector())
