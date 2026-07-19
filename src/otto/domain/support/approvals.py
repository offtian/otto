"""
Human-in-the-loop approval domain: entities, states, and the store contract.

When the agent calls a sensitive tool, the run pauses and a
``PendingApproval`` captures everything needed to resume it after a human
decision — including the serialized Agents SDK run state.
"""

import enum
import json
from datetime import UTC, datetime
from typing import Any, Protocol

import attrs
import databases
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from otto.data import models
from otto.domain.support import audit, entities


def canonical_arguments(raw: str) -> str:
    """
    Return a key-order/whitespace-insensitive form of a tool-arguments string
    for duplicate comparison (B4) — trivially different serializations of the
    same call compare equal; genuinely different arguments do not.
    """
    try:
        return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        return raw.strip()


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    # Terminal, set by the maintenance sweep (2.5) when nobody decided in time.
    # An expired approval is no longer pending, so the resolve() guard rejects
    # a late click exactly as it does an already-decided one.
    EXPIRED = "expired"


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
    # Stamped once the decided run's outcome was delivered (B2). None on a
    # terminal approved/denied row = decided but never executed.
    executed_at: datetime | None = None


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

    async def find_pending(
        self, *, origin: entities.Origin, tool_name: str, tool_arguments: str
    ) -> PendingApproval | None:
        """
        Return a still-pending approval for this origin, tool, and arguments
        (compared canonically, B4), or None. Used to suppress a duplicate card
        when a re-triggered event on the same conversation asks for the same
        gated action again while its earlier run is already awaiting a human
        OK — a *different* request on the same tool is not a duplicate.
        """
        ...

    async def mark_executed(self, approval_id: str) -> None:
        """
        Stamp the approval as executed — its decided run completed and the
        outcome was delivered (B2).
        """
        ...

    async def list_unexecuted(self) -> list[PendingApproval]:
        """
        Return approved/denied approvals whose run state is still present but
        whose execution was never stamped (B2) — decided in a process that
        died before the resumed run completed. Expired approvals owe no
        execution and are excluded.
        """
        ...

    async def record_event(self, event: audit.AuditEvent) -> None:
        """
        Append a rejected/system HITL event to the durable audit trail (B5),
        stamping ``occurred_at`` if unset.
        """
        ...

    async def list_events(self) -> list[audit.AuditEvent]:
        """
        Return every recorded audit event, oldest first (B5).
        """
        ...

    async def expire_pending(self, *, cutoff: datetime) -> list[PendingApproval]:
        """
        Transition every approval still pending since before ``cutoff`` to
        ``EXPIRED`` and return the ones just expired (so their cards can be
        closed out). Guarded on ``status = pending``, so it never races a
        concurrent resolve — whichever lands first wins, the other is rejected.
        """
        ...

    async def claim_due_reminders(self, *, cutoff: datetime) -> list[PendingApproval]:
        """
        Return the pending approvals last nudged (or, if never, created)
        before ``cutoff``, stamping their reminder time to now so the next
        sweep waits another interval. Each due approval is claimed once.
        """
        ...

    async def purge_resolved_state(self, *, cutoff: datetime) -> int:
        """
        Null out ``run_state_json`` on terminal approvals that reached their
        terminal state before ``cutoff`` (A9 — the conversation-bearing run
        state is PII at rest once its run can no longer resume). Returns the
        number of approvals purged.
        """
        ...

    async def list_audit_entries(self) -> list[audit.AuditEntry]:
        """
        Return an audit projection of every approval (2.7), oldest first — the
        system-of-record read the audit report is built from. Excludes the
        conversation-bearing run state.
        """
        ...


class InMemoryApprovalStore:
    """
    Dev/MVP store. Approvals do not survive a process restart — swap the
    wiring in ``config.py`` for a durable store before production.
    """

    def __init__(self) -> None:
        self._approvals: dict[str, PendingApproval] = {}
        # Sweep bookkeeping the entity does not carry (created_at/reminded_at
        # are DB-managed columns in the durable store).
        self._created_at: dict[str, datetime] = {}
        self._reminded_at: dict[str, datetime] = {}
        self._events: list[audit.AuditEvent] = []

    async def save(self, approval: PendingApproval) -> None:
        self._approvals[approval.id] = approval
        # First save wins the creation time; a re-save (insert-or-replace)
        # never resets it, mirroring the Postgres on_conflict behaviour.
        self._created_at.setdefault(approval.id, datetime.now(tz=UTC))

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

    async def find_pending(
        self, *, origin: entities.Origin, tool_name: str, tool_arguments: str
    ) -> PendingApproval | None:
        wanted = canonical_arguments(tool_arguments)
        for approval in self._approvals.values():
            if (
                approval.status is ApprovalStatus.PENDING
                and approval.origin == origin
                and approval.tool_name == tool_name
                and canonical_arguments(approval.tool_arguments) == wanted
            ):
                return approval
        return None

    async def mark_executed(self, approval_id: str) -> None:
        approval = await self.get(approval_id)
        self._approvals[approval_id] = attrs.evolve(approval, executed_at=datetime.now(tz=UTC))

    async def list_unexecuted(self) -> list[PendingApproval]:
        return [
            approval
            for approval in self._approvals.values()
            if approval.status in (ApprovalStatus.APPROVED, ApprovalStatus.DENIED)
            and approval.executed_at is None
            and approval.run_state_json
        ]

    async def record_event(self, event: audit.AuditEvent) -> None:
        if event.occurred_at is None:
            event = attrs.evolve(event, occurred_at=datetime.now(tz=UTC))
        self._events.append(event)

    async def list_events(self) -> list[audit.AuditEvent]:
        return list(self._events)

    async def expire_pending(self, *, cutoff: datetime) -> list[PendingApproval]:
        now = datetime.now(tz=UTC)
        expired: list[PendingApproval] = []
        for approval_id, approval in list(self._approvals.items()):
            if approval.status is not ApprovalStatus.PENDING:
                continue
            if self._created_at.get(approval_id, now) >= cutoff:
                continue
            evolved = attrs.evolve(approval, status=ApprovalStatus.EXPIRED, resolved_at=now)
            self._approvals[approval_id] = evolved
            expired.append(evolved)
        return expired

    async def claim_due_reminders(self, *, cutoff: datetime) -> list[PendingApproval]:
        now = datetime.now(tz=UTC)
        due: list[PendingApproval] = []
        for approval_id, approval in self._approvals.items():
            if approval.status is not ApprovalStatus.PENDING:
                continue
            last = self._reminded_at.get(approval_id) or self._created_at.get(approval_id, now)
            if last >= cutoff:
                continue
            self._reminded_at[approval_id] = now
            due.append(approval)
        return due

    async def purge_resolved_state(self, *, cutoff: datetime) -> int:
        purged = 0
        for approval_id, approval in list(self._approvals.items()):
            if approval.status is ApprovalStatus.PENDING:
                continue
            if approval.resolved_at is None or approval.resolved_at >= cutoff:
                continue
            if not approval.run_state_json:
                continue
            self._approvals[approval_id] = attrs.evolve(approval, run_state_json="")
            purged += 1
        return purged

    async def list_audit_entries(self) -> list[audit.AuditEntry]:
        entries = [
            audit.AuditEntry(
                approval_id=approval.id,
                requester_id=approval.requester_id,
                tool_name=approval.tool_name,
                status=approval.status.value,
                resolver_id=approval.resolver_id,
                created_at=self._created_at[approval_id],
                resolved_at=approval.resolved_at,
            )
            for approval_id, approval in self._approvals.items()
        ]
        return sorted(entries, key=lambda entry: entry.created_at)


# SQLModel exposes the SQLAlchemy Table at runtime; mypy needs the hint.
_TABLE: sa.Table = models.ApprovalRecord.__table__  # type: ignore[attr-defined]
_EVENTS: sa.Table = models.AuditEventRecord.__table__  # type: ignore[attr-defined]


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

    async def find_pending(
        self, *, origin: entities.Origin, tool_name: str, tool_arguments: str
    ) -> PendingApproval | None:
        table = _TABLE
        # origin_to_json emits fixed key order, so equality on the stored TEXT
        # is exact for both origin kinds. Arguments compare canonically in
        # Python (B4) — pending rows per origin+tool are at most a handful.
        rows = await self.database.fetch_all(
            sa.select(table).where(
                sa.and_(
                    table.c.status == ApprovalStatus.PENDING.value,
                    table.c.origin == entities.origin_to_json(origin),
                    table.c.tool_name == tool_name,
                )
            )
        )
        wanted = canonical_arguments(tool_arguments)
        for row in rows:
            approval = _from_row(row)
            if canonical_arguments(approval.tool_arguments) == wanted:
                return approval
        return None

    async def mark_executed(self, approval_id: str) -> None:
        table = _TABLE
        await self.database.execute(
            sa.update(table).where(table.c.id == approval_id).values(executed_at=sa.func.now())
        )

    async def list_unexecuted(self) -> list[PendingApproval]:
        table = _TABLE
        rows = await self.database.fetch_all(
            sa.select(table).where(
                sa.and_(
                    table.c.status.in_(
                        [ApprovalStatus.APPROVED.value, ApprovalStatus.DENIED.value]
                    ),
                    table.c.executed_at.is_(None),
                    table.c.run_state_json.is_not(None),
                )
            )
        )
        return [_from_row(row) for row in rows]

    async def record_event(self, event: audit.AuditEvent) -> None:
        await self.database.execute(
            sa.insert(_EVENTS).values(
                event_type=event.event_type,
                actor_id=event.actor_id,
                approval_id=event.approval_id,
                detail=event.detail,
                occurred_at=event.occurred_at if event.occurred_at is not None else sa.func.now(),
            )
        )

    async def list_events(self) -> list[audit.AuditEvent]:
        rows = await self.database.fetch_all(sa.select(_EVENTS).order_by(_EVENTS.c.occurred_at))
        return [
            audit.AuditEvent(
                event_type=row["event_type"],
                actor_id=row["actor_id"],
                approval_id=row["approval_id"],
                detail=row["detail"],
                occurred_at=row["occurred_at"],
            )
            for row in rows
        ]

    async def expire_pending(self, *, cutoff: datetime) -> list[PendingApproval]:
        table = _TABLE
        update = (
            sa.update(table)
            .where(
                sa.and_(
                    table.c.status == ApprovalStatus.PENDING.value,
                    table.c.created_at < cutoff,
                )
            )
            .values(status=ApprovalStatus.EXPIRED.value, resolved_at=sa.func.now())
            .returning(*table.c)
        )
        rows = await self.database.fetch_all(update)
        return [_from_row(row) for row in rows]

    async def claim_due_reminders(self, *, cutoff: datetime) -> list[PendingApproval]:
        table = _TABLE
        update = (
            sa.update(table)
            .where(
                sa.and_(
                    table.c.status == ApprovalStatus.PENDING.value,
                    sa.func.coalesce(table.c.reminded_at, table.c.created_at) < cutoff,
                )
            )
            .values(reminded_at=sa.func.now())
            .returning(*table.c)
        )
        rows = await self.database.fetch_all(update)
        return [_from_row(row) for row in rows]

    async def purge_resolved_state(self, *, cutoff: datetime) -> int:
        table = _TABLE
        update = (
            sa.update(table)
            .where(
                sa.and_(
                    table.c.status != ApprovalStatus.PENDING.value,
                    table.c.resolved_at.is_not(None),
                    table.c.resolved_at < cutoff,
                    table.c.run_state_json.is_not(None),
                )
            )
            .values(run_state_json=None)
            .returning(table.c.id)
        )
        rows = await self.database.fetch_all(update)
        return len(rows)

    async def list_audit_entries(self) -> list[audit.AuditEntry]:
        table = _TABLE
        rows = await self.database.fetch_all(
            sa.select(
                table.c.id,
                table.c.requester_id,
                table.c.tool_name,
                table.c.status,
                table.c.resolver_id,
                table.c.created_at,
                table.c.resolved_at,
            ).order_by(table.c.created_at)
        )
        return [
            audit.AuditEntry(
                approval_id=row["id"],
                requester_id=row["requester_id"],
                tool_name=row["tool_name"],
                status=row["status"],
                resolver_id=row["resolver_id"],
                created_at=row["created_at"],
                resolved_at=row["resolved_at"],
            )
            for row in rows
        ]


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
        "executed_at": approval.executed_at,
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
        executed_at=data["executed_at"],
    )
