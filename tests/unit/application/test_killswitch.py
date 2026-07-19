from datetime import UTC, datetime, timedelta

import pytest

from otto import config
from otto.application import killswitch
from otto.data import db
from otto.settings import Settings


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


class _FakeDatabase:
    """
    Stand-in for the ``databases`` pool: serves a scripted flag row (or an
    error) and counts queries so cache behaviour is observable.
    """

    is_connected = True

    def __init__(self, *, value: str | None = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.queries = 0

    async def fetch_one(self, query):
        self.queries += 1
        if self.error is not None:
            raise self.error
        return None if self.value is None else {"value": self.value}


@pytest.fixture
def wire(monkeypatch):
    def _wire(*, database=None, **settings_overrides):
        cfg = config.Configuration(
            settings=Settings(_env_file=None, **settings_overrides),
        )
        monkeypatch.setattr(config, "get_config", lambda: cfg)
        if database is not None:
            monkeypatch.setattr(db, "get_db", lambda: database)
        return cfg

    return _wire


class TestKillSwitch:
    async def test_returns_the_env_default_without_a_database(self, wire):
        # Given no database configured and the env kill switch off
        wire(database_url="", otto_enabled=False)

        # When the switch is read
        enabled = await killswitch.KillSwitch().enabled(now=NOW)

        # Then the env value rules
        assert enabled is False

    async def test_a_runtime_flag_row_overrides_the_env_default(self, wire):
        # Given the env says enabled but the runtime flag says off (C1)
        wire(otto_enabled=True, database=_FakeDatabase(value="false"))

        # When the switch is read
        enabled = await killswitch.KillSwitch().enabled(now=NOW)

        # Then the runtime flag wins — Otto is silenced without a restart
        assert enabled is False

    async def test_a_missing_flag_row_falls_back_to_the_env_default(self, wire):
        # Given a database with no otto_enabled row
        wire(otto_enabled=True, database=_FakeDatabase(value=None))

        # When the switch is read
        # Then the env default applies
        assert await killswitch.KillSwitch().enabled(now=NOW) is True

    async def test_a_database_error_fails_open_to_the_env_default(self, wire):
        # Given the flag is unreadable (DB blip) and the env says enabled
        wire(otto_enabled=True, database=_FakeDatabase(error=RuntimeError("db down")))

        # When the switch is read
        # Then it fails open — a blip must not silence Otto (C1 decision)
        assert await killswitch.KillSwitch().enabled(now=NOW) is True

    async def test_reads_are_cached_within_the_ttl_and_refreshed_after(self, wire):
        # Given a flag row and a 10-second cache window
        database = _FakeDatabase(value="true")
        wire(kill_switch_cache_seconds=10, database=database)
        switch = killswitch.KillSwitch()

        # When the switch is read twice inside the window and once after it
        await switch.enabled(now=NOW)
        await switch.enabled(now=NOW + timedelta(seconds=5))
        await switch.enabled(now=NOW + timedelta(seconds=15))

        # Then only the first and the post-TTL read hit the database
        assert database.queries == 2
