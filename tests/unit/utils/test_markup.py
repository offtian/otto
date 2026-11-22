import pytest

from otto.utils import markup


class TestSlackMrkdwn:
    @pytest.mark.parametrize(
        ("markdown", "mrkdwn"),
        [
            ("**Restart** the client", "*Restart* the client"),
            ("__Restart__ the client", "*Restart* the client"),
            ("it is *really* slow", "it is _really_ slow"),
            (
                "see [the VPN runbook](https://kb.example.com/vpn)",
                "see <https://kb.example.com/vpn|the VPN runbook>",
            ),
            ("## Next steps", "*Next steps*"),
            ("## **Next** steps", "*Next steps*"),
        ],
    )
    def test_converts_the_marks_slack_writes_differently(self, markdown, mrkdwn):
        # Given a reply using a Markdown mark Slack spells another way
        # When it is rendered for Slack
        # Then the mark is Slack's
        assert markup.slack_mrkdwn(markdown) == mrkdwn

    @pytest.mark.parametrize(
        "text",
        [
            "Restart the VPN client.",
            "- first\n- second",
            "* first\n* second",
            "2 * 3 * 4 is 24",
            "open my_file_name.txt and other_file_name.txt",
            "1. unplug it\n2. plug it back in",
            ":tada: done",
        ],
    )
    def test_leaves_what_is_not_a_mark_alone(self, text):
        # Given a reply whose asterisks and underscores are not emphasis
        # When it is rendered for Slack
        # Then it is unchanged
        assert markup.slack_mrkdwn(text) == text

    def test_a_reply_cannot_ping_a_channel_or_a_person(self):
        # Given a reply that carries Slack's own control sequences
        reply = "<!channel> ask <@U123> about R&D"

        # When it is rendered for Slack
        rendered = markup.slack_mrkdwn(reply)

        # Then they arrive as plain characters, not as mentions
        assert rendered == "&lt;!channel&gt; ask &lt;@U123&gt; about R&amp;D"

    def test_code_is_kept_as_written_apart_from_escaping(self):
        # Given a reply with marks inside inline code and a code block
        reply = "run `rm **/*.tmp` then:\n```hcl\ncpu = 32 # **not** bold, a < b\n```"

        # When it is rendered for Slack
        rendered = markup.slack_mrkdwn(reply)

        # Then the code keeps its asterisks and is only escaped
        assert rendered == "run `rm **/*.tmp` then:\n```cpu = 32 # **not** bold, a &lt; b\n```"


class TestJiraWiki:
    @pytest.mark.parametrize(
        ("markdown", "wiki"),
        [
            ("**Restart** the client", "*Restart* the client"),
            ("it is *really* slow", "it is _really_ slow"),
            (
                "see [the VPN runbook](https://kb.example.com/vpn)",
                "see [the VPN runbook|https://kb.example.com/vpn]",
            ),
            ("run `ipconfig /all`", "run {{ipconfig /all}}"),
            ("```\ncpu = 32\n```", "{code}\ncpu = 32\n{code}"),
            ("## Next steps", "*Next steps*"),
        ],
    )
    def test_converts_the_marks_jira_writes_differently(self, markdown, wiki):
        # Given a reply using a Markdown mark Jira spells another way
        # When it is rendered for Jira
        # Then the mark is Jira's
        assert markup.jira_wiki(markdown) == wiki

    def test_does_not_escape(self):
        # Given a reply with the characters Slack treats as control characters
        # When it is rendered for Jira
        # Then they are left alone — Jira reads them as themselves
        assert markup.jira_wiki("R&D says a < b") == "R&D says a < b"
