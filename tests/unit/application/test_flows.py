import pathlib
import types
from datetime import UTC, datetime, timedelta
from unittest import mock

import agents

from otto import config
from otto.application import flows, graph
from otto.domain.support import history as support_history
from otto.domain.support import memory as support_memory
from otto.vendors import llm


class FakeIntentClassifier:
    def __init__(self, reading: llm.IntentReading):
        self.reading = reading
        self.texts: list[str] = []

    async def classify(self, *, text: str) -> llm.IntentReading:
        self.texts.append(text)
        return self.reading


class FakeMemoryStore:
    def __init__(self, memories=(), error=None):
        self.memories = list(memories)
        self.error = error
        self.queries: list[str] = []

    async def search(self, *, query: str, limit: int) -> list[support_memory.Memory]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.memories[:limit]


class FakeCaseHistory:
    def __init__(self, cases=(), error=None):
        self.cases = list(cases)
        self.error = error
        self.asked: list[tuple[str, timedelta, int]] = []

    async def recent(
        self, *, requester_id: str, window: timedelta, limit: int
    ) -> list[support_history.PastCase]:
        self.asked.append((requester_id, window, limit))
        if self.error is not None:
            raise self.error
        return self.cases[:limit]


def _wire(
    monkeypatch, *, intent_classifier=None, access_agent=None, memory=None, case_history=None
):
    """
    Stub the wired config with sentinel agents and return (cfg, run_spy)
    where run_spy replaces ``agents.Runner.run``.
    """
    cfg = types.SimpleNamespace(
        agent=mock.sentinel.general_agent,
        access_agent=access_agent,
        intent_classifier=intent_classifier,
        memory=memory,
        case_history=case_history,
        settings=types.SimpleNamespace(memory_history_days=30),
    )
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    run_spy = mock.AsyncMock(return_value=mock.Mock(interruptions=[]))
    monkeypatch.setattr(agents.Runner, "run", run_spy)
    return cfg, run_spy


def _state(
    *, text: str = "I need snowflake access", runbooks_dir: pathlib.Path | None = None
) -> graph.State:
    # The context carries the runbook directory the agent's tools read; one
    # that does not exist means no runbook is ever hinted.
    ctx = types.SimpleNamespace(
        requester_id="U_REQ", runbooks_dir=runbooks_dir or pathlib.Path("no-runbooks-here")
    )
    return {"text": text, "input": "the input", "ctx": ctx}


class TestSupportGraph:
    async def test_no_classifier_routes_to_the_general_agent(self, monkeypatch):
        # Given a config wired without an intent classifier
        _cfg, run_spy = _wire(monkeypatch, access_agent=mock.sentinel.access_agent)

        # When a request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then the general agent handled it — the pre-routing behavior
        assert run_spy.await_args.args[0] is mock.sentinel.general_agent

    async def test_an_access_reading_routes_to_the_access_agent(self, monkeypatch):
        # Given a classifier that reads the request as an access request
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(llm.IntentReading(intent=llm.INTENT_ACCESS)),
            access_agent=mock.sentinel.access_agent,
        )

        # When the request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then the access agent handled it
        assert run_spy.await_args.args[0] is mock.sentinel.access_agent

    async def test_a_non_access_reading_routes_to_the_general_agent(self, monkeypatch):
        # Given a classifier that reads the request as troubleshooting
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(llm.IntentReading(intent="troubleshooting")),
            access_agent=mock.sentinel.access_agent,
        )

        # When the request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then the general agent handled it
        assert run_spy.await_args.args[0] is mock.sentinel.general_agent

    async def test_a_missing_access_agent_skips_classification_entirely(self, monkeypatch):
        # Given a classifier but no access agent wired
        classifier = FakeIntentClassifier(llm.IntentReading(intent=llm.INTENT_ACCESS))
        _cfg, run_spy = _wire(monkeypatch, intent_classifier=classifier)

        # When the request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then no classification call was spent and the general agent ran
        assert classifier.texts == []
        assert run_spy.await_args.args[0] is mock.sentinel.general_agent

    async def test_an_interrupted_run_suspends_with_the_node_name(self, monkeypatch):
        # Given an access-routed run that pauses on a gated tool
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(llm.IntentReading(intent=llm.INTENT_ACCESS)),
            access_agent=mock.sentinel.access_agent,
        )
        paused = mock.Mock(interruptions=[mock.Mock()])
        run_spy.return_value = paused

        # When the request runs through the support graph
        outcome = await flows.SUPPORT.run(_state())

        # Then it suspends at the access node with the run result as payload
        assert outcome == graph.Suspend(payload=paused, node=flows.ACCESS)

    async def test_resume_applies_the_decision_to_the_paused_run(self, monkeypatch):
        # Given a paused run state and an approving decision
        _cfg, run_spy = _wire(monkeypatch, access_agent=mock.sentinel.access_agent)
        interruption = mock.Mock()
        run_state = mock.Mock(get_interruptions=mock.Mock(return_value=[interruption]))
        monkeypatch.setattr(agents.RunState, "from_string", mock.AsyncMock(return_value=run_state))
        state: graph.State = {
            "text": "I need snowflake access",
            "resume": {"run_state_json": "{}", "approved": True},
            "ctx": mock.sentinel.ctx,
        }

        # When the graph is re-entered at the access node
        await flows.SUPPORT.run(state, entry=flows.ACCESS)

        # Then the interruption was approved and the paused state resumed on
        # the access agent — not restarted from the conversation input
        run_state.approve.assert_called_once_with(interruption)
        run_state.reject.assert_not_called()
        assert run_spy.await_args.args == (mock.sentinel.access_agent, run_state)

    async def test_the_chat_session_rides_the_fresh_run(self, monkeypatch):
        # Given a state carrying the dev chat's conversation session store
        _cfg, run_spy = _wire(monkeypatch)
        state = _state()
        state["session"] = mock.sentinel.session

        # When the request runs through the support graph
        await flows.SUPPORT.run(state)

        # Then the session reached the runner for conversation memory
        assert run_spy.await_args.kwargs["session"] is mock.sentinel.session

    async def test_resume_of_a_denied_decision_rejects_the_interruption(self, monkeypatch):
        # Given a paused run state and a denying decision
        _cfg, _run_spy = _wire(monkeypatch, access_agent=mock.sentinel.access_agent)
        interruption = mock.Mock()
        run_state = mock.Mock(get_interruptions=mock.Mock(return_value=[interruption]))
        monkeypatch.setattr(agents.RunState, "from_string", mock.AsyncMock(return_value=run_state))
        state: graph.State = {
            "text": "I need snowflake access",
            "resume": {"run_state_json": "{}", "approved": False},
            "ctx": mock.sentinel.ctx,
        }

        # When the graph is re-entered at the access node
        await flows.SUPPORT.run(state, entry=flows.ACCESS)

        # Then the interruption was rejected, never approved
        run_state.reject.assert_called_once_with(interruption)
        run_state.approve.assert_not_called()


class TestRunbookHints:
    @staticmethod
    def _runbooks(directory: pathlib.Path) -> pathlib.Path:
        (directory / "vpn.md").write_text(
            "# VPN — connect or fix a broken connection\n", encoding="utf-8"
        )
        return directory

    async def test_a_request_about_a_runbook_names_it_in_the_agents_input(
        self, monkeypatch, tmp_path
    ):
        # Given a runbook directory holding a VPN runbook
        _cfg, run_spy = _wire(monkeypatch)
        state = _state(text="My VPN keeps dropping", runbooks_dir=self._runbooks(tmp_path))

        # When a request about the VPN runs through the support graph
        await flows.SUPPORT.run(state)

        # Then the general agent reads the request followed by the runbook's
        # name, offered as a keyword match it may open
        agent_input = run_spy.await_args.args[1]
        assert agent_input.startswith("the input")
        assert "match this request: vpn." in agent_input
        assert "read_runbook" in agent_input

    async def test_a_request_about_no_runbook_is_left_as_it_was(self, monkeypatch, tmp_path):
        # Given a runbook directory holding a VPN runbook
        _cfg, run_spy = _wire(monkeypatch)
        state = _state(
            text="Where is the holiday calendar?", runbooks_dir=self._runbooks(tmp_path)
        )

        # When a request about something else runs through the support graph
        await flows.SUPPORT.run(state)

        # Then the agent's input is untouched
        assert run_spy.await_args.args[1] == "the input"

    async def test_the_access_agent_is_never_hinted(self, monkeypatch, tmp_path):
        # Given an access-routed request that happens to mention the VPN
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(llm.IntentReading(intent=llm.INTENT_ACCESS)),
            access_agent=mock.sentinel.access_agent,
        )
        state = _state(text="I need VPN access", runbooks_dir=self._runbooks(tmp_path))

        # When it runs through the support graph
        await flows.SUPPORT.run(state)

        # Then the access agent — which has no runbook tools — reads the
        # request alone
        assert run_spy.await_args.args[0] is mock.sentinel.access_agent
        assert run_spy.await_args.args[1] == "the input"


class TestRetrievalGate:
    PAST_CASE = support_memory.Memory(
        id=7,
        note="Symptom: VPN drops on hotel wifi. Fix: switch the client to TCP mode.",
        times_seen=3,
        last_seen=datetime(2026, 11, 20, tzinfo=UTC),
    )

    async def test_an_open_gate_recalls_past_cases_into_the_agents_input(self, monkeypatch):
        # Given a reading that says memory would help, and a memory holding
        # a similar past case
        memory = FakeMemoryStore(memories=[self.PAST_CASE])
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(
                llm.IntentReading(intent="troubleshooting", recall="vpn hotel wifi")
            ),
            memory=memory,
        )

        # When the request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then memory was searched with the reading's keywords, and the agent
        # reads the request followed by the past case — with how often and how
        # recently it held — marked as unverified
        agent_input = run_spy.await_args.args[1]
        assert memory.queries == ["vpn hotel wifi"]
        assert agent_input.startswith("the input")
        assert "Past case (seen 3 times, last on 2026-11-20):" in agent_input
        assert self.PAST_CASE.note in agent_input
        assert "may not apply" in agent_input

    async def test_the_requesters_own_recent_cases_come_first_and_only_once(self, monkeypatch):
        # Given a requester who had the past case twice this month, and a
        # memory whose keyword search finds that note and one other
        other_case = support_memory.Memory(
            id=8,
            note="Symptom: wifi drops in meeting rooms. Fix: forget the network and rejoin.",
            times_seen=1,
            last_seen=datetime(2026, 10, 2, tzinfo=UTC),
        )
        history = FakeCaseHistory(
            cases=[
                support_history.PastCase(
                    memory_id=self.PAST_CASE.id,
                    note=self.PAST_CASE.note,
                    times=2,
                    last_at=datetime(2026, 11, 21, tzinfo=UTC),
                )
            ]
        )
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(llm.IntentReading(recall="vpn hotel wifi")),
            memory=FakeMemoryStore(memories=[self.PAST_CASE, other_case]),
            case_history=history,
        )

        # When the request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then the agent reads the requester's own case first, with how often
        # they had it — asked for by their id, over the configured window —
        # and that note is not repeated among the keyword matches
        agent_input = run_spy.await_args.args[1]
        own_block = agent_input.index("This requester's own recently resolved cases")
        assert history.asked == [("U_REQ", timedelta(days=30), 3)]
        assert "This requester had this case 2 times, last on 2026-11-21:" in agent_input
        assert own_block < agent_input.index("Possibly relevant past cases from memory")
        assert agent_input.count(self.PAST_CASE.note) == 1
        assert other_case.note in agent_input

    async def test_a_history_outage_leaves_the_keyword_recall_in_place(self, monkeypatch):
        # Given a requester history whose store is down, over a working memory
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(llm.IntentReading(recall="vpn hotel wifi")),
            memory=FakeMemoryStore(memories=[self.PAST_CASE]),
            case_history=FakeCaseHistory(error=RuntimeError("database down")),
        )

        # When the request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then the agent still reads what the keyword search found
        agent_input = run_spy.await_args.args[1]
        assert self.PAST_CASE.note in agent_input
        assert "This requester's own" not in agent_input

    async def test_a_closed_gate_leaves_memory_alone(self, monkeypatch):
        # Given a reading that says memory would not help
        memory = FakeMemoryStore(memories=[self.PAST_CASE])
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(llm.IntentReading(intent=llm.INTENT_ACCESS)),
            access_agent=mock.sentinel.access_agent,
            memory=memory,
        )

        # When the request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then memory is never searched and the agent's input is untouched
        assert memory.queries == []
        assert run_spy.await_args.args[1] == "the input"

    async def test_a_memory_outage_never_blocks_the_request(self, monkeypatch):
        # Given an open gate over a memory whose backend is down
        _cfg, run_spy = _wire(
            monkeypatch,
            intent_classifier=FakeIntentClassifier(llm.IntentReading(recall="vpn hotel wifi")),
            memory=FakeMemoryStore(error=RuntimeError("database down")),
        )

        # When the request runs through the support graph
        await flows.SUPPORT.run(_state())

        # Then the agent still runs, on the request alone
        assert run_spy.await_args.args[1] == "the input"
