"""
Who is asking: cross-channel requester identity and team membership.

A ``User`` ties the ids one person has on each channel (Slack user id,
Jira account id) to a single identity with a team, so access-control
checks and approval cards reason about *people*, not channel-specific
ids. Approver *roles* stay in settings (D3) until the Phase 2 roles
table; entitlement policy stays SailPoint's job — this is a lookup, not
an identity system.
"""

import pathlib
from collections.abc import Sequence

import attrs
import yaml


@attrs.frozen
class User:
    """
    One person across channels. Empty channel ids simply never match.
    """

    name: str
    team: str
    slack_user_id: str = ""
    jira_account_id: str = ""


class UserDirectory:
    """
    In-memory lookup from any channel id to the person behind it.

    Loaded once at boot from ``users.yaml`` (prototype); a Postgres-backed
    directory replaces this alongside the Phase 2 roles table.
    """

    def __init__(self, users: Sequence[User]) -> None:
        self._by_channel_id: dict[str, User] = {}
        for user in users:
            for channel_id in (user.slack_user_id, user.jira_account_id):
                if channel_id:
                    self._by_channel_id[channel_id] = user

    def find(self, user_id: str) -> User | None:
        """
        Return the user owning this Slack or Jira id, or None if unmapped.
        """
        return self._by_channel_id.get(user_id)

    def same_person(self, first_id: str, second_id: str) -> bool:
        """
        Test whether two channel ids belong to one person — the
        cross-channel half of the self-approval exclusion (T3). Unmapped
        ids only match themselves.
        """
        if first_id == second_id:
            return True
        user = self.find(first_id)
        return user is not None and user is self.find(second_id)


def load_users(path: pathlib.Path) -> tuple[User, ...]:
    """
    Return the users declared in a YAML directory file. A missing or
    empty file yields an empty directory — identity mapping is optional
    in dev and everything degrades to raw channel ids.
    """
    if not path.is_file():
        return ()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return tuple(
        User(
            name=str(entry.get("name", "")),
            team=str(entry.get("team", "")),
            slack_user_id=str(entry.get("slack_user_id", "")),
            jira_account_id=str(entry.get("jira_account_id", "")),
        )
        for entry in data.get("users") or []
    )
