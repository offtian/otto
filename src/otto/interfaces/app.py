"""
FastAPI app assembly: lifespan, shared state, and router mounting.

The routes themselves live in ``interfaces.routers`` (one sub-router per
channel/concern). This module owns only what is app-wide: telemetry + MCP
lifecycle, the dedup remember-set, and Otto's cached Jira identity.
"""

import collections
import contextlib
from collections.abc import AsyncIterator

import fastapi

from otto import config
from otto.data import db as data_db
from otto.interfaces.routers import base, jira, slack
from otto.utils import logs, telemetry


class _RecentIds:
    """
    Bounded remember-set for event dedup (Slack retries and redeliveries).
    """

    # ponytail: in-process only — move to Redis when replicas > 1 (Phase 3).

    def __init__(self, maxlen: int = 2048) -> None:
        self._ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._maxlen = maxlen

    def seen(self, event_id: str) -> bool:
        """
        Record the id and return whether it had been seen before.
        """
        if event_id in self._ids:
            return True
        self._ids[event_id] = None
        if len(self._ids) > self._maxlen:
            self._ids.popitem(last=False)
        return False


@contextlib.asynccontextmanager
async def _lifespan(started_app: fastapi.FastAPI) -> AsyncIterator[None]:
    cfg = config.get_config()
    logs.configure_logging(level=cfg.settings.log_level)
    telemetry.setup_telemetry(
        service_name=cfg.settings.otel_service_name,
        environment=cfg.settings.environment,
        logfire_token=cfg.settings.logfire_token,
        otlp_endpoint=cfg.settings.otlp_endpoint,
    )
    telemetry.instrument_app(started_app)
    if cfg.settings.database_url:
        await data_db.connect_db()  # durable approval store (Phase 2)
    servers = [s for s in (cfg.confluence_mcp, cfg.sailpoint_mcp) if s is not None]
    for server in servers:
        await server.connect()  # type: ignore[no-untyped-call]  # SDK method lacks annotations
    logs.log_event("app_started", params={"otto_enabled": cfg.settings.otto_enabled})
    yield
    for server in servers:
        await server.cleanup()  # type: ignore[no-untyped-call]  # SDK method lacks annotations
    if cfg.settings.database_url:
        await data_db.disconnect_db()


app = fastapi.FastAPI(title="otto", lifespan=_lifespan)
app.state.recent_events = _RecentIds()
# Otto's own Jira account id, fetched lazily on the first webhook (FR9
# own-actor loop guard). None = not yet established.
app.state.jira_bot_account_id = None
app.include_router(slack.router)
app.include_router(jira.router)
app.include_router(base.router)
