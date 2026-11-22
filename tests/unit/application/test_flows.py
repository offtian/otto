import types
from datetime import UTC, datetime
from unittest import mock

import agents

from otto import config
from otto.application import flows, graph
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


def _wire(monkeypatch, *, intent_classifier=None, access_agent=None, memory=None):
    """
    Stub the wired config with sentinel agents and return (cfg, run_spy)
    where run_spy replaces ``agents.Runner.run``.
    """
    cfg = types.SimpleNamespace(
        agent=mock.sentinel.general_agent,
        access_agent=access_agent,
        intent_classifier=intent_classifier,
        memory=memory,
    )
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    run_spy = mock.AsyncMock(return_value=mock.Mock(interruptions=[]))
    monkeypatch.setattr(agents.Runner, "run", run_spy)
    return cfg, run_spy


def _state() -> graph.State:
    return {"text": "I need snowflake access", "input": "the input", "ctx": mock.sentinel.ctx}


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
