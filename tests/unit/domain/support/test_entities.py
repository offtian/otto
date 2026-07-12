import pytest

from otto.domain.support import entities


class TestOriginSerialization:
    @pytest.mark.parametrize(
        "origin",
        [
            entities.SlackThread(channel_id="C1", thread_ts="1700000000.001"),
            entities.TicketRef(issue_key="IT-42"),
        ],
    )
    def test_round_trips_both_origin_types(self, origin):
        # Given an origin of either channel
        # When it is serialized and rebuilt
        rebuilt = entities.origin_from_json(entities.origin_to_json(origin))

        # Then the value is preserved exactly (the durable ApprovalRecord relies on this)
        assert rebuilt == origin

    def test_serialization_tags_the_channel(self):
        # Given a Slack origin
        # When it is serialized
        raw = entities.origin_to_json(entities.SlackThread(channel_id="C1", thread_ts="1.0"))

        # Then the payload names its kind so the reader can discriminate
        assert '"kind": "slack"' in raw

    def test_from_json_rejects_an_unknown_kind(self):
        # Given a payload with an unrecognized kind
        # When it is deserialized
        # Then it fails loudly rather than returning a wrong shape
        with pytest.raises(ValueError, match="unknown origin kind"):
            entities.origin_from_json('{"kind": "carrier-pigeon"}')
