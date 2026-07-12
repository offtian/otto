"""
Human-in-the-loop approval domain: entities, states, and the store contract.

When the agent calls a sensitive tool, the run pauses and a
``PendingApproval`` captures everything needed to resume it after a human
decision — including the serialized Agents SDK run state.
"""

import enum
from datetime import UTC, datetime
from typing import Any, Protocol

import attrs
import databases
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from otto.data import models
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


# SQLModel exposes the SQLAlchemy Table at runtime; mypy needs the hint.
_TABLE: sa.Table = models.ApprovalRecord.__table__  # type: ignore[attr-defined]


@attrs.frozen
class PostgresApprovalStore:
    """
    Durable approval store over Postgres (2.2, the ApprovalStore protocol).

    ``resolve`` is a conditional UPDATE guarded on ``status = pending``, so two
    concurrent approvers can never both execute the run — the exactly-once,
    zero-double-write guarantee that FR4/FR6 rest on. The ``Database`` is
    injected by ``config`` so the domain never imports ``data.db`` (which reads
    settings); this store touches only ``data.models`` (the table).
    """

    database: databases.Database

    async def save(self, approval: PendingApproval) -> None:
        table = _TABLE
        row = _to_row(approval)
        insert = pg.insert(table).values(**row, created_at=sa.func.now())
        # Insert-or-replace per the protocol, but never reset created_at.
        insert = insert.on_conflict_do_update(
            index_elements=[table.c.id],
            set_={column: row[column] for column in row if column != "id"},
        )
        await self.database.execute(insert)

    async def get(self, approval_id: str) -> PendingApproval:
        table = _TABLE
        row = await self.database.fetch_one(sa.select(table).where(table.c.id == approval_id))
        if row is None:
            raise ApprovalNotFound(f"no approval with id {approval_id!r}")
        return _from_row(row)

    async def resolve(
        self, approval_id: str, status: ApprovalStatus, *, resolver_id: str
    ) -> PendingApproval:
        table = _TABLE
        update = (
            sa.update(table)
            .where(
                sa.and_(
                    table.c.id == approval_id,
                    table.c.status == ApprovalStatus.PENDING.value,
                )
            )
            .values(status=status.value, resolver_id=resolver_id, resolved_at=sa.func.now())
            .returning(*table.c)
        )
        updated = await self.database.fetch_one(update)
        if updated is not None:
            return _from_row(updated)
        # Nothing updated: the guard held. Distinguish unknown from already-resolved.
        exists = await self.database.fetch_one(
            sa.select(table.c.id).where(table.c.id == approval_id)
        )
        if exists is None:
            raise ApprovalNotFound(f"no approval with id {approval_id!r}")
        raise ApprovalAlreadyResolved(f"approval {approval_id!r} is already resolved")


def _to_row(approval: PendingApproval) -> dict[str, object]:
    return {
        "id": approval.id,
        "request_id": approval.request_id,
        "requester_id": approval.requester_id,
        "origin": entities.origin_to_json(approval.origin),
        "request_text": approval.request_text,
        "tool_name": approval.tool_name,
        "tool_arguments": approval.tool_arguments,
        "run_state_json": approval.run_state_json,
        "status": approval.status.value,
        "card_channel": approval.card_channel,
        "card_ts": approval.card_ts,
        "resolver_id": approval.resolver_id,
        "resolved_at": approval.resolved_at,
    }


def _from_row(row: Any) -> PendingApproval:
    # ``row`` is a databases Record (mapping access by column name).
    data: dict[str, Any] = dict(row)
    return PendingApproval(
        id=data["id"],
        request_id=data["request_id"],
        requester_id=data["requester_id"],
        origin=entities.origin_from_json(data["origin"]),
        request_text=data["request_text"],
        tool_name=data["tool_name"],
        tool_arguments=data["tool_arguments"],
        run_state_json=data["run_state_json"] or "",
        status=ApprovalStatus(data["status"]),
        card_channel=data["card_channel"],
        card_ts=data["card_ts"],
        resolver_id=data["resolver_id"],
        resolved_at=data["resolved_at"],
    )
