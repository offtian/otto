"""
Human-in-the-loop approval domain: entities, states, and the store contract.

When the agent calls a sensitive tool, the run pauses and a
``PendingApproval`` captures everything needed to resume it after a human
decision — including the serialized Agents SDK run state.
"""

import enum
from datetime import UTC, datetime
from typing import Protocol

import attrs

from otto.domain.support import entities


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalNotFound(Exception):
    """
    Raised when an approval id does not exist in the store.
    """


class ApprovalAlreadyResolved(Exception):
    """
    Raised when resolving an approval that is no longer pending
    (e.g. two approvers clicked concurrently).
    """


@attrs.frozen
class PendingApproval:
    """
    A paused agent run awaiting a human decision.
    """

    id: str
    request_id: str
    requester_id: str
    origin: entities.Origin
    request_text: str
    tool_name: str
    tool_arguments: str
    run_state_json: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    card_channel: str = ""
    card_ts: str = ""
    # Audit trail, set on resolve (empty/None while pending). The durable store
    # persists these to ApprovalRecord; the in-memory store carries them too so
    # the returned entity is complete.
    resolver_id: str = ""
    resolved_at: datetime | None = None


class ApprovalStore(Protocol):
    """
    Persistence contract for pending approvals. The MVP wires
    ``InMemoryApprovalStore``; a Postgres-backed implementation over
    ``data.models.ApprovalRecord`` is the production slot.
    """

    async def save(self, approval: PendingApproval) -> None:
        """Insert or replace the approval."""
        ...

    async def get(self, approval_id: str) -> PendingApproval:
        """
        Return the approval.

        :raises ApprovalNotFound: if the id is unknown.
        """
        ...

    async def resolve(
        self, approval_id: str, status: ApprovalStatus, *, resolver_id: str
    ) -> PendingApproval:
        """
        Transition a pending approval to a terminal status, record who
        resolved it (audit), and return it.

        :raises ApprovalNotFound: if the id is unknown.
        :raises ApprovalAlreadyResolved: if it is not pending.
        """
        ...


class InMemoryApprovalStore:
    """
    Dev/MVP store. Approvals do not survive a process restart — swap the
    wiring in ``config.py`` for a durable store before production.
    """

    def __init__(self) -> None:
        self._approvals: dict[str, PendingApproval] = {}

    async def save(self, approval: PendingApproval) -> None:
        self._approvals[approval.id] = approval

    async def get(self, approval_id: str) -> PendingApproval:
        try:
            return self._approvals[approval_id]
        except KeyError:
            raise ApprovalNotFound(f"no approval with id {approval_id!r}") from None

    async def resolve(
        self, approval_id: str, status: ApprovalStatus, *, resolver_id: str
    ) -> PendingApproval:
        approval = await self.get(approval_id)
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalAlreadyResolved(
                f"approval {approval_id!r} is already {approval.status.value}"
            )
        # ponytail: dev store stamps its own resolved_at; the durable store
        # will use the DB server clock so the audit time is authoritative.
        resolved = attrs.evolve(
            approval,
            status=status,
            resolver_id=resolver_id,
            resolved_at=datetime.now(tz=UTC),
        )
        self._approvals[approval_id] = resolved
        return resolved
