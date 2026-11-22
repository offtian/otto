"""
Integration tests for the requester history against a real Postgres — the
grouping, the time window and the cascade are SQL only Postgres can run.

Live: needs `just infra` (compose Postgres) + the migrations applied. Guarded
by RUN_INTEGRATION so the normal test suite and CI skip it. Run via
`just test-integration`.
"""

import os
from datetime import UTC, datetime, timedelta

import databases
import pytest

from otto.data import _dsn
from otto.domain.support import history, memory


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="needs Postgres — run via `just test-integration`",
)

DB_URL = _dsn.to_libpq(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/otto")
)

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)
DISTANT_PAST = datetime(2000, 1, 1, tzinfo=UTC)
A_MONTH = timedelta(days=30)

VPN_NOTE = "Symptom: VPN drops on hotel wifi\nFix: switch the client to TCP mode"
PRINTER_NOTE = "Symptom: print queue stuck\nFix: reinstall the driver"


@pytest.fixture
async def stores():
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM memory_notes")
    try:
        yield history.PostgresCaseHistory(database=db), memory.PostgresMemoryStore(database=db)
    finally:
        # Deleting the notes takes their requester cases with them.
        await db.execute("DELETE FROM memory_notes")
        await db.disconnect()


class TestPostgresCaseHistory:
    async def test_returns_each_of_a_requesters_notes_once_with_how_often_they_had_it(
        self, stores
    ):
        # Given a requester who had the VPN case twice and the printer case
        # once, and somebody else who had the printer case too
        cases, notes = stores
        vpn_id = await notes.remember(note=VPN_NOTE)
        printer_id = await notes.remember(note=PRINTER_NOTE)
        await cases.record(requester_id="U_REQ", memory_id=printer_id)
        await cases.record(requester_id="U_REQ", memory_id=vpn_id)
        await cases.record(requester_id="U_REQ", memory_id=vpn_id)
        await cases.record(requester_id="U_OTHER", memory_id=printer_id)

        # When the requester's recent cases are read
        recent = await cases.recent(requester_id="U_REQ", window=A_MONTH, limit=5)

        # Then each note comes back once, the one they had last first, with
        # how many times — and nothing of anybody else's
        assert [(case.memory_id, case.note, case.times) for case in recent] == [
            (vpn_id, VPN_NOTE, 2),
            (printer_id, PRINTER_NOTE, 1),
        ]

    async def test_returns_nothing_from_before_the_window(self, stores):
        # Given a case recorded just now
        cases, notes = stores
        await cases.record(requester_id="U_REQ", memory_id=await notes.remember(note=VPN_NOTE))

        # When recent cases are read over a window that ended before it
        recent = await cases.recent(requester_id="U_REQ", window=timedelta(0), limit=5)

        # Then it is not among them
        assert recent == []

    async def test_purges_only_what_is_older_than_the_cutoff(self, stores):
        # Given a case recorded just now
        cases, notes = stores
        await cases.record(requester_id="U_REQ", memory_id=await notes.remember(note=VPN_NOTE))

        # When the history is purged as of long ago, then as of the far future
        spared = await cases.purge(cutoff=DISTANT_PAST)
        gone = await cases.purge(cutoff=FAR_FUTURE)

        # Then the first purge leaves it and the second removes it
        assert (spared, gone) == (0, 1)
        assert await cases.recent(requester_id="U_REQ", window=A_MONTH, limit=5) == []

    async def test_a_forgotten_note_takes_its_cases_with_it(self, stores):
        # Given a requester's case filed under a note
        cases, notes = stores
        await cases.record(requester_id="U_REQ", memory_id=await notes.remember(note=VPN_NOTE))

        # When memory forgets that note
        await notes.purge(cutoff=FAR_FUTURE)

        # Then nothing is left that says the requester ever had it
        assert await cases.recent(requester_id="U_REQ", window=A_MONTH, limit=5) == []
