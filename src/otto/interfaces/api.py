"""
FastAPI interface: Slack event intake, interactive approvals, and health.

Translates Slack payloads into application calls — no business logic here.
Every Slack endpoint verifies the request signature (NFR4), acks
immediately and handles in the background (NFR1), and a crashed handler
still produces a user-visible reply at the origin (FR8).
"""

import collections
import contextlib
import json
import urllib.parse
from collections.abc import AsyncIterator, Mapping

import attrs
import fastapi
from slack_sdk.signature import SignatureVerifier

from otto import config
from otto.application import support
from otto.domain.support import entities
from otto.utils import logs, telemetry
from otto.vendors import slack as slack_vendor


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


@attrs.frozen
class _ApprovalDecision:
    approval_id: str
    resolver_id: str
    approved: bool


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
    servers = [s for s in (cfg.confluence_mcp, cfg.sailpoint_mcp) if s is not None]
    for server in servers:
        await server.connect()  # type: ignore[no-untyped-call]  # SDK method lacks annotations
    logs.log_event("app_started", params={"otto_enabled": cfg.settings.otto_enabled})
    yield
    for server in servers:
        await server.cleanup()  # type: ignore[no-untyped-call]  # SDK method lacks annotations


app = fastapi.FastAPI(title="otto", lifespan=_lifespan)
app.state.recent_events = _RecentIds()


@app.post("/slack/events")
async def slack_events(
    request: fastapi.Request,
    background: fastapi.BackgroundTasks,
) -> fastapi.Response:
    cfg = config.get_config()
    body = await request.body()
    _require_valid_signature(
        secret=cfg.settings.slack_signing_secret, body=body, headers=request.headers
    )
    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        return fastapi.responses.JSONResponse({"challenge": payload.get("challenge", "")})
    if request.app.state.recent_events.seen(str(payload.get("event_id", ""))):
        return fastapi.Response()
    support_request = _to_support_request(payload)
    if support_request is None or not cfg.settings.otto_enabled:
        return fastapi.Response()
    background.add_task(_handle_safely, support_request)
    return fastapi.Response()


@app.post("/slack/interactions")
async def slack_interactions(
    request: fastapi.Request,
    background: fastapi.BackgroundTasks,
) -> fastapi.Response:
    cfg = config.get_config()
    body = await request.body()
    _require_valid_signature(
        secret=cfg.settings.slack_signing_secret, body=body, headers=request.headers
    )
    form = urllib.parse.parse_qs(body.decode("utf-8"))
    payload = json.loads(form.get("payload", ["{}"])[0])
    decision = _to_approval_decision(payload)
    if decision is None or not cfg.settings.otto_enabled:
        return fastapi.Response()
    background.add_task(_resolve_safely, decision)
    return fastapi.Response()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _require_valid_signature(*, secret: str, body: bytes, headers: Mapping[str, str]) -> None:
    """
    Reject any request that does not carry a valid Slack signature (NFR4).
    Fails closed when no signing secret is configured.

    :raises fastapi.HTTPException: 401 on a missing or invalid signature.
    """
    if not secret or not SignatureVerifier(secret).is_valid(
        body=body,
        timestamp=headers.get("x-slack-request-timestamp", ""),
        signature=headers.get("x-slack-signature", ""),
    ):
        raise fastapi.HTTPException(status_code=401, detail="invalid Slack signature")


def _to_support_request(payload: dict[str, object]) -> entities.SupportRequest | None:
    """
    Normalize a Slack event into a ``SupportRequest``, or return None for
    events Otto must ignore (bots and message edits — the loop guard —
    and event types it does not handle).
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    if event.get("bot_id") or event.get("subtype"):
        return None
    is_mention = event.get("type") == "app_mention"
    is_dm = event.get("type") == "message" and event.get("channel_type") == "im"
    if not (is_mention or is_dm):
        return None
    return entities.SupportRequest(
        id=str(payload.get("event_id", "")),
        user_id=str(event["user"]),
        text=str(event.get("text", "")),
        origin=entities.SlackThread(
            channel_id=str(event["channel"]),
            thread_ts=str(event.get("thread_ts") or event["ts"]),
        ),
    )


def _to_approval_decision(payload: dict[str, object]) -> _ApprovalDecision | None:
    if payload.get("type") != "block_actions":
        return None
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        return None
    action = actions[0]
    action_id = action.get("action_id")
    if action_id not in (slack_vendor.APPROVE_ACTION_ID, slack_vendor.DENY_ACTION_ID):
        return None
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    return _ApprovalDecision(
        approval_id=str(action.get("value", "")),
        resolver_id=str(user.get("id", "")),
        approved=action_id == slack_vendor.APPROVE_ACTION_ID,
    )


async def _handle_safely(request: entities.SupportRequest) -> None:
    """
    FR8 wrapper: a crashed handler still yields an origin-visible apology
    and an alert-worthy log — never a silent drop.
    """
    try:
        await support.handle_support_request(request=request)
    except Exception as exc:
        logs.log_exception(exc, params={"request_id": request.id})
        try:
            await support.notify_failure(request=request)
        except Exception as notify_exc:
            logs.log_exception(notify_exc, params={"request_id": request.id})


async def _resolve_safely(decision: _ApprovalDecision) -> None:
    try:
        await support.resolve_approval(
            approval_id=decision.approval_id,
            resolver_id=decision.resolver_id,
            approved=decision.approved,
        )
    except Exception as exc:
        logs.log_exception(exc, params={"approval_id": decision.approval_id})
