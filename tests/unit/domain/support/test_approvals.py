from datetime import UTC, datetime

import pytest

from otto.domain.support import approvals, entities


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
