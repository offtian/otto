"""
Background-safe entry points for the support use-cases, plus the small input
value objects the interface maps transport payloads into.

Each function wraps one use-case for fire-and-forget execution after the
intake ack (NFR1). FR8: a crashed handler never drops silently — it logs, and
the request path additionally posts a user-visible apology at the origin. A
process that dies mid-run must not drop a request either, so requests pass
through the durable inbox: ``accept`` before the intake ack, and
``unfinished_requests`` at startup for whatever never finished. The interface
parses/validates payloads into these inputs and schedules these functions; it
holds no orchestration itself.
"""

from datetime import datetime, timedelta

import attrs

from otto import config
from otto.application import support
from otto.domain.support import entities
from otto.utils import logs


# An unfinished request older than this at startup is discarded rather than
# replayed: long enough to cover an overnight outage, short enough that a
# forgotten row never resurfaces in a conversation that moved on days ago.
_REPLAY_WINDOW = timedelta(hours=24)

# What the agent reads when a follow-up wakes it. The note is its own earlier
# words about an untrusted conversation, so it is quoted as data.
_FOLLOW_UP_PROMPT = (
    "[Scheduled follow-up — nobody wrote this message; you set it yourself "
    "earlier in this conversation.] Your note said: {note!r}. Do what the note "
    "calls for now and report the result here. If it is still not finished, say "
    "so, and schedule another follow-up only if it is worth waiting longer."
)


@attrs.frozen
class ApprovalDecision:
    approval_id: str
    resolver_id: str
    approved: bool


@attrs.frozen
class FeedbackVote:
    helpful: bool
    origin: entities.SlackThread
    voter_id: str


@attrs.frozen
class ResolveClick:
    origin_ref: str
    resolver_id: str
    card_channel: str
    card_ts: str


@attrs.frozen
class AssistantGreeting:
    channel: str
    thread_ts: str


async def accept(*, request: entities.SupportRequest) -> bool:
    """
    Record an inbound request in the durable inbox before its intake is
    acked, and return whether it should be handled — False for a redelivery
    of a request already in flight, on any replica or across a restart.

    Fail-open: with no inbox wired, or when the insert fails, the request is
    still handled — only without the restart guarantee.

    :param request: the normalized inbound request, any channel.
    """
    inbox = config.get_config().inbox
    if inbox is None:
        return True
    try:
        return await inbox.accept(request)
    except Exception as exc:
        logs.log_exception(exc, params={"request_id": request.id, "job": "inbox_accept"})
        return True


async def handle_safely(request: entities.SupportRequest) -> None:
    """
    Run the support handler; on failure, log and post an origin-visible
    apology (FR8) — never a silent drop. Either way the request then leaves
    the durable inbox; a run cut short by the process dying does not, and
    the next start replays it (``unfinished_requests``).
    """
    try:
        await support.handle_support_request(request=request)
    except Exception as exc:
        logs.log_exception(exc, params={"request_id": request.id})
        try:
            await support.notify_failure(request=request)
        except Exception as notify_exc:
            logs.log_exception(notify_exc, params={"request_id": request.id})
    # Deliberately not a ``finally``: a cancelled run (shutdown mid-flight)
    # must stay in the inbox so the next start replays it.
    await _finish(request_id=request.id)


async def unfinished_requests(*, now: datetime) -> list[entities.SupportRequest]:
    """
    Return the requests a previous process accepted but never finished: it
    died (crash, deploy, OOM) mid-run after the channel had already been told
    "received", so nobody will redeliver them. The caller replays each one
    through ``handle_safely``. Requests older than the replay window are
    discarded instead of returned.

    Call this before intake starts: every inbox row at that point was left by
    a previous process, whereas afterwards live requests share the table and
    would be replayed while still running.

    A replayed run that had already answered before the process died answers
    twice — visible, never silent. Anything gated still pauses for its own
    approval, so a replay can never execute a sensitive tool unapproved.

    ponytail: single-replica — a second replica starting up would replay
    requests the first is still running. Claim rows with a lease
    (``FOR UPDATE SKIP LOCKED``) before running replicas (Phase 3).

    :param now: reference time, injected so the window is deterministic and
        the clock is read at the interface layer.
    """
    inbox = config.get_config().inbox
    if inbox is None:
        return []
    discarded = await inbox.discard_stale(cutoff=now - _REPLAY_WINDOW)
    requests = await inbox.list_unfinished()
    logs.log_event("request_recovery", params={"replayed": len(requests), "discarded": discarded})
    return requests


async def fire_due_follow_ups(*, now: datetime) -> None:
    """
    Wake the conversations whose follow-up has come due: each becomes an
    inbound request on its conversation, on behalf of the original requester,
    with the agent's own note as the reason it woke.

    The request enters the durable inbox before the follow-up is marked
    fired, so a crash between the two re-fires it rather than losing it —
    and the outbox's one-reply-per-request key keeps that second run silent.

    ponytail: handled one after another on the always-on loop, so a slow run
    delays the next follow-up and the reply retries behind it. Hand them to
    background tasks if follow-ups ever queue up.

    :param now: reference time, read at the interface layer.
    """
    store = config.get_config().follow_ups
    if store is None:
        return
    for follow_up in await store.due(now=now):
        request = entities.SupportRequest(
            id=f"followup:{follow_up.id}",
            user_id=follow_up.requester_id,
            text=_FOLLOW_UP_PROMPT.format(note=follow_up.note),
            origin=follow_up.origin,
        )
        accepted = await accept(request=request)
        await store.mark_fired(follow_up.id)
        logs.log_event("follow_up_fired", params={"follow_up_id": follow_up.id})
        if accepted:
            await handle_safely(request)


async def _finish(*, request_id: str) -> None:
    inbox = config.get_config().inbox
    if inbox is None:
        return
    try:
        await inbox.finish(request_id)
    except Exception as exc:
        # The request was handled; a row left behind costs at most one
        # duplicate answer if the process restarts inside the replay window.
        logs.log_exception(exc, params={"request_id": request_id, "job": "inbox_finish"})


async def resolve_safely(decision: ApprovalDecision) -> None:
    try:
        await support.resolve_approval(
            approval_id=decision.approval_id,
            resolver_id=decision.resolver_id,
            approved=decision.approved,
        )
    except Exception as exc:
        logs.log_exception(exc, params={"approval_id": decision.approval_id})


async def record_feedback_safely(vote: FeedbackVote) -> None:
    try:
        await support.record_feedback(
            helpful=vote.helpful, origin=vote.origin, voter_id=vote.voter_id
        )
    except Exception as exc:
        logs.log_exception(exc, params={"feedback_channel": vote.origin.channel_id})


async def mark_resolved_safely(click: ResolveClick) -> None:
    try:
        await support.mark_resolved(
            origin_ref=click.origin_ref,
            resolver_id=click.resolver_id,
            card_channel=click.card_channel,
            card_ts=click.card_ts,
        )
    except Exception as exc:
        logs.log_exception(exc, params={"origin_ref": click.origin_ref})


async def greet_safely(greeting: AssistantGreeting) -> None:
    try:
        await support.greet_assistant_thread(
            channel=greeting.channel, thread_ts=greeting.thread_ts
        )
    except Exception as exc:
        logs.log_exception(exc, params={"assistant_thread": greeting.thread_ts})
