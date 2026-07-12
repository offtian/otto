"""
Who is asking, and may they approve: cross-channel identity, team, and role.

A ``User`` ties the ids one person has on each channel (Slack user id, Jira
account id) to a single identity with a team and an approver role, so
access-control checks and approval cards reason about *people*, not
channel-specific ids. The ``Directory`` seam has two implementations: the
in-memory ``UserDirectory`` (dev, loaded from ``users.yaml``) and
``PostgresUserDirectory`` (Phase 2, 2.3). Lookups are async so the Postgres
directory can query live — a role revoked in the DB rejects the next click.
Entitlement policy stays SailPoint's job; this is a lookup, not an identity
system.
"""

import pathlib
from collections.abc import Sequence
from typing import Any, Protocol

import attrs
import databases
import sqlalchemy as sa
import yaml

from otto.data import models


# Roles that may approve/deny sensitive actions (D3). The DB ``role`` column
# and the settings bootstrap lists both speak these values.
APPROVER_ROLES = frozenset({"support_user", "admin"})


@attrs.frozen
class User:
    """
    One person across channels. Empty channel ids simply never match.
    """

    name: str
    team: str
    slack_user_id: str = ""
    jira_account_id: str = ""
    role: str = ""


class Directory(Protocol):
    """
    Identity + role lookup. Async so a DB-backed directory can query live.
    """

    async def find(self, user_id: str) -> User | None: ...

    async def same_person(self, first_id: str, second_id: str) -> bool: ...

    async def role(self, user_id: str) -> str: ...


class UserDirectory:
    """
    In-memory directory loaded once from ``users.yaml`` (dev / no-DB). Roles
    come from the yaml entries (empty unless set); settings lists are the
    bootstrap fallback the application layer applies on top.
    """

    def __init__(self, users: Sequence[User]) -> None:
        self._by_channel_id: dict[str, User] = {}
        for user in users:
            for channel_id in (user.slack_user_id, user.jira_account_id):
                if channel_id:
                    self._by_channel_id[channel_id] = user

    async def find(self, user_id: str) -> User | None:
        """
        Return the user owning this Slack or Jira id, or None if unmapped.
        """
        return self._by_channel_id.get(user_id)

    async def same_person(self, first_id: str, second_id: str) -> bool:
        """
        Test whether two channel ids belong to one person (the cross-channel
        half of the self-approval exclusion, T3). Unmapped ids only match
        themselves.
        """
        if first_id == second_id:
            return True
        first = self._by_channel_id.get(first_id)
        return first is not None and first is self._by_channel_id.get(second_id)

    async def role(self, user_id: str) -> str:
        """
        Return the approver role of this user, or empty if unknown/none.
        """
        user = self._by_channel_id.get(user_id)
        return user.role if user is not None else ""


_USERS: sa.Table = models.UserRecord.__table__  # type: ignore[attr-defined]


@attrs.frozen
class PostgresUserDirectory:
    """
    Directory over the Postgres ``users`` table (2.3). Every lookup hits the
    DB, so a role revoked in the table rejects the next approval click — no
    stale cache to grant a revoked approver. The ``Database`` is injected by
    ``config``; this touches only ``data.models`` (never ``data.db``).
    """

    database: databases.Database

    async def find(self, user_id: str) -> User | None:
        if not user_id:
            return None
        row = await self.database.fetch_one(
            sa.select(_USERS).where(
                sa.or_(_USERS.c.slack_user_id == user_id, _USERS.c.jira_account_id == user_id)
            )
        )
        return _user_from_row(row) if row is not None else None

    async def same_person(self, first_id: str, second_id: str) -> bool:
        if first_id == second_id:
            return True
        first = await self.find(first_id)
        if first is None:
            return False
        second = await self.find(second_id)
        return second is not None and first == second

    async def role(self, user_id: str) -> str:
        user = await self.find(user_id)
        return user.role if user is not None else ""


def _user_from_row(row: Any) -> User:
    data: dict[str, Any] = dict(row)
    return User(
        name=data["name"],
        team=data["team"],
        slack_user_id=data["slack_user_id"],
        jira_account_id=data["jira_account_id"],
        role=data["role"],
    )


def load_users(path: pathlib.Path) -> tuple[User, ...]:
    """
    Return the users declared in a YAML directory file. A missing or empty
    file yields an empty directory — identity mapping is optional in dev and
    everything degrades to raw channel ids.
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
            role=str(entry.get("role", "")),
        )
        for entry in data.get("users") or []
    )
