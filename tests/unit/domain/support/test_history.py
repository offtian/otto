from datetime import UTC, datetime

import pytest

from otto.domain.support import history


class TestDescribe:
    @pytest.mark.parametrize(
        ("times", "heading"),
        [
            (1, "This requester had this case once, last on 2026-11-20:"),
            (3, "This requester had this case 3 times, last on 2026-11-20:"),
        ],
    )
    def test_heads_the_note_with_how_often_and_how_recently_they_had_it(self, times, heading):
        # Given a note a requester's cases were filed under
        case = history.PastCase(
            memory_id=7,
            note="Symptom: VPN drops.\nFix: restart the client.",
            times=times,
            last_at=datetime(2026, 11, 20, 9, 30, tzinfo=UTC),
        )

        # When it is described for an agent
        described = history.describe(case)

        # Then their own history comes first and the note follows, untouched
        assert described == f"{heading}\nSymptom: VPN drops.\nFix: restart the client."
