"""
Support use-cases: handle one inbound request end-to-end, and apply a human
approve/deny decision to a paused run (FR1-FR8).
"""

import json
import pathlib
import uuid
from datetime import datetime, timedelta

import agents
import attrs

from otto import config
from otto.domain.identity import users as identity_users
from otto.domain.support import agent as support_agent
from otto.domain.support import approvals, audit, entities
from otto.utils import logs


async def handle_support_request(*, request: entities.SupportRequest) -> None:
    """
    Run the agent for one inbound request and deliver the outcome at its
    origin — the final answer, or an approval card in the triage channel
    when a sensitive tool paused the run (FR4/FR6).

    :param request: the normalized inbound request, any channel.
    """
    cfg = config.get_config()
    await _begin_thinking(origin=request.origin, cfg=cfg)
    result = await agents.Runner.run(
        cfg.agent,
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
        await _post_answer(origin=request.origin, text=str(result.final_output), cfg=cfg)
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

    rejection = await _resolve_rejection(resolver_id=resolver_id, pending=pending, cfg=cfg)
    if rejection is not None:
        logs.log_event(
            "approval_click_rejected",
            params={"approval_id": approval_id, "resolver_id": resolver_id, "reason": rejection},
        )
        # B5: rejected attempts are durable audit rows, not just log lines.
        await cfg.approvals.record_event(
            audit.AuditEvent(event_type=rejection, actor_id=resolver_id, approval_id=approval_id)
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
        await cfg.approvals.resolve(approval_id, status, resolver_id=resolver_id)
    except approvals.ApprovalAlreadyResolved:
        logs.log_event(
            "approval_already_resolved",
            params={"approval_id": approval_id, "resolver_id": resolver_id},
        )
        return

    result = await _resume_run(pending=pending, approved=approved, cfg=cfg)
    await _post_reply(origin=pending.origin, text=str(result.final_output), cfg=cfg)
    verdict = "Approved" if approved else "Denied"
    requester = await _requester_label(
        user_id=pending.requester_id, origin=pending.origin, cfg=cfg
    )
    await cfg.slack.update_message(
        channel=pending.card_channel,
        ts=pending.card_ts,
        text=(
            f":white_check_mark: {verdict} by <@{resolver_id}> — `{pending.tool_name}` "
            f"for {requester}, outcome delivered at the origin."
        ),
    )
    # B2 disposition stamp: a restart resumes any terminal approval whose
    # outcome never landed. ponytail: stamped after delivery, so a crash
    # mid-delivery re-fires the approved tool on recovery — a duplicate,
    # visible SailPoint request, never an unapproved one; a two-phase
    # started/finished stamp is the upgrade if duplicates ever matter.
    await cfg.approvals.mark_executed(approval_id)
    logs.log_event(
        "approval_resolved",
        params={
            "approval_id": approval_id,
            "resolver_id": resolver_id,
            "status": status.value,
        },
    )
    if approved:
        # Native D6 resolution signal — the gated tool ran (SailPoint submission).
        logs.log_event(
            "request_resolved",
            params={"signal": "access_granted", "request_id": pending.request_id},
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


# Agent-mode starter prompts (3.5) — (button label, message sent on tap). These
# are content, like the runbooks, so they live in code, not settings.
_SUGGESTED_PROMPTS = (
    ("Reset my password", "How do I reset my password?"),
    ("Set up the VPN", "Walk me through setting up the VPN"),
    ("Request system access", "I need access to a system for my work"),
)


async def greet_assistant_thread(*, channel: str, thread_ts: str) -> None:
    """
    Greet a newly opened assistant thread and offer starter prompts — the
    agent-mode entry experience (3.5). Cards for sensitive actions still go to
    the triage channel; this is only the requester-facing welcome.

    :param channel: the assistant thread's channel id.
    :param thread_ts: the assistant thread's root ts.
    """
    cfg = config.get_config()
    await cfg.slack.post_message(
        channel=channel,
        thread_ts=thread_ts,
        text=(
            "Hi — I'm Otto, your tech-support assistant. Ask me a how-to "
            "question, walk through a runbook, or request access to a system. "
            "I cite my sources, and anything sensitive goes to a human first."
        ),
    )
    await cfg.slack.set_suggested_prompts(
        channel=channel,
        thread_ts=thread_ts,
        title="Try one of these",
        prompts=list(_SUGGESTED_PROMPTS),
    )


async def record_feedback(*, helpful: bool, origin: entities.Origin, voter_id: str) -> None:
    """
    Apply a requester's "Did this help?" vote on a Slack answer (T2, the
    Slack-side D6 signal). A yes records a resolution; a no loops in a human
    and records nothing as resolved. Either way the voter gets an origin
    reply confirming the vote landed.

    :param helpful: True for the yes vote, False for the no vote.
    :param origin: the answered conversation (always a Slack thread today —
        the vote buttons ride only on Slack answers).
    :param voter_id: Slack user id of whoever clicked.
    """
    cfg = config.get_config()
    if helpful:
        logs.log_event(
            "request_resolved",
            params={"signal": "helpful_vote", "origin": _origin_ref(origin), "voter_id": voter_id},
        )
        await _post_reply(origin=origin, text="Glad that helped! :tada:", cfg=cfg)
        return
    await cfg.triage.escalate(
        subject="Unresolved support answer",
        summary="The requester reported that my answer did not resolve their issue.",
        urgency="normal",
        requester_id=voter_id,
        origin_ref=_origin_ref(origin),
    )
    logs.log_event(
        "feedback_negative",
        params={"origin": _origin_ref(origin), "voter_id": voter_id},
    )
    await _post_reply(
        origin=origin,
        text="Sorry that didn't help — I've flagged this for a human to take a look.",
        cfg=cfg,
    )


async def mark_resolved(
    *, origin_ref: str, resolver_id: str, card_channel: str, card_ts: str
) -> None:
    """
    Apply a support agent's "Mark resolved" click on an escalation card — the
    D6 support-agent-marks-resolved signal. Role-gated to support/admin (D3);
    an unauthorized click changes nothing and gets a polite note under the card.

    :param origin_ref: the escalated request's origin reference (button value).
    :param resolver_id: Slack user id of whoever clicked.
    :param card_channel: channel of the escalation card (to update / reply).
    :param card_ts: ts of the escalation card.
    """
    cfg = config.get_config()
    if not await _is_authorized_approver(resolver_id=resolver_id, cfg=cfg):
        logs.log_event(
            "resolve_click_unauthorized",
            params={"resolver_id": resolver_id, "origin_ref": origin_ref},
        )
        await cfg.slack.post_message(
            channel=card_channel,
            thread_ts=card_ts,
            text=f"Sorry <@{resolver_id}> — only support/admin roles may mark escalations resolved.",
        )
        return
    logs.log_event(
        "request_resolved",
        params={"signal": "agent_marked", "origin_ref": origin_ref, "resolver_id": resolver_id},
    )
    await cfg.slack.update_message(
        channel=card_channel,
        ts=card_ts,
        text=f":white_check_mark: Escalation resolved by <@{resolver_id}>.",
    )


async def sweep_approvals(*, now: datetime) -> None:
    """
    Run the periodic approval-maintenance sweep (2.5): expire stale pending
    approvals and close out their cards, nudge the triage channel about the
    ones still waiting, and purge resolved run state past its retention
    window (A9 — the serialized conversation is PII at rest).

    :param now: reference time, injected so the sweep is deterministic and the
        clock is read at the interface layer, not inside the use-case.
    """
    cfg = config.get_config()
    s = cfg.settings

    # Expire first: an approval that ages out this tick must not also be
    # reminded. Expiry is terminal, so a later click hits the resolve() guard.
    expired = await cfg.approvals.expire_pending(
        cutoff=now - timedelta(minutes=s.approval_expiry_minutes)
    )
    for approval in expired:
        await cfg.slack.update_message(
            channel=approval.card_channel,
            ts=approval.card_ts,
            text=(
                f":hourglass: Expired — `{approval.tool_name}` was not approved "
                "in time and will not run."
            ),
        )
        await _post_reply(
            origin=approval.origin,
            text=(
                "That request expired before anyone approved it, so I didn't run "
                "it. Ask again if you still need it."
            ),
            cfg=cfg,
        )
        # B5: an expiry is an audit fact — nobody decided in time.
        await cfg.approvals.record_event(
            audit.AuditEvent(
                event_type="expired", actor_id="system:sweep", approval_id=approval.id
            )
        )

    reminders = await cfg.approvals.claim_due_reminders(
        cutoff=now - timedelta(minutes=s.approval_reminder_minutes)
    )
    for approval in reminders:
        await cfg.slack.post_message(
            channel=approval.card_channel,
            thread_ts=approval.card_ts,
            text=(
                f":bell: Still waiting on a decision for `{approval.tool_name}` — "
                "approve or deny on the card above."
            ),
        )

    purged = await cfg.approvals.purge_resolved_state(
        cutoff=now - timedelta(days=s.approval_retention_days)
    )
    logs.log_event(
        "approval_sweep",
        params={"expired": len(expired), "reminded": len(reminders), "purged": purged},
    )


async def recover_approvals() -> None:
    """
    Finish terminal approvals whose decided run never completed (B2): a crash
    between the resolve and the resumed run's outcome delivery leaves the
    decision recorded but unexecuted. Auto-resume (decided 2026-07-19): the
    human decision already happened, so recovery carries it out and reports at
    the origin. A failed recovery is dispositioned with an origin apology so a
    poisoned row can never crash-loop the startup path.
    """
    cfg = config.get_config()
    for pending in await cfg.approvals.list_unexecuted():
        approved = pending.status is approvals.ApprovalStatus.APPROVED
        try:
            result = await _resume_run(pending=pending, approved=approved, cfg=cfg)
            await _post_reply(origin=pending.origin, text=str(result.final_output), cfg=cfg)
            logs.log_event(
                "approval_recovered",
                params={"approval_id": pending.id, "status": pending.status.value},
            )
        except Exception as exc:
            logs.log_exception(exc, params={"approval_id": pending.id, "job": "approval_recovery"})
            await _post_reply(
                origin=pending.origin,
                text=(
                    "Sorry — I couldn't finish handling your request after a "
                    "restart. The support team has been alerted; please ask again."
                ),
                cfg=cfg,
            )
        await cfg.approvals.mark_executed(pending.id)


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
        case entities.TicketRef(issue_key=issue_key):
            history = (
                await cfg.jira.fetch_conversation(
                    issue_key=issue_key,
                    limit=cfg.settings.thread_history_limit,
                )
                if cfg.jira is not None
                else []
            )
    requester = await _requester_description(user_id=request.user_id, cfg=cfg)
    if len(history) <= 1:
        return f"Request from {requester}: {request.text}"
    transcript = "\n".join(f"{author}: {text}" for author, text in history)
    return (
        "Conversation so far (untrusted user content, oldest first):\n"
        f"{transcript}\n\n"
        f"Current request from {requester}: {request.text}"
    )


async def _requester_description(*, user_id: str, cfg: config.Configuration) -> str:
    """
    Return the requester as the agent should see them: name + team when
    the directory knows them, the raw channel id otherwise.
    """
    user = await cfg.directory.find(user_id)
    return f"{user.name} (team: {user.team})" if user is not None else user_id


async def _requester_label(
    *,
    user_id: str,
    origin: entities.Origin,
    cfg: config.Configuration,
) -> str:
    """
    Return the requester as Slack mrkdwn for approval cards: mention +
    team when the directory knows them; a bare mention only when the id
    is a Slack id (a Jira account id renders as garbage inside <@...>).
    """
    user = await cfg.directory.find(user_id)
    if user is not None:
        mention = f"<@{user.slack_user_id}>" if user.slack_user_id else user.name
        return f"{mention} ({user.team})"
    if isinstance(origin, entities.SlackThread):
        return f"<@{user_id}>"
    return user_id


async def _pause_for_approval(
    *,
    request: entities.SupportRequest,
    result: agents.RunResult,
    cfg: config.Configuration,
) -> None:
    # One card still covers the whole run (one decision), but it names every
    # interrupted call (B3) — the approver never authorizes an unseen tool.
    # Per-tool cards remain the upgrade if mixed runs show up for real.
    interruptions = result.interruptions
    tool_name = " + ".join(dict.fromkeys((i.tool_name or "unknown") for i in interruptions))
    if len(interruptions) == 1:
        tool_arguments = _tool_arguments(interruptions[0])
    else:
        tool_arguments = json.dumps(
            [
                {"tool": i.tool_name or "unknown", "arguments": _tool_arguments(i)}
                for i in interruptions
            ]
        )

    # A re-triggered event on the same conversation (e.g. the requester nudges
    # the ticket during the approval gap) reruns the agent and can reach the
    # same gated tool again. Suppress the duplicate card — the earlier run is
    # already waiting, and resolving it delivers the outcome here. A different
    # request on the same tool is NOT a duplicate (B4): arguments compare too.
    # ponytail: dedupe catches the human-paced re-trigger; a sub-second
    # double-fire could still race past it at single-replica. Add a partial
    # unique index (status='pending') if multi-replica makes that real.
    if (
        await cfg.approvals.find_pending(
            origin=request.origin, tool_name=tool_name, tool_arguments=tool_arguments
        )
        is not None
    ):
        logs.log_event(
            "approval_duplicate_suppressed",
            params={"request_id": request.id, "tool_name": tool_name},
        )
        await _post_reply(
            origin=request.origin,
            text=(
                "I'm already waiting on a human OK to file this for you — no "
                "need to ask again; I'll follow up here once it's approved."
            ),
            cfg=cfg,
        )
        return

    approval = approvals.PendingApproval(
        id=str(uuid.uuid4()),
        request_id=request.id,
        requester_id=request.user_id,
        origin=request.origin,
        request_text=request.text,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        run_state_json=result.to_state().to_string(),
    )
    requester = await _requester_label(
        user_id=approval.requester_id, origin=request.origin, cfg=cfg
    )
    card_ts = await cfg.slack.post_approval_card(
        channel=cfg.settings.slack_triage_channel,
        approval_id=approval.id,
        requester=requester,
        tool_name=approval.tool_name,
        summary=_approval_summary(tool_arguments=approval.tool_arguments),
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
            "This needs a human OK before I file it on your behalf — I've "
            "asked the support team and will follow up here with the outcome."
        ),
        cfg=cfg,
    )


async def _resume_run(
    *,
    pending: approvals.PendingApproval,
    approved: bool,
    cfg: config.Configuration,
) -> agents.RunResult:
    # The context must be re-supplied via from_string, NOT via Runner.run:
    # a context passed to run() replaces the state's context wrapper, which
    # is where approve()/reject() decisions are recorded — the run would
    # re-interrupt forever.
    state = await agents.RunState.from_string(
        cfg.agent,
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
    return await agents.Runner.run(cfg.agent, state)


async def _resolve_rejection(
    *,
    resolver_id: str,
    pending: approvals.PendingApproval,
    cfg: config.Configuration,
) -> str | None:
    """
    Return why this click may not resolve the approval (an audit event type),
    or None when it may. Self-approval prohibited (T3). Cross-channel resolves
    fail closed (B1): a ticket's requester id is a Jira account id while the
    click is a Slack id, so id equality proves nothing — the clicker must be a
    mapped identity whose Jira id is known and provably not the requester's.
    An unmapped clicker *could be* the requester; deny rather than guess.
    """
    if not await _is_authorized_approver(resolver_id=resolver_id, cfg=cfg):
        return "unauthorized_role"
    if isinstance(pending.origin, entities.TicketRef):
        resolver = await cfg.directory.find(resolver_id)
        if resolver is None or not resolver.jira_account_id:
            return "unverified_identity"
        if resolver.jira_account_id == pending.requester_id:
            return "self_approval"
        return None
    if await cfg.directory.same_person(resolver_id, pending.requester_id):
        return "self_approval"
    return None


async def _is_authorized_approver(*, resolver_id: str, cfg: config.Configuration) -> bool:
    """
    Test whether the clicker holds an approver role (D3). The DB ``role`` is
    the source of truth (2.3); the settings id-lists are the bootstrap
    fallback for users not yet migrated. Fail-closed: a role-lookup outage
    denies rather than risking an unauthorized approval.
    """
    try:
        role = await cfg.directory.role(resolver_id)
    except Exception as exc:
        logs.log_exception(exc, params={"guard": "role_lookup", "resolver_id": resolver_id})
        return False
    if role in identity_users.APPROVER_ROLES:
        return True
    return resolver_id in cfg.settings.approver_ids


def _tool_arguments(interruption: agents.ToolApprovalItem) -> str:
    raw = interruption.raw_item
    if isinstance(raw, dict):
        return str(raw.get("arguments", ""))
    return str(getattr(raw, "arguments", ""))


def _approval_summary(*, tool_arguments: str) -> str:
    """
    Render the paused arguments as readable mrkdwn for the approver — the
    target and business justification of an access request, not raw JSON — so
    they can judge whether to let Otto file it on the requester's behalf.
    When several calls paused in one run (B3), every one gets its own block.
    """
    try:
        args = json.loads(tool_arguments)
    except (json.JSONDecodeError, TypeError):
        args = None
    if isinstance(args, list):
        return "\n\n".join(
            f"*Tool:* `{entry.get('tool', 'tool')}`\n"
            + _arguments_block(str(entry.get("arguments", "")))
            for entry in args
            if isinstance(entry, dict)
        )
    return _arguments_block(tool_arguments)


def _arguments_block(tool_arguments: str) -> str:
    """
    Render one tool's argument string: labeled access-request fields when the
    args are the access shape, the raw arguments fenced otherwise.
    """
    try:
        args = json.loads(tool_arguments)
    except (json.JSONDecodeError, TypeError):
        args = None
    if not isinstance(args, dict):
        return f"```{tool_arguments}```" if tool_arguments else "_no details provided_"
    labels = {
        "system": "System",
        "entitlement": "Entitlement",
        "justification": "Business justification",
    }
    return "\n".join(f"*{labels.get(key, key)}:* {value}" for key, value in args.items())


async def _post_answer(
    *,
    origin: entities.Origin,
    text: str,
    cfg: config.Configuration,
) -> None:
    """
    Deliver a knowledge answer at its origin. Slack answers carry a "Did
    this help?" resolution vote (T2); ticket answers rely on the native
    status signal (D6), so they post as a plain comment.

    ponytail: the vote rides every non-paused Slack answer, so it also
    trails intermediate runbook-walkthrough steps — mildly naggy. Upgrade:
    have the agent flag a terminal answer and vote only on those.
    """
    match origin:
        case entities.SlackThread(channel_id=channel_id, thread_ts=thread_ts):
            await cfg.slack.post_answer(
                channel=channel_id,
                text=text,
                thread_ts=thread_ts,
                feedback_value=_feedback_value(origin),
            )
        case entities.TicketRef():
            await _post_reply(origin=origin, text=text, cfg=cfg)


def _feedback_value(origin: entities.SlackThread) -> str:
    """
    Encode a Slack thread into a button value the interaction handler can
    split back into its channel and thread ts.
    """
    return f"{origin.channel_id}:{origin.thread_ts}"


async def _begin_thinking(*, origin: entities.Origin, cfg: config.Configuration) -> None:
    """
    Show the assistant "is working" cue while Otto processes a Slack request
    (agent-mode, 3.5). Assistant threads are DMs (channel ids start with "D");
    channel mentions have no such indicator and are skipped. Best-effort — the
    status is pure UX, so a failure must never block the actual reply. Posting
    the answer later clears the indicator, so no explicit clear is needed.
    """
    if not isinstance(origin, entities.SlackThread) or not origin.channel_id.startswith("D"):
        return
    try:
        await cfg.slack.set_status(
            channel=origin.channel_id,
            thread_ts=origin.thread_ts,
            status="Otto is looking into this…",
        )
    except Exception as exc:
        logs.log_exception(exc, params={"status": "set_thinking"})


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
            if cfg.jira is None:
                # Unreachable while the webhook is the only ticket intake —
                # it rejects events whenever the gateway is unconfigured.
                logs.log_event("ticket_reply_skipped", params={"issue_key": issue_key})
            else:
                await cfg.jira.post_comment(issue_key=issue_key, text=text)
