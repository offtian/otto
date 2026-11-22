from datetime import UTC, datetime

import pytest

from otto.domain.support import memory


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
