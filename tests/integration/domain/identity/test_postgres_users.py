"""
Integration tests for the DB-backed identity + role directory (2.3) against a
real Postgres. The revocation test is the acceptance criterion: a role removed
in the DB must reject the next click — no stale cache.

Live: needs `just infra` + migrations. Guarded by RUN_INTEGRATION; run via
`just test-integration`.
"""

import os

import databases
import pytest

from otto.data import _dsn
from otto.domain.identity import users as identity_users


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="needs Postgres — run via `just test-integration`",
)

DB_URL = _dsn.to_libpq(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/otto")
)


async def _insert(db, *, id_, name, team="IT", slack="", jira="", role=""):
    await db.execute(
        "INSERT INTO users (id, name, team, slack_user_id, jira_account_id, role) "
        "VALUES (:id, :name, :team, :slack, :jira, :role)",
        {"id": id_, "name": name, "team": team, "slack": slack, "jira": jira, "role": role},
    )


@pytest.fixture
async def directory():
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM users")
    try:
        yield db, identity_users.PostgresUserDirectory(database=db)
    finally:
        await db.execute("DELETE FROM users")
        await db.disconnect()


class TestPostgresUserDirectory:
    async def test_finds_across_channels_with_team_and_role(self, directory):
        # Given a cross-channel approver in the DB
        db, dir_ = directory
        await _insert(
            db, id_="u1", name="Sam", slack="U_SAM", jira="JIRA_SAM", role="support_user"
        )

        # When looked up by either channel id
        by_slack = await dir_.find("U_SAM")
        by_jira = await dir_.find("JIRA_SAM")

        # Then both resolve to the person, with team and role
        assert by_slack == by_jira
        assert by_slack.team == "IT"
        assert await dir_.same_person("U_SAM", "JIRA_SAM")
        assert await dir_.role("U_SAM") == "support_user"

    async def test_unknown_id_degrades_to_empty(self, directory):
        # Given an empty directory
        _db, dir_ = directory

        # When unknown ids are looked up
        # Then everything degrades to "no match" / no role
        assert await dir_.find("U_NOBODY") is None
        assert await dir_.role("U_NOBODY") == ""
        assert not await dir_.same_person("U_A", "U_B")

    async def test_role_lookup_is_live_after_revocation(self, directory):
        # Given an approver whose role is granted in the DB
        db, dir_ = directory
        await _insert(db, id_="u1", name="Sam", slack="U_SAM", role="support_user")
        assert await dir_.role("U_SAM") == "support_user"

        # When the role is revoked directly in the DB
        await db.execute("UPDATE users SET role = '' WHERE slack_user_id = 'U_SAM'")

        # Then the very next lookup reflects it — no stale cache grants a
        # revoked approver
        assert await dir_.role("U_SAM") == ""
