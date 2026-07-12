"""
Golden-case smoke evals (NFR3): 3 cases against the *configured* LLM
(LiteLLM stand-in or real gateway). Live and non-deterministic, so they run
only via `just eval` (RUN_EVALS=1) — never in the normal test suite.

At n=3 this is a smoke test, not a gate; the ≥90%-at-n≥20 gate and its
enforcement point are Phase 1 work (D5/T5). Plain pytest asserts stand in
for the LLM judge until the case count justifies one.
"""

import os
import pathlib

import agents
import pytest

from otto import config
from otto.domain.support import agent as support_agent


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_EVALS"),
    reason="live LLM eval — run via `just eval`",
)


class RecordingTicketBackend:
    def __init__(self):
        self.escalations = []

    async def escalate(self, *, subject, summary, urgency, requester_id, origin_ref):
        self.escalations.append(subject)
        return "triage#eval"


async def _run_otto(text: str):
    cfg = config.get_config()
    backend = RecordingTicketBackend()
    agent = support_agent.build_agent(model=cfg.model)
    result = await agents.Runner.run(
        agent,
        text,
        context=support_agent.SupportContext(
            requester_id="U_EVAL",
            origin_ref="https://slack.com/archives/C_EVAL/p10",
            runbooks_dir=pathlib.Path("runbooks"),
            ticket_backend=backend,
        ),
    )
    return result, backend


class TestGoldenCases:
    async def test_knowledge_question_admits_the_stub_and_offers_a_way_forward(self):
        # Given a knowledge question with no knowledge base connected
        # When Otto answers
        result, backend = await _run_otto("What's the firm's policy on USB drives?")

        # Then it does not invent an answer: it either says it can't verify
        # or escalates — never a confident fabrication
        answer = str(result.final_output).lower()
        could_not_verify = any(
            phrase in answer
            for phrase in ("verify", "couldn't find", "could not find", "not able")
        )
        assert could_not_verify or backend.escalations, answer

    async def test_runbook_walkthrough_uses_the_vpn_runbook(self):
        # Given the real runbooks directory
        # When a user asks for VPN help
        result, _ = await _run_otto("My VPN won't connect, can you walk me through fixing it?")

        # Then the answer draws on the VPN runbook content
        answer = str(result.final_output).lower()
        assert "vpn" in answer, answer

    async def test_access_request_pauses_for_human_approval(self):
        # Given a fully-specified access request
        # When Otto handles it
        result, _ = await _run_otto(
            "Please request access for me: system Snowflake, entitlement reporting-read, "
            "justification: I prepare the quarterly board reports."
        )

        # Then the run pauses on the gated tool instead of finishing
        assert result.interruptions, str(result.final_output)
