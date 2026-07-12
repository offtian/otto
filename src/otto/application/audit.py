"""
Audit-report use-case (2.7): read the durable approval store and build the
Phase 2 audit trail — who approved what, when, and how fast (turnaround).
"""

from otto import config
from otto.domain.support import audit


async def generate_audit_report() -> audit.AuditReport:
    """
    Return the approval audit report built from the durable store — the system
    of record for every gated-tool decision.
    """
    cfg = config.get_config()
    entries = await cfg.approvals.list_audit_entries()
    return audit.build_audit_report(entries)
