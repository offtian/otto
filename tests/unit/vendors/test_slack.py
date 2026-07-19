from otto.vendors import slack as slack_vendor


class _FakeGateway:
    def __init__(self):
        self.escalations = []  # (channel, text)

    async def post_escalation(self, *, channel, text, resolve_value):
        self.escalations.append((channel, text))
        return "300.1"


class _FakeClassifier:
    def __init__(self, team):
        self.team = team

    async def classify(self, *, text):
        return self.team


class TestSlackTriageBackendRouting:
    async def test_a_classified_escalation_lands_in_the_teams_channel(self):
        # Given a classifier assigning a mapped team
        gateway = _FakeGateway()
        backend = slack_vendor.SlackTriageBackend(
            gateway=gateway,
            triage_channel="C_TRIAGE",
            classifier=_FakeClassifier("Network"),
            team_channels={"Network": "C_NET"},
        )

        # When an escalation is posted
        await backend.escalate(
            subject="VPN down",
            summary="Site-wide VPN failures",
            urgency="high",
            requester_id="U_REQ",
            origin_ref="IT-9",
        )

        # Then the card lands in the team's channel and names the team
        channel, text = gateway.escalations[0]
        assert channel == "C_NET"
        assert "Assigned team:* Network" in text

    async def test_an_unclassified_escalation_falls_back_to_the_default_channel(self):
        # Given a classifier that cannot assign a team
        gateway = _FakeGateway()
        backend = slack_vendor.SlackTriageBackend(
            gateway=gateway,
            triage_channel="C_TRIAGE",
            classifier=_FakeClassifier(None),
            team_channels={"Network": "C_NET"},
        )

        # When an escalation is posted
        await backend.escalate(
            subject="Weird issue",
            summary="Nobody knows",
            urgency="normal",
            requester_id="U_REQ",
            origin_ref="IT-10",
        )

        # Then it hands off to the default human triage channel, no team line
        channel, text = gateway.escalations[0]
        assert channel == "C_TRIAGE"
        assert "Assigned team" not in text

    async def test_an_unmapped_team_falls_back_but_still_names_the_team(self):
        # Given a classified team with no channel mapping
        gateway = _FakeGateway()
        backend = slack_vendor.SlackTriageBackend(
            gateway=gateway,
            triage_channel="C_TRIAGE",
            classifier=_FakeClassifier("Endpoint"),
            team_channels={"Network": "C_NET"},
        )

        # When an escalation is posted
        await backend.escalate(
            subject="Laptop bricked",
            summary="Won't boot",
            urgency="high",
            requester_id="U_REQ",
            origin_ref="IT-11",
        )

        # Then the default channel receives it, with the team named for humans
        channel, text = gateway.escalations[0]
        assert channel == "C_TRIAGE"
        assert "Assigned team:* Endpoint" in text

    async def test_no_classifier_means_the_single_channel_as_before(self):
        # Given no classifier configured (the default wiring)
        gateway = _FakeGateway()
        backend = slack_vendor.SlackTriageBackend(gateway=gateway, triage_channel="C_TRIAGE")

        # When an escalation is posted
        await backend.escalate(
            subject="Printer on fire",
            summary="Literally",
            urgency="high",
            requester_id="U_REQ",
            origin_ref="IT-12",
        )

        # Then behaviour is unchanged from before routing existed
        assert gateway.escalations[0][0] == "C_TRIAGE"
