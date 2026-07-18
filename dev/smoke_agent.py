"""
End-to-end smoke test for the agent pipeline, run on the host:

    just agent-smoke ["your question"]

Builds the real Otto agent against the configured model (Ollama/LiteLLM per
.env), runs one turn, and exports the trace to the configured OTLP sink.
Proves model reachability + Agents SDK + tracing are wired end-to-end, without
Slack/Jira/signatures. Open the trace in your tracing UI afterwards.
"""

import asyncio
import pathlib
import sys

import agents
import logfire

from otto.domain.support import agent as support_agent
from otto.settings import settings
from otto.utils import telemetry
from otto.vendors import llm


class _NoopTicketBackend:
    """
    Return a fake reference — enough for the agent's escalate tool to run in
    the smoke without a real triage channel.
    """

    async def escalate(
        self, *, subject: str, summary: str, urgency: str, requester_id: str, origin_ref: str
    ) -> str:
        return "smoke-escalation-1"


async def main() -> None:
    question = " ".join(sys.argv[1:]) or "Give me one quick tip for VPN drops on hotel wifi."
    telemetry.setup_telemetry(
        service_name=settings.otel_service_name,
        environment=settings.environment,
        logfire_token=settings.logfire_token,
        otlp_endpoint=settings.otlp_endpoint,
        langfuse_host=settings.langfuse_host,
        langfuse_public_key=settings.langfuse_public_key,
        langfuse_secret_key=settings.langfuse_secret_key,
    )
    model = llm.build_model(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model_name=settings.llm_model,
    )
    agent = support_agent.build_agent(model=model)
    context = support_agent.SupportContext(
        requester_id="U_SMOKE",
        origin_ref="smoke",
        runbooks_dir=pathlib.Path(settings.runbooks_dir),
        ticket_backend=_NoopTicketBackend(),
    )

    # build_model() disables SDK tracing, which also starves the logfire→OTLP
    # export; re-enable it here so this smoke actually emits a trace.
    agents.set_tracing_disabled(disabled=False)
    print(f"model={settings.llm_model} via {settings.llm_base_url}  otlp={settings.otlp_endpoint}")
    result = await agents.Runner.run(agent, question, context=context)
    if result.interruptions:
        print("\n=== PASS: agent paused for human approval (a gated tool fired) ===")
    else:
        print("\n=== agent output ===")
        print(result.final_output)
    logfire.force_flush()  # short-lived process: flush spans before exit


if __name__ == "__main__":
    asyncio.run(main())
