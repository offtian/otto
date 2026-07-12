"""
Slack router: event intake and interactive (Block Kit) actions.

Verifies the request signature (NFR4), acks immediately, and dispatches the
mapped domain/DTO object to a background task (NFR1). Payload validation and
domain mapping live in ``interfaces.schemas`` — no business logic here.
"""

import json
import urllib.parse
from collections.abc import Mapping

import fastapi
from slack_sdk.signature import SignatureVerifier

from otto import config
from otto.application import dispatch
from otto.interfaces import schemas


router = fastapi.APIRouter()


@router.post("/slack/events")
async def slack_events(
    request: fastapi.Request,
    background: fastapi.BackgroundTasks,
) -> fastapi.Response:
    cfg = config.get_config()
    body = await request.body()
    _require_valid_signature(
        secret=cfg.settings.slack_signing_secret, body=body, headers=request.headers
    )
    envelope = schemas.SlackEventEnvelope.parse(json.loads(body))
    if envelope is None:
        return fastapi.Response()
    if envelope.type == "url_verification":
        return fastapi.responses.JSONResponse({"challenge": envelope.challenge})
    if request.app.state.recent_events.seen(envelope.event_id):
        return fastapi.Response()
    support_request = envelope.to_support_request()
    if support_request is None or not cfg.settings.otto_enabled:
        return fastapi.Response()
    background.add_task(dispatch.handle_safely, support_request)
    return fastapi.Response()


@router.post("/slack/interactions")
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
    interaction = schemas.SlackInteraction.parse(json.loads(form.get("payload", ["{}"])[0]))
    if interaction is None or not cfg.settings.otto_enabled:
        return fastapi.Response()
    decision = interaction.to_approval_decision()
    if decision is not None:
        background.add_task(dispatch.resolve_safely, decision)
        return fastapi.Response()
    vote = interaction.to_feedback_vote()
    if vote is not None:
        background.add_task(dispatch.record_feedback_safely, vote)
        return fastapi.Response()
    resolve = interaction.to_resolve_click()
    if resolve is not None:
        background.add_task(dispatch.mark_resolved_safely, resolve)
    return fastapi.Response()


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
