"""
Jira router: ticket webhook intake.

Verifies the webhook secret, filters Otto's own actions (FR9 loop guard),
records the native resolved-status signal (D6), and dispatches new issues and
comments to a background task. Payload validation and domain mapping live in
``interfaces.schemas`` — no business logic here.
"""

import json
from datetime import UTC, datetime

import fastapi

from otto import config
from otto.application import dispatch
from otto.interfaces import schemas
from otto.utils import logs
from otto.vendors import jira as jira_vendor


router = fastapi.APIRouter()


@router.post("/jira/webhook")
async def jira_webhook(
    request: fastapi.Request,
    background: fastapi.BackgroundTasks,
) -> fastapi.Response:
    cfg = config.get_config()
    jira = cfg.jira
    body = await request.body()
    if jira is None or not jira_vendor.verify_webhook(
        secret=cfg.settings.jira_webhook_secret,
        body=body,
        signature=request.headers.get("x-hub-signature", ""),
        url_token=request.query_params.get("secret", ""),
    ):
        raise fastapi.HTTPException(status_code=401, detail="invalid Jira webhook secret")
    webhook = schemas.JiraWebhook.parse(json.loads(body))
    if webhook is None:
        return fastapi.Response()
    resolved_key = webhook.resolution_key()
    if resolved_key is not None:
        # Native D6 resolution signal — pure telemetry, so deliberately not
        # gated by the kill switch (the ticket resolved regardless of Otto).
        if not request.app.state.recent_events.seen(
            f"jira:resolved:{webhook.resolution_dedup_id()}"
        ):
            logs.log_event(
                "request_resolved",
                params={"signal": "ticket_status", "issue_key": resolved_key},
            )
        return fastapi.Response()
    support_request = webhook.to_support_request()
    if support_request is None or not await request.app.state.kill_switch.enabled(
        now=datetime.now(tz=UTC)
    ):
        return fastapi.Response()
    if request.app.state.recent_events.seen(support_request.id):
        return fastapi.Response()
    # C2: global cap before the own-actor lookup and the LLM — a bulk
    # import/transition storm is acked, logged loudly, and dropped.
    if not request.app.state.jira_rate_limiter.allow("jira", now=datetime.now(tz=UTC)):
        logs.log_event("jira_rate_limited", params={"event_id": support_request.id})
        return fastapi.Response()
    if await _is_own_jira_actor(app_=request.app, jira=jira, actor_id=support_request.user_id):
        return fastapi.Response()
    background.add_task(dispatch.handle_safely, support_request)
    return fastapi.Response()


async def _is_own_jira_actor(
    *,
    app_: fastapi.FastAPI,
    jira: jira_vendor.JiraGateway,
    actor_id: str,
) -> bool:
    """
    Test whether the webhook actor is Otto itself (the FR9 loop guard).
    Fails closed: while the bot identity cannot be established, every event is
    treated as Otto's own — a reply loop can never start.
    """
    bot_id: str | None = app_.state.jira_bot_account_id
    if bot_id is None:
        try:
            bot_id = await jira.get_myself_account_id()
        except Exception as exc:
            logs.log_exception(exc, params={"guard": "jira_own_actor"})
            return True
        app_.state.jira_bot_account_id = bot_id
    return actor_id == bot_id
