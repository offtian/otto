"""
Runtime kill switch (C1, FR8): whether Otto may reply, answerable without a
restart.

The env value (``settings.otto_enabled``) is the default; with a database
configured and connected, a ``runtime_flags`` row named ``otto_enabled``
overrides it (``just otto-off`` / ``just otto-on``). Reads are cached for
``kill_switch_cache_seconds`` — the worst-case latency between flipping the
flag and Otto going quiet. Fail-open by decision (2026-07-19): a DB blip must
not silence Otto; the env default applies while the flag is unreadable.
"""

from datetime import datetime

import sqlalchemy as sa

from otto import config
from otto.data import db, models
from otto.utils import logs


_FLAGS: sa.Table = models.RuntimeFlagRecord.__table__  # type: ignore[attr-defined]


class KillSwitch:
    """
    Cached per-request read of the runtime ``otto_enabled`` flag.
    """

    def __init__(self) -> None:
        self._cache: tuple[bool, datetime] | None = None  # (value, read_at)

    async def enabled(self, *, now: datetime) -> bool:
        """
        Return whether Otto may reply right now. ``now`` is injected so the
        cache window is deterministic under test.
        """
        cfg = config.get_config()
        default = cfg.settings.otto_enabled
        if not cfg.settings.database_url:
            return default
        database = db.get_db()
        if not database.is_connected:
            # No pool (tests, CLI entry points without the app lifespan) —
            # the env default rules.
            return default
        if self._cache is not None:
            value, read_at = self._cache
            if (now - read_at).total_seconds() < cfg.settings.kill_switch_cache_seconds:
                return value
        try:
            row = await database.fetch_one(
                sa.select(_FLAGS.c.value).where(_FLAGS.c.name == "otto_enabled")
            )
        except Exception as exc:
            # Fail-open (C1 decision): the env default applies while the
            # flag is unreadable.
            logs.log_exception(exc, params={"guard": "kill_switch"})
            return default
        value = default if row is None else row["value"] == "true"
        self._cache = (value, now)
        return value
