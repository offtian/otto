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
