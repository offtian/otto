import types
from unittest import mock

import agents

from otto import config
from otto.application import flows, graph
from otto.vendors import llm


class FakeIntentClassifier:
    def __init__(self, reading: llm.IntentReading):
        self.reading = reading
        self.texts: list[str] = []

    async def classify(self, *, text: str) -> llm.IntentReading:
        self.texts.append(text)
        return self.reading


def _wire(monkeypatch, *, intent_classifier=None, access_agent=None):
    """
    Stub the wired config with sentinel agents and return (cfg, run_spy)
    where run_spy replaces ``agents.Runner.run``.
    """
    cfg = types.SimpleNamespace(
        agent=mock.sentinel.general_agent,
        access_agent=access_agent,
        intent_classifier=intent_classifier,
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
