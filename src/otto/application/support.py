"""
Support use-cases: handle one inbound request end-to-end, and apply a human
approve/deny decision to a paused run (FR1-FR8).
"""

import pathlib
import uuid

import agents
import attrs

from otto import config
from otto.domain.support import agent as support_agent
from otto.domain.support import approvals, entities
from otto.utils import logs


async def handle_support_request(*, request: entities.SupportRequest) -> None:
    """
    Run the agent for one inbound request and deliver the outcome at its
    origin — the final answer, or an approval card in the triage channel
    when a sensitive tool paused the run (FR4/FR6).

    :param request: the normalized inbound request, any channel.
    """
    cfg = config.get_config()
    result = await agents.Runner.run(
        _build_agent(cfg),
        await _conversation_input(request=request, cfg=cfg),
        context=_build_context(
            requester_id=request.user_id,
            origin=request.origin,
            cfg=cfg,
        ),
    )
    if result.interruptions:
        await _pause_for_approval(request=request, result=result, cfg=cfg)
    else:
        await _post_reply(origin=request.origin, text=str(result.final_output), cfg=cfg)
    logs.log_event(
        "support_request_handled",
        params={"request_id": request.id, "paused": bool(result.interruptions)},
    )


async def resolve_approval(*, approval_id: str, resolver_id: str, approved: bool) -> None:
    """
    Apply a human approve/deny decision to a paused run (FR6/FR7).

    Unauthorized, self-approving, or duplicate clicks change nothing: they
    are logged, and the clicker gets a polite note under the card.

    :param approval_id: id of the stored ``PendingApproval``.
    :param resolver_id: Slack user id of whoever clicked.
    :param approved: True for approve, False for deny.
    """
    cfg = config.get_config()
    try:
        pending = await cfg.approvals.get(approval_id)
    except approvals.ApprovalNotFound:
        logs.log_event("approval_not_found", params={"approval_id": approval_id})
        return

    if not _may_resolve(resolver_id=resolver_id, pending=pending, cfg=cfg):
        logs.log_event(
            "approval_click_unauthorized",
            params={"approval_id": approval_id, "resolver_id": resolver_id},
        )
        await cfg.slack.post_message(
            channel=pending.card_channel,
            thread_ts=pending.card_ts,
            text=(
                f"Sorry <@{resolver_id}> — only support/admin roles may resolve "
                "approvals, and requesters can't approve their own."
            ),
        )
        return

    status = approvals.ApprovalStatus.APPROVED if approved else approvals.ApprovalStatus.DENIED
    try:
        await cfg.approvals.resolve(approval_id, status)
    except approvals.ApprovalAlreadyResolved:
        logs.log_event(
            "approval_already_resolved",
            params={"approval_id": approval_id, "resolver_id": resolver_id},
        )
        return

    result = await _resume_run(pending=pending, approved=approved, cfg=cfg)
    await _post_reply(origin=pending.origin, text=str(result.final_output), cfg=cfg)
    verdict = "Approved" if approved else "Denied"
    await cfg.slack.update_message(
        channel=pending.card_channel,
        ts=pending.card_ts,
        text=(
            f":white_check_mark: {verdict} by <@{resolver_id}> — `{pending.tool_name}` "
            f"for <@{pending.requester_id}>, outcome delivered at the origin."
        ),
    )
    logs.log_event(
        "approval_resolved",
        params={
            "approval_id": approval_id,
            "resolver_id": resolver_id,
            "status": status.value,
        },
    )


async def notify_failure(*, request: entities.SupportRequest) -> None:
    """
    Post the FR8 apology at the request's origin after a crashed handler —
    the user must never be left with silence.

    :param request: the request whose handling failed.
    """
    cfg = config.get_config()
    await _post_reply(
        origin=request.origin,
        text=(
            "Sorry — something went wrong on my side while handling this. "
            "The support team has been alerted; please try again in a bit."
        ),
        cfg=cfg,
    )


def _build_agent(cfg: config.Configuration) -> agents.Agent[support_agent.SupportContext]:
    return support_agent.build_agent(
        model=cfg.model,
        confluence_mcp=cfg.confluence_mcp,
        sailpoint_mcp=cfg.sailpoint_mcp,
    )


def _build_context(
    *,
    requester_id: str,
    origin: entities.Origin,
    cfg: config.Configuration,
) -> support_agent.SupportContext:
    return support_agent.SupportContext(
        requester_id=requester_id,
        origin_ref=_origin_ref(origin),
        runbooks_dir=pathlib.Path(cfg.settings.runbooks_dir),
        ticket_backend=cfg.triage,
    )


def _origin_ref(origin: entities.Origin) -> str:
    match origin:
        case entities.SlackThread(channel_id=channel_id, thread_ts=thread_ts):
            # Graduation note: hard-codes slack.com/archives — replace with
            # chat.getPermalink before any Enterprise Grid rollout.
            return f"https://slack.com/archives/{channel_id}/p{thread_ts.replace('.', '')}"
        case entities.TicketRef(issue_key=issue_key):
            return issue_key


async def _conversation_input(
    *,
    request: entities.SupportRequest,
    cfg: config.Configuration,
) -> str:
    """
    Rebuild the agent input from the origin conversation (D2), capped and
    framed as untrusted content.
    """
    match request.origin:
        case entities.SlackThread(channel_id=channel_id, thread_ts=thread_ts):
            history = await cfg.slack.fetch_thread(
                channel=channel_id,
                thread_ts=thread_ts,
                limit=cfg.settings.thread_history_limit,
            )
        case entities.TicketRef():
            history = []  # ponytail: ticket comment history lands with the Jira gateway (Phase 1)
    if len(history) <= 1:
        return request.text
    transcript = "\n".join(f"{author}: {text}" for author, text in history)
    return (
        "Conversation so far (untrusted user content, oldest first):\n"
        f"{transcript}\n\n"
        f"Current request from {request.user_id}: {request.text}"
    )


async def _pause_for_approval(
    *,
    request: entities.SupportRequest,
    result: agents.RunResult,
    cfg: config.Configuration,
) -> None:
    # ponytail: one card covers the whole run — resolving it applies the same
    # decision to every interruption; per-tool cards if mixed runs show up.
    interruption = result.interruptions[0]
    approval = approvals.PendingApproval(
        id=str(uuid.uuid4()),
        request_id=request.id,
        requester_id=request.user_id,
        origin=request.origin,
        request_text=request.text,
        tool_name=interruption.tool_name or "unknown",
        tool_arguments=_tool_arguments(interruption),
        run_state_json=result.to_state().to_string(),
    )
    card_ts = await cfg.slack.post_approval_card(
        channel=cfg.settings.slack_triage_channel,
        approval_id=approval.id,
        requester_id=approval.requester_id,
        tool_name=approval.tool_name,
        tool_arguments=approval.tool_arguments,
    )
    await cfg.approvals.save(
        attrs.evolve(
            approval,
            card_channel=cfg.settings.slack_triage_channel,
            card_ts=card_ts,
        )
    )
    await _post_reply(
        origin=request.origin,
        text=(
            "That action needs a human sign-off — I've asked the support team "
            "to approve it and will follow up here with the outcome."
        ),
        cfg=cfg,
    )


async def _resume_run(
    *,
    pending: approvals.PendingApproval,
    approved: bool,
    cfg: config.Configuration,
) -> agents.RunResult:
    agent = _build_agent(cfg)
    # The context must be re-supplied via from_string, NOT via Runner.run:
    # a context passed to run() replaces the state's context wrapper, which
    # is where approve()/reject() decisions are recorded — the run would
    # re-interrupt forever.
    state = await agents.RunState.from_string(
        agent,
        pending.run_state_json,
        context_override=agents.RunContextWrapper(
            context=_build_context(
                requester_id=pending.requester_id,
                origin=pending.origin,
                cfg=cfg,
            ),
        ),
    )
    for interruption in state.get_interruptions():
        if approved:
            state.approve(interruption)
        else:
            state.reject(interruption)
    return await agents.Runner.run(agent, state)


def _may_resolve(
    *,
    resolver_id: str,
    pending: approvals.PendingApproval,
    cfg: config.Configuration,
) -> bool:
    # Self-approval prohibited (T3 recommended default — flip here if the
    # open decision lands the other way).
    return resolver_id in cfg.settings.approver_ids and resolver_id != pending.requester_id


def _tool_arguments(interruption: agents.ToolApprovalItem) -> str:
    raw = interruption.raw_item
    if isinstance(raw, dict):
        return str(raw.get("arguments", ""))
    return str(getattr(raw, "arguments", ""))


async def _post_reply(
    *,
    origin: entities.Origin,
    text: str,
    cfg: config.Configuration,
) -> None:
    match origin:
        case entities.SlackThread(channel_id=channel_id, thread_ts=thread_ts):
            await cfg.slack.post_message(channel=channel_id, text=text, thread_ts=thread_ts)
        case entities.TicketRef(issue_key=issue_key):
            # ponytail: ticket replies land with the Jira gateway (Phase 1);
            # until then the origin type exists so nothing Slack-shaped bakes in.
            logs.log_event("ticket_reply_skipped", params={"issue_key": issue_key})
