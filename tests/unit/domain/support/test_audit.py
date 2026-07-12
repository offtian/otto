from datetime import UTC, datetime

from otto.domain.support import audit


CREATED = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)


def _entry(status: str, resolved: datetime | None) -> audit.AuditEntry:
    return audit.AuditEntry(
        approval_id=f"ap-{status}-{resolved}",
        requester_id="U_REQ",
        tool_name="submit_access_request",
        status=status,
        resolver_id="U_SUP" if resolved else "",
        created_at=CREATED,
        resolved_at=resolved,
    )


class TestAuditEntry:
    def test_latency_is_the_seconds_between_request_and_decision(self):
        # Given an approval resolved two minutes after it was created
        entry = _entry("approved", datetime(2026, 7, 12, 10, 2, tzinfo=UTC))

        # When its latency is read
        # Then it is the elapsed seconds
        assert entry.latency_seconds == 120.0

    def test_latency_is_none_while_unresolved(self):
        # Given a still-pending approval
        entry = _entry("pending", None)

        # When its latency is read
        # Then there is none yet
        assert entry.latency_seconds is None


class TestBuildAuditReport:
    def test_counts_by_status_and_authorized_writes(self):
        # Given approvals across statuses
        entries = [
            _entry("approved", datetime(2026, 7, 12, 10, 1, tzinfo=UTC)),
            _entry("approved", datetime(2026, 7, 12, 10, 3, tzinfo=UTC)),
            _entry("denied", datetime(2026, 7, 12, 10, 5, tzinfo=UTC)),
            _entry("expired", datetime(2026, 7, 12, 10, 9, tzinfo=UTC)),
        ]

        # When the report is built
        report = audit.build_audit_report(entries)

        # Then totals, per-status counts, and the authorized-write count are right
        assert report.total == 4
        assert report.by_status == {"approved": 2, "denied": 1, "expired": 1}
        assert report.writes_authorized == 2

    def test_median_and_max_turnaround_over_resolved_entries(self):
        # Given resolved approvals with 60 / 180 / 300 second latencies
        entries = [
            _entry("approved", datetime(2026, 7, 12, 10, 1, tzinfo=UTC)),
            _entry("denied", datetime(2026, 7, 12, 10, 3, tzinfo=UTC)),
            _entry("approved", datetime(2026, 7, 12, 10, 5, tzinfo=UTC)),
        ]

        # When the report is built
        report = audit.build_audit_report(entries)

        # Then the median and max decision turnarounds are computed
        assert report.median_latency_seconds == 180.0
        assert report.max_latency_seconds == 300.0

    def test_empty_store_reports_no_turnaround(self):
        # Given no approvals
        report = audit.build_audit_report([])

        # Then the report is empty and latency stats are absent
        assert report.total == 0
        assert report.writes_authorized == 0
        assert report.median_latency_seconds is None

    def test_render_includes_the_headline_numbers(self):
        # Given a report with one approved approval resolved after two minutes
        report = audit.build_audit_report(
            [_entry("approved", datetime(2026, 7, 12, 10, 2, tzinfo=UTC))]
        )

        # When it is rendered for the ops console
        text = audit.render_report(report)

        # Then the operator sees the totals and the turnaround
        assert "Total approvals:            1" in text
        assert "Writes authorized" in text
        assert "2.0 min" in text
