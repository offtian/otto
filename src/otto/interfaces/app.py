"""
FastAPI app assembly: lifespan, shared state, and router mounting.

The routes themselves live in ``interfaces.routers`` (one sub-router per
channel/concern). This module owns only what is app-wide: telemetry + MCP
lifecycle, the dedup remember-set, and Otto's cached Jira identity.
"""

import asyncio
import collections
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import fastapi
from agents import mcp as agents_mcp

from otto import config
from otto.application import support
from otto.data import db
from otto.interfaces.routers import base, jira, slack
from otto.settings import settings
from otto.utils import logs, telemetry


async def _sweep_loop(interval_seconds: int) -> None:
    """
    Run the approval-maintenance sweep (2.5) every ``interval_seconds`` for the
    life of the process. A failed tick is logged and the loop continues — a
    transient DB blip must not stop expiry/retention forever.

    ponytail: in-process, single-replica — two replicas would double every
    reminder. Move to a locked/leader job before running replicas (Phase 3).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await support.sweep_approvals(now=datetime.now(tz=UTC))
        except Exception as exc:
            logs.log_exception(exc, params={"job": "approval_sweep"})


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


class _RateLimiter:
    """
    Per-user fixed-window rate limit (agent-mode spend/abuse guard, 3.5).
    ``allow`` records the hit and returns whether the user is under the cap.
    """

    # ponytail: in-process only — move to Redis with the dedup set at replicas > 1.

    def __init__(self, *, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: dict[str, list[datetime]] = {}

    def allow(self, user_id: str, *, now: datetime) -> bool:
        """
        Return whether ``user_id`` is under the per-minute cap, counting this
        request. A cap of 0 disables the limit (always allowed).
        """
        if self._per_minute <= 0:
            return True
        window_start = now - timedelta(seconds=60)
        hits = [hit for hit in self._hits.get(user_id, []) if hit >= window_start]
        allowed = len(hits) < self._per_minute
        if allowed:
            hits.append(now)
        self._hits[user_id] = hits
        return allowed


@contextlib.asynccontextmanager
async def _lifespan(started_app: fastapi.FastAPI) -> AsyncIterator[None]:
    cfg = config.get_config()
    logs.configure_logging(level=cfg.settings.log_level)
    telemetry.setup_telemetry(
        service_name=cfg.settings.otel_service_name,
        environment=cfg.settings.environment,
        logfire_token=cfg.settings.logfire_token,
        otlp_endpoint=cfg.settings.otlp_endpoint,
        langfuse_host=cfg.settings.langfuse_host,
        langfuse_public_key=cfg.settings.langfuse_public_key,
        langfuse_secret_key=cfg.settings.langfuse_secret_key,
    )
    telemetry.instrument_app(started_app)
    async with contextlib.AsyncExitStack() as stack:
        if cfg.settings.database_url:
            # Durable approval store (Phase 2): the shared pool stays open for
            # the process lifetime and closes last on shutdown (the sweep and
            # MCP teardown above it may still query).
            await stack.enter_async_context(db.database())
        servers = [
            mount.server for mount in (cfg.confluence_mcp, cfg.sailpoint_mcp) if mount is not None
        ]
        connected: list[agents_mcp.MCPServerStreamableHttp] = []
        for server in servers:
            try:
                await server.connect()  # type: ignore[no-untyped-call]  # SDK lacks annotations
            except Exception as exc:
                # A misconfigured or unavailable MCP degrades its own capability
                # (its tool errors per request, caught by FR8) — it must never take
                # the whole service down at startup.
                logs.log_exception(exc, params={"mcp_connect": type(server).__name__})
                continue
            connected.append(server)
        # After connect on purpose: wiring the agent pulls each mount's tool list
        # over its live connection (a failed mount degrades to its stub tool).
        await cfg.load_agents()
        # Approval maintenance sweep (2.5): only meaningful against the durable
        # store, and the interval is a kill switch (0 = off).
        sweep_task: asyncio.Task[None] | None = None
        if cfg.settings.database_url and cfg.settings.approval_sweep_interval_minutes > 0:
            sweep_task = asyncio.create_task(
                _sweep_loop(cfg.settings.approval_sweep_interval_minutes * 60)
            )
        logs.log_event("app_started", params={"otto_enabled": cfg.settings.otto_enabled})
        yield
        if sweep_task is not None:
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task
        for server in connected:
            await server.cleanup()  # type: ignore[no-untyped-call]  # SDK lacks annotations


app = fastapi.FastAPI(title="otto", lifespan=_lifespan)
app.state.recent_events = _RecentIds()
app.state.rate_limiter = _RateLimiter(per_minute=settings.slack_user_rate_limit_per_minute)
# Otto's own Jira account id, fetched lazily on the first webhook (FR9
# own-actor loop guard). None = not yet established.
app.state.jira_bot_account_id = None
app.include_router(slack.router)
app.include_router(jira.router)
app.include_router(base.router)
