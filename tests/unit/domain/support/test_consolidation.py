from datetime import UTC, datetime
from unittest import mock

import agents
import pytest
from agents.models import interface as model_interface

from otto.domain.support import consolidation, memory


TRANSCRIPT = (
    "U_REQ: VPN drops on hotel wifi\notto: Switch the client to TCP mode.\nU_REQ: fixed, thanks"
)
KNOWN_NOTE = memory.Memory(
    id=12,
    note="Symptom: VPN drops on hotel wifi\nFix: switch the client to TCP mode",
    times_seen=2,
    last_seen=datetime(2026, 11, 1, tzinfo=UTC),
)


class TestParseCase:
    def test_reads_a_new_note_past_a_model_thinking_aloud(self):
        # Given an answer that rambles first, then decides and writes a note
        answer = (
            "Let me think about what this adds.\n\n"
            "DECISION: new\n"
            "Symptom: VPN drops on hotel wifi\n"
            "Cause: unknown\n\n"
            "Fix: switch the client to TCP mode\n"
            "Systems: GlobalConnect\n"
        )

        # When it is parsed
        case = consolidation.parse_case(answer, known_ids=set())

        # Then only the lines after the decision are the note
        assert case == memory.CaseNote(
            note=(
                "Symptom: VPN drops on hotel wifi\n"
                "Cause: unknown\n"
                "Fix: switch the client to TCP mode\n"
                "Systems: GlobalConnect"
            )
        )

    def test_reads_which_known_note_the_case_repeats(self):
        # Given an answer naming one of the notes the model was shown
        # When it is parsed
        case = consolidation.parse_case("DECISION: known 12", known_ids={12, 15})

        # Then the case confirms that note and adds nothing
        assert case == memory.CaseNote(restates=12)

    @pytest.mark.parametrize(
        "answer",
        [
            "DECISION: nothing",
            "Symptom: VPN drops. Fix: restart the client.",
            "DECISION: known 99",
            "DECISION: known",
            "DECISION: new",
            "",
        ],
    )
    def test_keeps_nothing_for_any_other_answer(self, answer):
        # Given an answer that declines, states no decision, names a note
        # the model was never shown, or promises a note and writes none
        # When it is parsed
        # Then nothing is remembered
        assert consolidation.parse_case(answer, known_ids={12, 15}) is None


class TestAgentCaseDistiller:
    async def test_shows_the_model_the_known_notes_and_returns_its_decision(self, monkeypatch):
        # Given a model that recognizes the conversation as a known note
        run_spy = mock.AsyncMock(return_value=mock.Mock(final_output="DECISION: known 12"))
        monkeypatch.setattr(agents.Runner, "run", run_spy)
        distiller = consolidation.AgentCaseDistiller(model=mock.Mock(spec=model_interface.Model))

        # When a resolved conversation is distilled
        case = await distiller.distill(transcript=TRANSCRIPT, known=[KNOWN_NOTE])

        # Then the model read the conversation beside the numbered known
        # note, and its decision comes back
        request = run_spy.await_args.args[1]
        assert f"Note 12:\n{KNOWN_NOTE.note}" in request
        assert TRANSCRIPT in request
        assert case == memory.CaseNote(restates=12)

    async def test_fails_closed_on_a_model_error(self, monkeypatch):
        # Given a gateway error during distillation
        monkeypatch.setattr(
            agents.Runner, "run", mock.AsyncMock(side_effect=RuntimeError("gateway down"))
        )
        distiller = consolidation.AgentCaseDistiller(model=mock.Mock(spec=model_interface.Model))

        # When a resolved conversation is distilled
        # Then nothing is remembered — never something nobody vetted
        assert await distiller.distill(transcript=TRANSCRIPT, known=[]) is None
