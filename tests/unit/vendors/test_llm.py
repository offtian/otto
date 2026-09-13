from unittest import mock

from otto.vendors import llm


TEAMS = ("Network", "Identity & Access", "Endpoint")


def _classifier(reply: str | None = None, error: Exception | None = None) -> llm.LLMTeamClassifier:
    client = mock.AsyncMock()
    if error is not None:
        client.chat.completions.create.side_effect = error
    else:
        response = mock.Mock()
        response.choices = [mock.Mock(message=mock.Mock(content=reply))]
        client.chat.completions.create.return_value = response
    return llm.LLMTeamClassifier(client=client, model_name="test-model", teams=TEAMS)


class TestLLMTeamClassifier:
    async def test_returns_the_team_the_model_names(self):
        # Given a model reply naming a taxonomy team with different casing
        classifier = _classifier(reply="  identity & access \n")

        # When an escalation is classified
        team = await classifier.classify(text="I can't get into Workday")

        # Then the canonical taxonomy name comes back
        assert team == "Identity & Access"

    async def test_returns_none_for_an_off_taxonomy_reply(self):
        # Given a model reply outside the taxonomy (including 'unknown')
        classifier = _classifier(reply="unknown")

        # When an escalation is classified
        # Then no team is assigned — the caller falls back to default routing
        assert await classifier.classify(text="Something odd") is None

    async def test_fails_open_to_none_on_a_model_error(self):
        # Given a gateway error during classification
        classifier = _classifier(error=RuntimeError("gateway down"))

        # When an escalation is classified
        # Then it degrades to unclassified — routing must never block escalation
        assert await classifier.classify(text="VPN down") is None


def _intent_classifier(
    reply: str | None = None, error: Exception | None = None
) -> llm.LLMIntentClassifier:
    client = mock.AsyncMock()
    if error is not None:
        client.chat.completions.create.side_effect = error
    else:
        response = mock.Mock()
        response.choices = [mock.Mock(message=mock.Mock(content=reply))]
        client.chat.completions.create.return_value = response
    return llm.LLMIntentClassifier(client=client, model_name="test-model")


class TestLLMIntentClassifier:
    async def test_returns_the_intent_and_lowercased_services(self):
        # Given a model reply naming an intent and mixed-case service mentions
        classifier = _intent_classifier(
            reply='{"intent": "troubleshooting", "services": ["Coder", "Jenkins"]}'
        )

        # When a request is classified
        reading = await classifier.classify(text="Coder workspace build fails on Jenkins")

        # Then the reading carries the intent and normalized services
        assert reading == llm.IntentReading(
            intent="troubleshooting", services=("coder", "jenkins")
        )

    async def test_reads_the_json_out_of_model_chatter(self):
        # Given a local thinking model that decorates its answer with fences
        classifier = _intent_classifier(
            reply='Sure!\n```json\n{"intent": "access_request", "services": []}\n```\nDone.'
        )

        # When a request is classified
        reading = await classifier.classify(text="I need snowflake reporting access")

        # Then the embedded JSON still parses
        assert reading == llm.IntentReading(intent=llm.INTENT_ACCESS, services=())

    async def test_drops_an_off_taxonomy_intent_but_keeps_the_services(self):
        # Given a model reply whose intent label is outside the taxonomy
        classifier = _intent_classifier(
            reply='{"intent": "party_planning", "services": ["snowflake"]}'
        )

        # When a request is classified
        reading = await classifier.classify(text="Plan me a party in snowflake")

        # Then the intent is unclassified while the service mentions survive
        assert reading == llm.IntentReading(intent=None, services=("snowflake",))

    async def test_fails_open_to_an_empty_reading_on_a_model_error(self):
        # Given a gateway error during classification
        classifier = _intent_classifier(error=RuntimeError("gateway down"))

        # When a request is classified
        # Then it degrades to an empty reading — routing must never block a request
        assert await classifier.classify(text="VPN down") == llm.IntentReading()

    async def test_fails_open_when_the_reply_carries_no_json(self):
        # Given a model reply that ignored the JSON format instruction
        classifier = _intent_classifier(reply="access_request")

        # When a request is classified
        # Then it degrades to an empty reading rather than guessing
        assert await classifier.classify(text="I need access") == llm.IntentReading()
