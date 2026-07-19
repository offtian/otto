"""
Approval audit trail (2.7): a read-only projection of the durable
``ApprovalRecord`` store into an auditor-facing report — who approved what,
when, and how fast (decision latency / turnaround), plus the status breakdown.

Kept free of ``approvals`` (it takes plain values) so the store can return
``AuditEntry`` without an import cycle. The zero-unapproved-writes guarantee is
structural — a gated tool executes only on an approved record — so
``writes_authorized`` equals the approved count; the exit demonstration proves
the count empirically against the mock.
"""

import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime

import attrs


_APPROVED = "approved"


@attrs.frozen
class AuditEvent:
    """
    One rejected or system-driven HITL event (B5) — an attempt or expiry the
    approval rows alone cannot show: who tried and was turned away, and what
    the sweep timed out. ``occurred_at`` is stamped by the store on record.
    """

    event_type: str  # unauthorized_role | self_approval | unverified_identity | expired
    actor_id: str
    approval_id: str = ""
    detail: str = ""
    occurred_at: datetime | None = None


@attrs.frozen
class AuditEntry:
    """
    One approval as the audit trail sees it — the durable record minus the
    conversation-bearing run state.
    """

    approval_id: str
    requester_id: str
    tool_name: str
    status: str
    resolver_id: str
    created_at: datetime
    resolved_at: datetime | None

    @property
    def latency_seconds(self) -> float | None:
        """
        Return the seconds from request to human decision, or None while the
        approval is unresolved.
        """
        if self.resolved_at is None:
            return None
        return (self.resolved_at - self.created_at).total_seconds()


@attrs.frozen
class AuditReport:
    """
    Aggregate view over every approval: the Phase 2 audit-trail artefact.
    """

    total: int
    by_status: Mapping[str, int]
    writes_authorized: int
    median_latency_seconds: float | None
    max_latency_seconds: float | None
    entries: tuple[AuditEntry, ...]
    events: tuple[AuditEvent, ...] = ()
    # Per-approver decision counts and the latency tail (D2): the pilot's
    # answer to the approver-volume/diligence question — measured, not guessed.
    by_resolver: Mapping[str, int] = attrs.field(factory=dict)
    p90_latency_seconds: float | None = None


def build_audit_report(
    entries: Sequence[AuditEntry], *, events: Sequence[AuditEvent] = ()
) -> AuditReport:
    """
    Aggregate audit entries into a report: status counts, authorized-write
    count, decision-latency (turnaround) statistics, and the rejected/system
    events recorded alongside the decisions (B5).
    """
    by_status: dict[str, int] = {}
    by_resolver: dict[str, int] = {}
    for entry in entries:
        by_status[entry.status] = by_status.get(entry.status, 0) + 1
        if entry.resolver_id:
            by_resolver[entry.resolver_id] = by_resolver.get(entry.resolver_id, 0) + 1
    latencies = [e.latency_seconds for e in entries if e.latency_seconds is not None]
    return AuditReport(
        total=len(entries),
        by_status=by_status,
        writes_authorized=by_status.get(_APPROVED, 0),
        median_latency_seconds=statistics.median(latencies) if latencies else None,
        max_latency_seconds=max(latencies) if latencies else None,
        entries=tuple(entries),
        events=tuple(events),
        by_resolver=by_resolver,
        p90_latency_seconds=_p90(latencies),
    )


def _p90(latencies: Sequence[float]) -> float | None:
    """
    Return the 90th-percentile latency — the tail the median hides. With a
    single sample the sample is the tail.
    """
    if not latencies:
        return None
    if len(latencies) == 1:
        return latencies[0]
    return statistics.quantiles(latencies, n=10)[-1]


def render_report(report: AuditReport) -> str:
    """
    Return the audit report as a plain-text summary for the ops console.
    """
    lines = [
        "Approval audit report",
        "=====================",
        f"Total approvals:            {report.total}",
        f"Writes authorized (approved): {report.writes_authorized}",
        "By status:                  "
        + (
            ", ".join(f"{name}={count}" for name, count in sorted(report.by_status.items())) or "—"
        ),
    ]
    if report.median_latency_seconds is not None and report.max_latency_seconds is not None:
        lines.append(f"Median decision turnaround:  {report.median_latency_seconds / 60:.1f} min")
        if report.p90_latency_seconds is not None:
            lines.append(f"P90 decision turnaround:     {report.p90_latency_seconds / 60:.1f} min")
        lines.append(f"Max decision turnaround:     {report.max_latency_seconds / 60:.1f} min")
    else:
        lines.append("Decision turnaround:         n/a (no resolved approvals)")
    if report.by_resolver:
        lines.append(
            "Decisions per approver:     "
            + ", ".join(f"{name}={count}" for name, count in sorted(report.by_resolver.items()))
        )
    if report.events:
        by_type: dict[str, int] = {}
        for event in report.events:
            by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
        lines.append(
            "Rejected/system events:     "
            + ", ".join(f"{name}={count}" for name, count in sorted(by_type.items()))
        )
    lines.append("")
    lines.append("Staged prototype figures prove the mechanism, not load.")
    return "\n".join(lines)
