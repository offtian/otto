"""
CLI: print the approval audit report (2.7) from the durable store.

    just audit-report        # needs DATABASE_URL configured (the durable store)
"""

import asyncio
import sys

from otto.application import audit as audit_app
from otto.data import db as data_db
from otto.domain.support import audit as audit_domain
from otto.utils import logs


async def _run() -> None:
    await data_db.connect_db()
    try:
        report = await audit_app.generate_audit_report()
    finally:
        await data_db.disconnect_db()
    logs.log_event(
        "audit_report_generated",
        params={"total": report.total, "writes_authorized": report.writes_authorized},
    )
    sys.stdout.write(audit_domain.render_report(report) + "\n")


def main() -> None:
    """
    Connect to the durable store, print the audit report, disconnect.
    """
    asyncio.run(_run())


if __name__ == "__main__":
    main()
