from datetime import UTC, datetime

import attrs
import pytest

from otto.domain.support import approvals, audit, entities


SLACK_ORIGIN = entities.SlackThread(channel_id="C1", thread_ts="1.0")
FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)
DISTANT_PAST = datetime(2000, 1, 1, tzinfo=UTC)


def _pending(origin: entities.Origin) -> approvals.PendingApproval:
    return approvals.PendingApproval(
        id="ap-1",
        request_id="req-1",
        requester_id="U_REQ",
        origin=origin,
        request_text="I need access",
        tool_name="request_access",
        tool_arguments='{"system": "snowflake"}',
        run_state_json="{}",
    )


class TestInMemoryApprovalStore:
    @pytest.mark.parametrize(
        "origin",
        [
            entities.SlackThread(channel_id="C1", thread_ts="1.0"),
            entities.TicketRef(issue_key="IT-42"),
        ],
    )
    async def test_round_trips_an_approval_for_both_origin_types(self, origin):
        # Given an empty store and a pending approval with this origin
        store = approvals.InMemoryApprovalStore()
        pending = _pending(origin)

        # When the approval is saved and fetched back
        await store.save(pending)
        fetched = await store.get("ap-1")

        # Then the fetched approval carries the same origin unchanged
        assert fetched == pending
        assert fetched.origin == origin

    async def test_resolve_transitions_a_pending_approval_and_records_the_resolver(self):
        # Given a stored pending approval
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(entities.SlackThread(channel_id="C1", thread_ts="1.0")))

        # When it is resolved as approved by a named resolver
        resolved = await store.resolve(
            "ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT"
        )

        # Then the returned and stored approval are terminal and carry the audit trail
        assert resolved.status is approvals.ApprovalStatus.APPROVED
        assert resolved.resolver_id == "U_SUPPORT"
        assert resolved.resolved_at is not None
        assert (await store.get("ap-1")).status is approvals.ApprovalStatus.APPROVED

    async def test_resolve_raises_when_already_resolved(self):
        # Given an approval that was already denied
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(entities.SlackThread(channel_id="C1", thread_ts="1.0")))
        await store.resolve("ap-1", approvals.ApprovalStatus.DENIED, resolver_id="U_ADMIN")

        # When a second resolution is attempted
        # Then the exactly-once guard fires
        with pytest.raises(approvals.ApprovalAlreadyResolved):
            await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_OTHER")

    async def test_get_raises_for_an_unknown_id(self):
        # Given an empty store
        store = approvals.InMemoryApprovalStore()

        # When an unknown id is fetched
        # Then the store signals it explicitly
        with pytest.raises(approvals.ApprovalNotFound):
            await store.get("nope")


class TestInMemoryApprovalStoreFindPending:
    async def test_returns_a_pending_approval_matching_origin_tool_and_arguments(self):
        # Given a stored pending approval on a Slack origin
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))

        # When a matching origin, tool, and a differently-formatted but
        # equivalent argument string are looked up (B4: canonical comparison)
        found = await store.find_pending(
            origin=SLACK_ORIGIN,
            tool_name="request_access",
            tool_arguments='{ "system" : "snowflake" }',
        )

        # Then that pending approval is returned
        assert found is not None
        assert found.id == "ap-1"

    async def test_returns_none_for_a_different_tool(self):
        # Given a pending approval for one tool
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))

        # When a different tool on the same origin is looked up
        found = await store.find_pending(
            origin=SLACK_ORIGIN,
            tool_name="submit_access_request",
            tool_arguments='{"system": "snowflake"}',
        )

        # Then nothing matches — the tool differs
        assert found is None

    async def test_returns_none_for_different_arguments(self):
        # Given a pending approval for one argument set
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))

        # When the same tool on the same origin is looked up with different
        # arguments — a genuinely different second request (B4)
        found = await store.find_pending(
            origin=SLACK_ORIGIN,
            tool_name="request_access",
            tool_arguments='{"system": "workday"}',
        )

        # Then nothing matches — it must get its own approval card
        assert found is None

    async def test_returns_none_for_a_different_origin(self):
        # Given a pending approval on a Slack origin
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))

        # When the same tool on a different origin is looked up
        found = await store.find_pending(
            origin=entities.TicketRef(issue_key="IT-42"),
            tool_name="request_access",
            tool_arguments='{"system": "snowflake"}',
        )

        # Then nothing matches — the origin differs
        assert found is None

    async def test_ignores_an_already_resolved_approval(self):
        # Given an approval that has been resolved
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))
        await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

        # When its origin, tool, and arguments are looked up
        found = await store.find_pending(
            origin=SLACK_ORIGIN,
            tool_name="request_access",
            tool_arguments='{"system": "snowflake"}',
        )

        # Then it is not returned — only a still-pending run suppresses a duplicate
        assert found is None


class TestInMemoryApprovalStoreExecution:
    async def test_a_resolved_approval_is_unexecuted_until_marked(self):
        # Given an approval resolved but never marked executed (B2 crash window)
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))
        await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

        # When the unexecuted approvals are listed
        unexecuted = await store.list_unexecuted()

        # Then it is returned — decided but never carried out
        assert [a.id for a in unexecuted] == ["ap-1"]

    async def test_mark_executed_dispositions_the_approval(self):
        # Given a resolved approval
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))
        await store.resolve("ap-1", approvals.ApprovalStatus.DENIED, resolver_id="U_ADMIN")

        # When it is marked executed
        await store.mark_executed("ap-1")

        # Then it no longer needs recovery and carries the execution stamp
        assert await store.list_unexecuted() == []
        assert (await store.get("ap-1")).executed_at is not None

    async def test_pending_and_expired_approvals_never_need_recovery(self):
        # Given one just-saved pending approval and one the sweep expired
        store = approvals.InMemoryApprovalStore()
        await store.save(
            attrs.evolve(_pending(entities.TicketRef(issue_key="IT-42")), id="ap-expired")
        )
        await store.expire_pending(cutoff=FAR_FUTURE)
        await store.save(_pending(SLACK_ORIGIN))

        # When the unexecuted approvals are listed
        unexecuted = await store.list_unexecuted()

        # Then neither is returned — pending owes no execution yet, expired never will
        assert unexecuted == []


class TestInMemoryApprovalStoreSurfaceIsolation:
    async def test_another_surfaces_rows_are_left_alone_by_sweep_and_recovery(self):
        # Given one pending and one decided-but-unexecuted dev-chat approval
        store = approvals.InMemoryApprovalStore()
        await store.save(attrs.evolve(_pending(SLACK_ORIGIN), channel="streamlit"))
        await store.save(
            attrs.evolve(_pending(SLACK_ORIGIN), id="ap-chat-decided", channel="streamlit")
        )
        await store.resolve(
            "ap-chat-decided", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT"
        )

        # When the server-side sweep and recovery queries run (slack scope)
        expired = await store.expire_pending(cutoff=FAR_FUTURE)
        reminders = await store.claim_due_reminders(cutoff=FAR_FUTURE)
        unexecuted = await store.list_unexecuted()

        # Then the other surface's rows are untouched — the server must never
        # close chat cards or replay chat-approved tools
        assert expired == []
        assert reminders == []
        assert unexecuted == []
        assert (await store.get("ap-1")).status is approvals.ApprovalStatus.PENDING


class TestInMemoryApprovalStoreEvents:
    async def test_record_event_stamps_and_lists_in_order(self):
        # Given a store and two rejected-attempt events (B5)
        store = approvals.InMemoryApprovalStore()
        await store.record_event(
            audit.AuditEvent(event_type="unauthorized_role", actor_id="U_RANDO")
        )
        await store.record_event(
            audit.AuditEvent(event_type="self_approval", actor_id="U_SUPPORT", approval_id="ap-1")
        )

        # When the events are listed
        events = await store.list_events()

        # Then both are returned in order with occurred_at stamped
        assert [e.event_type for e in events] == ["unauthorized_role", "self_approval"]
        assert all(e.occurred_at is not None for e in events)


class TestInMemoryApprovalStoreSweep:
    async def test_expire_pending_transitions_stale_approvals_to_terminal(self):
        # Given a stored pending approval
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))

        # When the sweep expires everything created before a future cutoff
        expired = await store.expire_pending(cutoff=FAR_FUTURE)

        # Then it is returned and stored as EXPIRED, with a terminal timestamp
        assert [a.id for a in expired] == ["ap-1"]
        stored = await store.get("ap-1")
        assert stored.status is approvals.ApprovalStatus.EXPIRED
        assert stored.resolved_at is not None

    async def test_expire_pending_leaves_fresh_approvals_alone(self):
        # Given a just-created pending approval
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))

        # When the sweep only expires approvals older than a past cutoff
        expired = await store.expire_pending(cutoff=DISTANT_PAST)

        # Then nothing expires and it stays pending
        assert expired == []
        assert (await store.get("ap-1")).status is approvals.ApprovalStatus.PENDING

    async def test_expired_approval_rejects_a_late_click(self):
        # Given an approval the sweep has already expired
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))
        await store.expire_pending(cutoff=FAR_FUTURE)

        # When someone clicks approve after it expired
        # Then the resolve guard rejects it — expired is terminal
        with pytest.raises(approvals.ApprovalAlreadyResolved):
            await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

    async def test_claim_due_reminders_returns_a_pending_approval_once_per_interval(self):
        # Given a pending approval past its reminder interval
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))

        # When the sweep claims reminders due before a future cutoff
        first = await store.claim_due_reminders(cutoff=FAR_FUTURE)

        # Then it is due once...
        assert [a.id for a in first] == ["ap-1"]

        # ...and not again until another interval elapses (a past cutoff is
        # before the just-stamped reminder time)
        second = await store.claim_due_reminders(cutoff=DISTANT_PAST)
        assert second == []

    async def test_purge_resolved_state_nulls_run_state_after_retention(self):
        # Given a resolved approval whose run state is retained
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))
        await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

        # When the retention sweep runs with a cutoff after its resolution
        purged = await store.purge_resolved_state(cutoff=FAR_FUTURE)

        # Then the conversation-bearing run state is dropped (A9)
        assert purged == 1
        assert (await store.get("ap-1")).run_state_json == ""

    async def test_purge_resolved_state_spares_pending_and_recent_approvals(self):
        # Given one pending approval and one just-resolved approval
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))
        await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

        # When the retention cutoff predates the resolution
        purged = await store.purge_resolved_state(cutoff=DISTANT_PAST)

        # Then nothing is purged — the run state is still within retention
        assert purged == 0
        assert (await store.get("ap-1")).run_state_json == "{}"

    async def test_list_audit_entries_projects_saved_and_resolved_approvals(self):
        # Given a saved then resolved approval
        store = approvals.InMemoryApprovalStore()
        await store.save(_pending(SLACK_ORIGIN))
        await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

        # When the audit projection is read
        entries = await store.list_audit_entries()

        # Then it carries the audit facts — resolver and timestamps included
        assert len(entries) == 1
        entry = entries[0]
        assert entry.approval_id == "ap-1"
        assert entry.status == "approved"
        assert entry.resolver_id == "U_SUPPORT"
        assert entry.resolved_at is not None
        assert entry.latency_seconds is not None
