from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from otto.domain.support import memory


SEEN = datetime(2026, 11, 20, tzinfo=UTC)
VPN_ROW = {"id": 1, "note": "Symptom: VPN drops.", "times_seen": 2, "last_seen": SEEN}
VPN_NOTE = memory.Memory(id=1, note="Symptom: VPN drops.", times_seen=2, last_seen=SEEN)


def _note(memory_id: int) -> memory.Memory:
    return memory.Memory(id=memory_id, note=f"note {memory_id}", times_seen=1, last_seen=SEEN)


class FakeEmbedder:
    """
    Answers every text with the same vector, or fails, and records what it
    was asked to embed.
    """

    def __init__(self, error=None):
        self.error = error
        self.asked = []

    async def embed(self, *, texts):
        self.asked.append(list(texts))
        if self.error is not None:
            raise self.error
        return [[0.25, 0.5] for _ in texts]


class FakeDatabase:
    """
    Stands in for ``databases.Database``: records each statement as the SQL
    Postgres would be sent, and answers reads from a script, in order — an
    exception in the script is raised instead.
    """

    def __init__(self, reads=()):
        self.reads = list(reads)
        self.sent = []

    def _record(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        self.sent.append((" ".join(str(compiled).split()), dict(compiled.params)))

    async def fetch_val(self, statement):
        self._record(statement)
        return 7

    async def execute(self, statement):
        self._record(statement)

    async def fetch_all(self, statement):
        self._record(statement)
        answer = self.reads.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestSearchTerms:
    @pytest.mark.parametrize(
        ("text", "terms"),
        [
            ("VPN drops on hotel Wi-Fi", ["vpn", "drops", "on", "hotel", "wi", "fi"]),
            ("vpn VPN Vpn timeout", ["vpn", "timeout"]),
            ('"quoted" -negated (grouped) a:b & c | d', ["quoted", "negated", "grouped"]),
            ("Müller kann sich nicht anmelden", ["müller", "kann", "sich", "nicht", "anmelden"]),
            ("?! — :)", []),
            ("", []),
        ],
    )
    def test_returns_the_distinct_words_and_nothing_that_reads_as_syntax(self, text, terms):
        # Given text as a requester or a model wrote it
        # When it is reduced to what memory is searched for
        # Then only its words are left, lowercased, each once, in order
        assert memory.search_terms(text) == terms

    def test_searches_a_long_text_by_its_first_words_only(self):
        # Given a pasted log of a thousand distinct words
        log = " ".join(f"word{number}" for number in range(1000))

        # When it is reduced to search terms
        terms = memory.search_terms(log)

        # Then the search is bounded, and starts from the top of the text
        assert len(terms) == 32
        assert terms[0] == "word0"


class TestDescribe:
    @pytest.mark.parametrize(
        ("times_seen", "heading"),
        [
            (1, "Past case (seen once, last on 2026-11-20):"),
            (3, "Past case (seen 3 times, last on 2026-11-20):"),
        ],
    )
    def test_heads_the_note_with_how_often_and_how_recently_it_held(self, times_seen, heading):
        # Given a remembered note and its history
        recalled = memory.Memory(
            id=7,
            note="Symptom: VPN drops.\nFix: restart the client.",
            times_seen=times_seen,
            last_seen=datetime(2026, 11, 20, 9, 30, tzinfo=UTC),
        )

        # When it is described for an agent
        described = memory.describe(recalled)

        # Then the history comes first and the note follows, untouched
        assert described == f"{heading}\nSymptom: VPN drops.\nFix: restart the client."


class TestFuse:
    def test_a_note_found_both_ways_outranks_one_found_a_single_way(self):
        # Given a keyword ranking and a ranking by meaning that share one note
        by_keyword = [_note(1), _note(2)]
        by_meaning = [_note(3), _note(2)]

        # When they are fused
        fused = memory.fuse(by_keyword, by_meaning, limit=5)

        # Then the shared note leads, each note appears once, and the rest
        # keep their places — keyword first among equals
        assert [found.id for found in fused] == [2, 1, 3]

    def test_keeps_no_more_than_the_limit_and_one_ranking_as_it_is(self):
        # Given a single ranking — nothing was found the other way
        by_keyword = [_note(1), _note(2), _note(3)]

        # When it is fused for two notes
        fused = memory.fuse(by_keyword, [], limit=2)

        # Then it is the same ranking, cut to the limit
        assert [found.id for found in fused] == [1, 2]


class TestPostgresMemoryStore:
    async def test_a_note_is_kept_with_its_embedding(self):
        # Given a store with an embedder
        database = FakeDatabase()
        store = memory.PostgresMemoryStore(database=database, embedder=FakeEmbedder())

        # When a note is remembered
        memory_id = await store.remember(note="Symptom: VPN drops.")

        # Then one row goes in with the vector, sent as text and cast in SQL
        ((sql, params),) = database.sent
        assert memory_id == 7
        assert "CAST(CAST(%(param_1)s AS TEXT) AS vector)" in sql
        assert params["param_1"] == "[0.25,0.5]"

    async def test_a_note_is_still_kept_when_it_cannot_be_embedded(self):
        # Given a store whose embedding model cannot be reached
        database = FakeDatabase()
        embedder = FakeEmbedder(error=RuntimeError("gateway down"))
        store = memory.PostgresMemoryStore(database=database, embedder=embedder)

        # When a note is remembered
        await store.remember(note="Symptom: VPN drops.")

        # Then the note goes in without an embedding — keyword search finds
        # it at once, and reindex embeds it later
        ((sql, _),) = database.sent
        assert sql.startswith("INSERT INTO memory_notes")
        assert "embedding" not in sql

    async def test_search_fuses_what_keywords_and_meaning_each_find(self):
        # Given a store whose keyword search finds one note and whose search
        # by meaning finds another
        other = {"id": 2, "note": "Symptom: tunnel collapses.", "times_seen": 1, "last_seen": SEEN}
        database = FakeDatabase(reads=[[VPN_ROW], [other]])
        store = memory.PostgresMemoryStore(database=database, embedder=FakeEmbedder())

        # When memory is searched
        found = await store.search(query="vpn drops", limit=3)

        # Then both come back, and the second statement compared vectors
        # within the configured distance
        by_meaning_sql, by_meaning_params = database.sent[1]
        assert [note.id for note in found] == [1, 2]
        assert "<=>" in by_meaning_sql
        assert 0.5 in by_meaning_params.values()

    @pytest.mark.parametrize(
        ("embedder", "reads"),
        [
            (None, [[VPN_ROW]]),
            (FakeEmbedder(error=RuntimeError("gateway down")), [[VPN_ROW]]),
            (FakeEmbedder(), [[VPN_ROW], RuntimeError("type vector does not exist")]),
        ],
        ids=["no embedder", "the model cannot be reached", "the database has no pgvector"],
    )
    async def test_search_is_keyword_search_whenever_meaning_is_not_to_be_had(
        self, embedder, reads
    ):
        # Given a store that cannot search by meaning, for one reason or another
        store = memory.PostgresMemoryStore(database=FakeDatabase(reads=reads), embedder=embedder)

        # When memory is searched
        found = await store.search(query="vpn drops", limit=3)

        # Then what the keywords found comes back, and nothing is raised
        assert found == [VPN_NOTE]

    async def test_a_query_with_nothing_to_search_for_asks_nobody(self):
        # Given a store with an embedder
        database, embedder = FakeDatabase(), FakeEmbedder()
        store = memory.PostgresMemoryStore(database=database, embedder=embedder)

        # When memory is searched with no words at all
        found = await store.search(query="?!", limit=3)

        # Then nothing is found, embedded or read — never "the nearest note"
        assert (found, embedder.asked, database.sent) == ([], [], [])

    async def test_reindex_embeds_the_notes_that_have_no_embedding(self):
        # Given two notes remembered while there was no embedding to be had
        pending = [{"id": 3, "note": "note three"}, {"id": 4, "note": "note four"}]
        database, embedder = FakeDatabase(reads=[pending]), FakeEmbedder()
        store = memory.PostgresMemoryStore(database=database, embedder=embedder)

        # When the index is caught up
        reindexed = await store.reindex(limit=20)

        # Then both were embedded in one request and each got its vector
        updates = [sql for sql, _ in database.sent if sql.startswith("UPDATE memory_notes")]
        assert reindexed == 2
        assert embedder.asked == [["note three", "note four"]]
        assert len(updates) == 2

    async def test_reindex_does_nothing_without_an_embedder(self):
        # Given a keyword-only store
        database = FakeDatabase()
        store = memory.PostgresMemoryStore(database=database)

        # When the index is caught up
        # Then there is nothing to catch up on, and the database is not asked
        assert await store.reindex(limit=20) == 0
        assert database.sent == []
