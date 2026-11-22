"""
Integration tests for the memory store against a real Postgres — its search
is full-text SQL that only Postgres can run.

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


@pytest.fixture
async def store():
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM memory_notes")
    try:
        yield memory.PostgresMemoryStore(database=db)
    finally:
        await db.execute("DELETE FROM memory_notes")
        await db.disconnect()


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
