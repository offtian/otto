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
    async def test_returns_the_intent_the_model_names(self):
        # Given a model reply naming a taxonomy intent
        classifier = _intent_classifier(reply='{"intent": "troubleshooting", "recall": ""}')

        # When a request is classified
        reading = await classifier.classify(text="Coder workspace build fails on Jenkins")

        # Then the reading carries the intent, with the gate closed
        assert reading == llm.IntentReading(intent="troubleshooting")

    async def test_reads_the_retrieval_gates_keywords(self):
        # Given a model reply that says past cases would help, with keywords
        classifier = _intent_classifier(
            reply='{"intent": "troubleshooting", "recall": " vpn hotel wifi "}'
        )

        # When a request is classified
        reading = await classifier.classify(text="My VPN keeps dropping at the hotel")

        # Then the reading carries the trimmed keywords — the gate is open
        assert reading.recall == "vpn hotel wifi"

    async def test_keeps_the_gate_closed_when_the_reply_is_not_keywords(self):
        # Given a model reply whose recall field is not a string
        classifier = _intent_classifier(reply='{"intent": "how_to", "recall": true}')

        # When a request is classified
        reading = await classifier.classify(text="How do I reset my password?")

        # Then the gate stays closed rather than searching for nonsense
        assert reading.recall == ""

    async def test_reads_the_json_out_of_model_chatter(self):
        # Given a local thinking model that decorates its answer with fences
        classifier = _intent_classifier(
            reply='Sure!\n```json\n{"intent": "access_request", "recall": ""}\n```\nDone.'
        )

        # When a request is classified
        reading = await classifier.classify(text="I need snowflake reporting access")

        # Then the embedded JSON still parses
        assert reading == llm.IntentReading(intent=llm.INTENT_ACCESS)

    async def test_drops_an_off_taxonomy_intent_but_keeps_the_gates_verdict(self):
        # Given a model reply whose intent label is outside the taxonomy
        classifier = _intent_classifier(
            reply='{"intent": "party_planning", "recall": "snowflake outage"}'
        )

        # When a request is classified
        reading = await classifier.classify(text="Plan me a party in snowflake")

        # Then the intent is unclassified while the recall keywords survive
        assert reading == llm.IntentReading(intent=None, recall="snowflake outage")

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


def _distiller(reply: str | None = None, error: Exception | None = None) -> llm.LLMCaseDistiller:
    client = mock.AsyncMock()
    if error is not None:
        client.chat.completions.create.side_effect = error
    else:
        response = mock.Mock()
        response.choices = [mock.Mock(message=mock.Mock(content=reply))]
        client.chat.completions.create.return_value = response
    return llm.LLMCaseDistiller(client=client, model_name="test-model")


class TestLLMCaseDistiller:
    TRANSCRIPT = "U_REQ: VPN drops on hotel wifi\notto: Restart the client.\nU_REQ: fixed, thanks"

    async def test_returns_the_case_note_the_model_wrote(self):
        # Given a model reply that keeps the case, with a note
        distiller = _distiller(
            reply='{"keep": true, "note": " Symptom: VPN drops. Fix: restart the client. "}'
        )

        # When a resolved conversation is distilled
        note = await distiller.distill(transcript=self.TRANSCRIPT)

        # Then the trimmed note comes back
        assert note == "Symptom: VPN drops. Fix: restart the client."

    async def test_returns_none_when_the_model_finds_nothing_reusable(self):
        # Given a model reply that declines to keep the case
        distiller = _distiller(reply='{"keep": false, "note": ""}')

        # When a resolved conversation is distilled
        # Then there is nothing to remember
        assert await distiller.distill(transcript=self.TRANSCRIPT) is None

    async def test_fails_closed_on_a_model_error(self):
        # Given a gateway error during distillation
        distiller = _distiller(error=RuntimeError("gateway down"))

        # When a resolved conversation is distilled
        # Then nothing is remembered — never the unvetted transcript
        assert await distiller.distill(transcript=self.TRANSCRIPT) is None

    async def test_fails_closed_on_a_reply_that_is_not_the_asked_json(self):
        # Given a model reply that ignored the JSON format instruction
        distiller = _distiller(reply="Symptom: VPN drops. Fix: restart the client.")

        # When a resolved conversation is distilled
        # Then nothing is remembered
        assert await distiller.distill(transcript=self.TRANSCRIPT) is None
