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


def build_audit_report(entries: Sequence[AuditEntry]) -> AuditReport:
    """
    Aggregate audit entries into a report: status counts, authorized-write
    count, and decision-latency (turnaround) statistics.
    """
    by_status: dict[str, int] = {}
    for entry in entries:
        by_status[entry.status] = by_status.get(entry.status, 0) + 1
    latencies = [e.latency_seconds for e in entries if e.latency_seconds is not None]
    return AuditReport(
        total=len(entries),
        by_status=by_status,
        writes_authorized=by_status.get(_APPROVED, 0),
        median_latency_seconds=statistics.median(latencies) if latencies else None,
        max_latency_seconds=max(latencies) if latencies else None,
        entries=tuple(entries),
    )


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
        lines.append(f"Max decision turnaround:     {report.max_latency_seconds / 60:.1f} min")
    else:
        lines.append("Decision turnaround:         n/a (no resolved approvals)")
    lines.append("")
    lines.append("Staged prototype figures prove the mechanism, not load.")
    return "\n".join(lines)
