"""
GitHub router: webhook intake for the pull requests Otto opens.

Verifies the webhook signature, maps a merge, a closure or a request for
changes to a request on the conversation that asked for the pull request, and
dispatches it to a background task like any other inbound request. Every
other event, and every pull request that does not carry Otto's signed return
address, is acked and ignored. Payload validation and domain mapping live in
``interfaces.schemas`` — no business logic here.
"""

import json
from datetime import UTC, datetime

import fastapi

from otto import config
from otto.application import dispatch
from otto.interfaces import schemas
from otto.vendors import github as github_vendor


router = fastapi.APIRouter()


@router.post("/github/webhook")
async def github_webhook(
    request: fastapi.Request,
    background: fastapi.BackgroundTasks,
) -> fastapi.Response:
    secret = config.get_config().settings.github_webhook_secret
    body = await request.body()
    if not github_vendor.verify_webhook(
        secret=secret, body=body, signature=request.headers.get("x-hub-signature-256", "")
    ):
        raise fastapi.HTTPException(status_code=401, detail="invalid GitHub webhook signature")
    webhook = schemas.GitHubWebhook.parse(json.loads(body))
    delivery_id = request.headers.get("x-github-delivery", "")
    if webhook is None or not delivery_id:
        return fastapi.Response()
    event = webhook.to_pull_request_event(
        event=request.headers.get("x-github-event", ""), signing_key=secret
    )
    if event is None or not await request.app.state.kill_switch.enabled(now=datetime.now(tz=UTC)):
        return fastapi.Response()
    if request.app.state.recent_events.seen(f"github:{delivery_id}"):
        return fastapi.Response()
    support_request = dispatch.pull_request_update(event=event, delivery_id=delivery_id)
    # Durable before the ack, like every other intake: GitHub does not
    # redeliver a webhook it was told arrived.
    if not await dispatch.accept(request=support_request):
        return fastapi.Response()
    background.add_task(dispatch.handle_safely, support_request)
    return fastapi.Response()
