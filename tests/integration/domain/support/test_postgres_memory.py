"""
Integration tests for the memory store against a real Postgres — its search
is full-text and pgvector SQL that only Postgres can run.

Live: needs `just infra` (compose Postgres) + the migrations applied. Guarded
by RUN_INTEGRATION so the normal test suite and CI skip it. Run via
`just test-integration`.
"""

import os
from datetime import UTC, datetime

import databases
import pytest

from otto.data import _dsn
from otto.domain.support import memory


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="needs Postgres — run via `just test-integration`",
)

DB_URL = _dsn.to_libpq(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/otto")
)

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)
DISTANT_PAST = datetime(2000, 1, 1, tzinfo=UTC)

VPN_NOTE = (
    "Symptom: VPN drops on hotel wifi\n"
    "Cause: the captive portal blocks UDP\n"
    "Fix: switch the GlobalConnect client to TCP mode\n"
    "Systems: GlobalConnect VPN"
)
WIFI_NOTE = (
    "Symptom: laptop will not join the hotel wifi\n"
    "Cause: unknown\n"
    "Fix: forget the network and join it again\n"
    "Systems: wifi"
)
PRINTER_NOTE = (
    "Symptom: print queue stuck\n"
    "Cause: stale driver\n"
    "Fix: reinstall the driver from Self Service\n"
    "Systems: printing"
)


class TopicEmbedder:
    """
    A stand-in for an embedding model, with one axis per topic: a text points
    along the axis of the topic it mentions. Two texts on one topic are at
    distance 0, and texts on different topics at distance 1.
    """

    TOPICS = (("vpn", "tunnel"), ("print", "paper"))

    async def embed(self, *, texts):
        return [self._vector(text.lower()) for text in texts]

    def _vector(self, text):
        for axis, words in enumerate(self.TOPICS):
            if any(word in text for word in words):
                return [1.0 if place == axis else 0.0 for place in range(3)]
        return [0.0, 0.0, 1.0]


class TwoAxisEmbedder:
    """
    Another model: its vectors are a different length.
    """

    async def embed(self, *, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
async def database():
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM memory_notes")
    try:
        yield db
    finally:
        await db.execute("DELETE FROM memory_notes")
        await db.disconnect()


@pytest.fixture
def store(database):
    return memory.PostgresMemoryStore(database=database)


@pytest.fixture
def store_by_meaning(database):
    return memory.PostgresMemoryStore(database=database, embedder=TopicEmbedder())


class TestPostgresMemoryStore:
    async def test_finds_a_note_in_the_requesters_own_words(self, store):
        # Given a VPN note and an unrelated one
        vpn_id = await store.remember(note=VPN_NOTE)
        await store.remember(note=PRINTER_NOTE)

        # When memory is searched with other forms of the note's words, and
        # punctuation that would be search syntax if it were let through
        found = await store.search(query='my "VPN" keeps dropping -- on HOTEL wifi!', limit=5)

        # Then only the VPN note comes back, as first remembered
        assert [
            (found_note.id, found_note.note, found_note.times_seen) for found_note in found
        ] == [(vpn_id, VPN_NOTE, 1)]

    async def test_ranks_the_note_sharing_more_of_the_query_first(self, store):
        # Given two notes about hotel wifi, one of them about the VPN on it
        await store.remember(note=WIFI_NOTE)
        vpn_id = await store.remember(note=VPN_NOTE)

        # When memory is searched for the VPN problem, for one note
        found = await store.search(query="vpn drops hotel wifi", limit=1)

        # Then the note sharing more of the query's words wins the one place
        assert [found_note.id for found_note in found] == [vpn_id]

    async def test_a_query_with_nothing_to_search_for_finds_nothing(self, store):
        # Given a remembered note
        await store.remember(note=VPN_NOTE)

        # When memory is searched with no words at all, and with only words
        # too common for Postgres to index
        wordless = await store.search(query="?!", limit=5)
        too_common = await store.search(query="it is the", limit=5)

        # Then neither search falls back to handing over what it has
        assert wordless == []
        assert too_common == []

    async def test_a_confirmed_note_counts_another_sighting(self, store):
        # Given a note seen once
        note_id = await store.remember(note=VPN_NOTE)

        # When another resolved case confirms it
        await store.confirm(note_id)

        # Then it has been seen twice
        (found,) = await store.search(query="vpn", limit=5)
        assert found.times_seen == 2

    async def test_forgets_only_what_was_last_seen_before_the_cutoff(self, store):
        # Given a note seen just now
        await store.remember(note=VPN_NOTE)

        # When memory is purged as of long ago, then as of the far future
        spared = await store.purge(cutoff=DISTANT_PAST)
        forgotten = await store.purge(cutoff=FAR_FUTURE)

        # Then the first purge leaves it and the second forgets it
        assert (spared, forgotten) == (0, 1)
        assert await store.search(query="vpn", limit=5) == []


class TestPostgresMemoryStoreByMeaning:
    async def test_finds_a_note_that_shares_no_word_with_the_request(self, store_by_meaning):
        # Given a note about the VPN
        vpn_id = await store_by_meaning.remember(note=VPN_NOTE)

        # When memory is searched in words the note does not use, about the
        # same thing
        found = await store_by_meaning.search(query="the tunnel keeps collapsing", limit=5)

        # Then the note is found on meaning alone
        assert [found_note.id for found_note in found] == [vpn_id]

    async def test_recalls_nothing_on_meaning_from_too_far_away(self, store_by_meaning):
        # Given a note about the VPN
        await store_by_meaning.remember(note=VPN_NOTE)

        # When memory is searched about something else, in other words
        found = await store_by_meaning.search(query="a paper jam", limit=5)

        # Then the nearest note is still too far to be handed over
        assert found == []

    async def test_a_note_another_model_embedded_is_passed_over_not_an_error(
        self, database, store_by_meaning
    ):
        # Given a note embedded by a model whose vectors are another length
        other_model = memory.PostgresMemoryStore(database=database, embedder=TwoAxisEmbedder())
        vpn_id = await other_model.remember(note=VPN_NOTE)

        # When memory is searched by words it shares, and by words it does not
        by_keyword = await store_by_meaning.search(query="vpn tunnel", limit=5)
        by_meaning_only = await store_by_meaning.search(query="tunnel collapsing", limit=5)

        # Then keywords still find it, and meaning quietly does not
        assert [found_note.id for found_note in by_keyword] == [vpn_id]
        assert by_meaning_only == []

    async def test_reindex_brings_in_a_note_remembered_without_an_embedding(
        self, store, store_by_meaning
    ):
        # Given a note remembered while there was no embedding model
        vpn_id = await store.remember(note=VPN_NOTE)

        # When the index is caught up, twice
        first = await store_by_meaning.reindex(limit=20)
        second = await store_by_meaning.reindex(limit=20)

        # Then the note was embedded once, and is now found on meaning alone
        found = await store_by_meaning.search(query="the tunnel keeps collapsing", limit=5)
        assert (first, second) == (1, 0)
        assert [found_note.id for found_note in found] == [vpn_id]
