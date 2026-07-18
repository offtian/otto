"""
Phase 2 exit demonstration (2.7): stage 50 access requests through the real
agent + approval flow against live Postgres and the mock SailPoint MCP, then
prove **zero unapproved writes** — the mock executed a write for every approved
request and for none of the denied or expired ones — and generate the audit
report from the durable store.

This is the Phase 2 exit bar: "50 requests, zero unapproved writes, report from
the store." 50 staged requests prove the mechanism, not load.

Live: needs Postgres + the mock (`docker compose --profile sailpoint up`, or
`uv run python dev/sailpoint_mock.py`). Guarded by RUN_INTEGRATION; run via
`just test-integration`.
"""

import json
import os
from datetime import UTC, datetime

import databases
import pytest
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from otto import config
from otto.application import audit as audit_app
from otto.application import support
from otto.data import _dsn
from otto.domain.identity import users
from otto.domain.support import approvals, entities, policy
from otto.settings import Settings
from otto.vendors import mcp as mcp_vendor
from otto.vendors import slack as slack_vendor


pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("RUN_INTEGRATION"),
        reason="needs Postgres + the mock SailPoint MCP — run via `just test-integration`",
    ),
    # SDK transport emits a DeprecationWarning on connect — not ours to fix.
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

DB_URL = _dsn.to_libpq(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/otto")
)
SAILPOINT_URL = os.environ.get("SAILPOINT_MCP_URL", "http://localhost:9100/mcp")

APPROVE, DENY, EXPIRE = 40, 7, 3  # 50 staged requests


def _tool_call() -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id="fc",
        call_id="call",
        type="function_call",
        name="submit_access_request",
        arguments=json.dumps(
            {"system": "snowflake", "entitlement": "reporting-read", "justification": "audit"}
        ),
    )


def _text(message: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="m",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=message, annotations=[])],
    )


class _ScriptedModel(Model):
    def __init__(self, turns):
        self.turns = list(turns)

    async def get_response(self, *args, **kwargs):
        return ModelResponse(output=self.turns.pop(0), usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class _FakeSlack:
    async def post_message(self, **kwargs):
        return "1.0"

    async def post_answer(self, **kwargs):
        return "1.1"

    async def post_escalation(self, **kwargs):
        return "3.0"

    async def update_message(self, **kwargs):
        return None

    async def set_status(self, **kwargs):
        return None

    async def set_suggested_prompts(self, **kwargs):
        return None

    async def post_approval_card(self, **kwargs):
        return "2.0"

    async def fetch_thread(self, **kwargs):
        return []


async def _mock_write_count(mock) -> int:
    # The mock records every executed submit; count them robustly across
    # FastMCP's list serialization (one array block, or one block per item).
    result = await mock.call_tool("list_access_requests", {})
    texts = [json.loads(block.text) for block in result.content if getattr(block, "text", None)]
    if len(texts) == 1 and isinstance(texts[0], list):
        return len(texts[0])
    return len([t for t in texts if isinstance(t, dict)])


@pytest.fixture
async def wired(monkeypatch):
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM approvals")
    mount = mcp_vendor.build_mount(
        spec=mcp_vendor.MCPSpec(
            name="sailpoint",
            url=SAILPOINT_URL,
            token="",
            ungated_tools=policy.SENSITIVITY_POLICY.ungated,
        )
    )
    mock = mount.server
    await mock.connect()
    total = APPROVE + DENY + EXPIRE
    # total pauses, then one resume turn per resolved (approve+deny) request.
    model = _ScriptedModel([[_tool_call()]] * total + [[_text("done")]] * (APPROVE + DENY))
    gateway = _FakeSlack()
    cfg = config.Configuration(
        settings=Settings(
            _env_file=None,
            slack_triage_channel="C_TRIAGE",
            support_user_ids="U_SUP",
            admin_user_ids="",
        ),
        slack=gateway,
        triage=slack_vendor.SlackTriageBackend(gateway=gateway, triage_channel="C_TRIAGE"),
        jira=None,
        directory=users.UserDirectory(users=[]),
        approvals=approvals.PostgresApprovalStore(database=db),
        model=model,
        confluence_mcp=None,
        sailpoint_mcp=mount,
    )
    await cfg.load_agents()  # mock already connected — wires the gated tools
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    try:
        yield cfg, mock
    finally:
        await mock.cleanup()
        await db.execute("DELETE FROM approvals")
        await db.disconnect()


class TestPhase2Exit:
    async def test_fifty_requests_yield_zero_unapproved_writes_and_an_audit_report(self, wired):
        # Given a fresh durable store and a mock write-counter snapshot
        cfg, mock = wired
        writes_before = await _mock_write_count(mock)

        # When 50 access requests are staged — each pauses on the gated write
        for index in range(APPROVE + DENY + EXPIRE):
            await support.handle_support_request(
                request=entities.SupportRequest(
                    id=f"ev-{index}",
                    user_id="U_REQ",
                    text="I need Snowflake reporting access",
                    origin=entities.SlackThread(channel_id="D", thread_ts=f"{index}.0"),
                )
            )
        pending_ids = [entry.approval_id for entry in await cfg.approvals.list_audit_entries()]
        assert len(pending_ids) == 50, "every request should pause for approval"

        # When they are resolved: 40 approved, 7 denied, 3 left to expire
        for approval_id in pending_ids[:APPROVE]:
            await support.resolve_approval(
                approval_id=approval_id, resolver_id="U_SUP", approved=True
            )
        for approval_id in pending_ids[APPROVE : APPROVE + DENY]:
            await support.resolve_approval(
                approval_id=approval_id, resolver_id="U_SUP", approved=False
            )
        await support.sweep_approvals(now=datetime(2999, 1, 1, tzinfo=UTC))

        # Then the mock executed a write for exactly the approved requests —
        # denied and expired ones ran nothing (zero unapproved writes)
        writes_executed = await _mock_write_count(mock) - writes_before
        assert writes_executed == APPROVE

        # Then the audit report from the durable store tells the whole story
        report = await audit_app.generate_audit_report()
        assert report.total == 50
        assert report.by_status == {"approved": APPROVE, "denied": DENY, "expired": EXPIRE}
        assert report.writes_authorized == APPROVE
        assert report.writes_authorized == writes_executed  # store agrees with the mock
        assert report.median_latency_seconds is not None
