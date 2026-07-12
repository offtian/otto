import pathlib
import textwrap

from otto.domain.identity import users


ALICE = users.User(
    name="Alice Example",
    team="Data Platform",
    slack_user_id="U_ALICE",
    jira_account_id="JIRA_ALICE",
)
BOB = users.User(name="Bob Example", team="Sales", slack_user_id="U_BOB")


class TestUserDirectory:
    async def test_finds_a_user_by_either_channel_id(self):
        # Given a directory holding a cross-channel user
        directory = users.UserDirectory(users=[ALICE])

        # When looked up by each channel id
        by_slack = await directory.find("U_ALICE")
        by_jira = await directory.find("JIRA_ALICE")

        # Then both ids resolve to the same person
        assert by_slack is ALICE
        assert by_jira is ALICE

    async def test_returns_none_for_an_unmapped_id(self):
        # Given a directory without the id
        directory = users.UserDirectory(users=[ALICE])

        # When an unknown id is looked up
        found = await directory.find("U_STRANGER")

        # Then there is no match
        assert found is None

    async def test_same_person_matches_across_channels(self):
        # Given a directory tying Alice's Slack and Jira ids together
        directory = users.UserDirectory(users=[ALICE, BOB])

        # When her two channel ids are compared
        same = await directory.same_person("U_ALICE", "JIRA_ALICE")

        # Then they are recognized as one person (T3 cross-channel)
        assert same

    async def test_same_person_rejects_different_people(self):
        # Given a directory with two distinct users
        directory = users.UserDirectory(users=[ALICE, BOB])

        # When their ids are compared
        same = await directory.same_person("U_ALICE", "U_BOB")

        # Then they are not the same person
        assert not same

    async def test_role_comes_from_the_matched_user(self):
        # Given a directory with an approver and a plain user
        approver = users.User(name="Sam", team="IT", slack_user_id="U_SAM", role="support_user")
        directory = users.UserDirectory(users=[approver, BOB])

        # When their roles are looked up
        # Then the approver's role is returned and unknowns are empty
        assert await directory.role("U_SAM") == "support_user"
        assert await directory.role("U_BOB") == ""
        assert await directory.role("U_NOBODY") == ""

    async def test_unmapped_ids_only_match_themselves(self):
        # Given an empty directory
        directory = users.UserDirectory(users=[])

        # When an unmapped id is compared with itself and with another
        identical = await directory.same_person("U_X", "U_X")
        different = await directory.same_person("U_X", "U_Y")

        # Then only literal equality matches
        assert identical
        assert not different


class TestLoadUsers:
    def test_parses_users_from_yaml(self, tmp_path):
        # Given a YAML directory file with one entry
        path = tmp_path / "users.yaml"
        path.write_text(
            textwrap.dedent(
                """
                users:
                  - name: Alice Example
                    team: Data Platform
                    slack_user_id: U_ALICE
                    jira_account_id: JIRA_ALICE
                """
            )
        )

        # When it is loaded
        loaded = users.load_users(path)

        # Then the entry becomes a User with both channel ids
        assert loaded == (ALICE,)

    def test_missing_file_yields_an_empty_directory(self):
        # Given a path that does not exist

        # When it is loaded
        loaded = users.load_users(pathlib.Path("nowhere/users.yaml"))

        # Then the directory is simply empty (identity mapping is optional)
        assert loaded == ()

    def test_empty_users_list_yields_no_entries(self, tmp_path):
        # Given the shipped placeholder file shape
        path = tmp_path / "users.yaml"
        path.write_text("users: []\n")

        # When it is loaded
        loaded = users.load_users(path)

        # Then there are no users
        assert loaded == ()
