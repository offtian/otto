"""
Background-safe entry points for the support use-cases, plus the small input
value objects the interface maps transport payloads into.

Each function wraps one use-case for fire-and-forget execution after the
intake ack (NFR1). FR8: a crashed handler never drops silently — it logs, and
the request path additionally posts a user-visible apology at the origin. The
interface parses/validates payloads into these inputs and schedules these
functions; it holds no orchestration itself.
"""

import attrs

from otto.application import support
from otto.domain.support import entities
from otto.utils import logs


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


async def handle_safely(request: entities.SupportRequest) -> None:
    """
    Run the support handler; on failure, log and post an origin-visible
    apology (FR8) — never a silent drop.
    """
    try:
        await support.handle_support_request(request=request)
    except Exception as exc:
        logs.log_exception(exc, params={"request_id": request.id})
        try:
            await support.notify_failure(request=request)
        except Exception as notify_exc:
            logs.log_exception(notify_exc, params={"request_id": request.id})


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
