import pathlib

import pytest

from otto.domain.support import runbooks


# The runbooks Otto ships with: the hints people will actually get.
SHIPPED_RUNBOOKS = pathlib.Path(__file__).parents[4] / "runbooks"

HEADINGS = {
    "vpn": "VPN — connect or fix a broken connection",
    "printer-setup": "Printing — add a network printer",
    "wifi-setup": "Wi-Fi — connect to the office network",
    "password-reset": "Password reset — SSO / directory account",
}


@pytest.fixture
def runbooks_dir(tmp_path):
    for name, heading in HEADINGS.items():
        (tmp_path / f"{name}.md").write_text(f"# {heading}\n\n1. First step.\n", encoding="utf-8")
    return tmp_path


class TestMatch:
    @pytest.mark.parametrize(
        ("asked", "names"),
        [
            ("My VPN keeps dropping", ["vpn"]),
            ("I forgot my passwords again", ["password-reset"]),
            ("I can't print", ["printer-setup"]),
            ("The wi-fi is down", ["wifi-setup"]),
            ("printer setup please", ["printer-setup"]),
            ("help me with setup", []),
            ("please add a network drive", []),
            ("can you approve my expense report", []),
            ("", []),
        ],
    )
    def test_names_a_runbook_only_for_words_it_is_called_by(self, runbooks_dir, asked, names):
        # Given four runbooks, two of which are called a "setup"
        # When a request is matched against them — by a plural, by the
        # beginning of a word, by a hyphenated spelling, by a word two
        # runbooks share, or only by words from a runbook's description
        # Then a runbook is named only when the request uses its own words
        assert runbooks.match(text=asked, runbooks_dir=runbooks_dir) == names

    def test_names_the_best_match_first_and_no_more_than_the_limit(self, runbooks_dir):
        # Given a request that touches three runbooks, one of them twice over
        asked = "reset my password, then look at the vpn and the printer"

        # When it is matched for two names
        names = runbooks.match(text=asked, runbooks_dir=runbooks_dir, limit=2)

        # Then the runbook sharing most comes first, and the third is left out
        assert names == ["password-reset", "printer-setup"]

    def test_finds_nothing_where_there_are_no_runbooks(self, tmp_path):
        # Given a runbook directory that does not exist
        # When a request is matched
        # Then there is simply nothing to name
        assert runbooks.match(text="my vpn is down", runbooks_dir=tmp_path / "missing") == []

    @pytest.mark.parametrize(
        ("asked", "names"),
        [
            ("My VPN keeps dropping on hotel wifi", ["vpn", "wifi-setup"]),
            ("I forgot my password", ["password-reset"]),
            ("I'm locked out of my account", ["account-unlock"]),
            ("How do I install Slack?", ["software-install"]),
            ("I can't print anything", ["printer-setup"]),
            ("Need to enrol my new phone for MFA", ["mfa-enroll"]),
            ("Can I get access to the finance dashboard?", []),
        ],
    )
    def test_points_everyday_requests_at_the_shipped_runbooks(self, asked, names):
        # Given the runbooks in this repository
        # When an everyday request is matched against them
        # Then it is pointed at the runbook a person would have picked
        assert runbooks.match(text=asked, runbooks_dir=SHIPPED_RUNBOOKS) == names
