"""
Reply delivery: every outcome a run owes its conversation leaves through
here. With an outbox wired, ``deliver`` records the reply first, sends it at
once, and leaves a failed send to ``retry_due`` — a channel outage delays the
reply instead of losing it, and a run replayed after a crash cannot answer
the same conversation twice. Without one (no database) a reply is simply
sent, exactly as before.
"""

from datetime import datetime

from otto import config
from otto.domain.support import entities, outbox
from otto.utils import logs


async def deliver(*, key: str, origin: entities.Origin, text: str, vote: bool = False) -> None:
    """
    Deliver a run's reply at its origin, durably when an outbox is wired.

    A failed send is not raised: the reply is on record and will be retried,
    so the caller's use-case completes instead of apologizing for an answer
    that is merely late.

    :param key: idempotency key — one reply per key ever reaches the
        conversation (the request id for a run's answer or apology).
    :param origin: the conversation to reply in.
    :param text: the reply.
    :param vote: carry the "Did this help?" vote where the channel has one.
    :raises Exception: only when no outbox could record the reply and the
        channel send then fails.
    """
    store = config.get_config().outbox
    if store is None:
        await send(origin=origin, text=text, vote=vote)
        return
    try:
        message = await store.enqueue(key=key, origin=origin, text=text, vote=vote)
    except Exception as exc:
        # Fail-open like the inbox: an outbox outage costs durability, never
        # the reply.
        logs.log_exception(exc, params={"job": "outbox_enqueue", "key": key})
        await send(origin=origin, text=text, vote=vote)
        return
    if message is None:
        logs.log_event("reply_already_recorded", params={"key": key})
        return
    await _attempt(message, store=store)


async def retry_due(*, now: datetime) -> None:
    """
    Retry every recorded reply whose next attempt time has passed.

    ponytail: one sender at a time — a claimed reply is hidden for a minute,
    which is what keeps two replicas from double-sending; raise the hold if a
    channel send can ever take longer than that.

    :param now: reference time, read at the interface layer.
    """
    store = config.get_config().outbox
    if store is None:
        return
    for message in await store.claim_due(now=now):
        await _attempt(message, store=store)


async def send(*, origin: entities.Origin, text: str, vote: bool = False) -> None:
    """
    Send one reply to its channel, once, with no record — the raw channel
    send. Slack answers may carry a "Did this help?" resolution vote (T2);
    ticket answers rely on the native status signal (D6), so they post as a
    plain comment.

    ponytail: the vote rides every non-paused Slack answer, so it also
    trails intermediate runbook-walkthrough steps — mildly naggy. Upgrade:
    have the agent flag a terminal answer and vote only on those.

    :raises Exception: whatever the channel client raises on a failed send.
    """
    cfg = config.get_config()
    match origin:
        case entities.SlackThread(channel_id=channel_id, thread_ts=thread_ts):
            if vote:
                await cfg.slack.post_answer(
                    channel=channel_id,
                    text=text,
                    thread_ts=thread_ts,
                    # Echoed back on click so the handler can split it into
                    # the answered thread's channel and ts.
                    feedback_value=f"{channel_id}:{thread_ts}",
                )
            else:
                await cfg.slack.post_message(channel=channel_id, text=text, thread_ts=thread_ts)
        case entities.TicketRef(issue_key=issue_key):
            if cfg.jira is None:
                # Unreachable while the webhook is the only ticket intake —
                # it rejects events whenever the gateway is unconfigured.
                logs.log_event("ticket_reply_skipped", params={"issue_key": issue_key})
            else:
                await cfg.jira.post_comment(issue_key=issue_key, text=text)


async def _attempt(message: outbox.OutboundMessage, *, store: outbox.PostgresOutbox) -> None:
    """
    Try one delivery of a recorded reply and record how it went. Never
    raises on a failed send — the outbox owns the retry.
    """
    try:
        await send(origin=message.origin, text=message.text, vote=message.vote)
    except Exception as exc:
        retry_in = outbox.retry_delay(attempts=message.attempts + 1)
        logs.log_exception(
            exc, params={"job": "reply_delivery", "key": message.key, "dead": retry_in is None}
        )
        await store.record_failure(message.id, error=type(exc).__name__, retry_in=retry_in)
        if retry_in is None:
            await _report_dead(message)
        return
    await store.mark_sent(message.id)


async def _report_dead(message: outbox.OutboundMessage) -> None:
    """
    Tell the triage channel, once, that a reply could not be delivered —
    a human has to pick the conversation up. Best-effort: if Slack itself is
    what is down, the log line above is all there is.
    """
    cfg = config.get_config()
    try:
        await cfg.slack.post_message(
            channel=cfg.settings.slack_triage_channel,
            text=(
                ":warning: I could not deliver a reply at "
                f"{entities.origin_ref(message.origin)} after {message.attempts + 1} "
                "attempts and have stopped trying — someone needs to follow up there."
            ),
        )
    except Exception as exc:
        logs.log_exception(exc, params={"job": "reply_dead_notice", "key": message.key})
