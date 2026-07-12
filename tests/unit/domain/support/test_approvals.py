import pytest

from otto.domain.support import approvals, entities


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
