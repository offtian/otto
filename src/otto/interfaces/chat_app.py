"""
Streamlit chat surface that mimics the Slack integration on the host: the
real Otto agent, the same telemetry pipeline (OTLP → Tempo/Langfuse), and
the same HITL approval round-trip (serialized RunState, approve/reject,
resume) — minus Slack itself. Streamlit is a dev-group dependency; this
module is only imported by `streamlit run` (just chat), never by the app.
"""

import asyncio
import pathlib

import agents
import logfire
import streamlit as st

from otto.domain.support import agent as support_agent
from otto.settings import settings
from otto.utils import telemetry
from otto.vendors import llm


class _NoopTicketBackend:
    """
    Return a fake reference — enough for the agent's escalate tool to run
    without a real triage channel.
    """

    async def escalate(
        self, *, subject: str, summary: str, urgency: str, requester_id: str, origin_ref: str
    ) -> str:
        return "streamlit-escalation-1"


def _build() -> tuple[agents.Agent[support_agent.SupportContext], support_agent.SupportContext]:
    """
    Build the agent + context fresh per interaction: the AsyncOpenAI client
    binds to the current event loop, and each asyncio.run() uses a new one.
    """
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
        requester_id="U_STREAMLIT",
        origin_ref="streamlit",
        runbooks_dir=pathlib.Path(settings.runbooks_dir),
        ticket_backend=_NoopTicketBackend(),
    )
    # build_model() disables SDK tracing, which also starves the logfire→OTLP
    # export; re-enable it so every chat turn emits a trace.
    agents.set_tracing_disabled(disabled=False)
    return agent, context


async def _run_turn(question: str) -> agents.RunResult:
    agent, context = _build()
    items = [*st.session_state.input_items, {"role": "user", "content": question}]
    return await agents.Runner.run(agent, items, context=context)


async def _resume(state_json: str, *, approved: bool) -> agents.RunResult:
    # Same round-trip as the Slack integration (application/support.py):
    # deserialize the RunState, re-supply the context via override (approvals
    # are recorded on the state's context wrapper), then continue the run.
    agent, context = _build()
    state = await agents.RunState.from_string(
        agent,
        state_json,
        context_override=agents.RunContextWrapper(context=context),
    )
    for interruption in state.get_interruptions():
        if approved:
            state.approve(interruption)
        else:
            state.reject(interruption)
    return await agents.Runner.run(agent, state)


def _absorb(result: agents.RunResult) -> None:
    st.session_state.input_items = result.to_input_list()
    if result.interruptions:
        raw = result.interruptions[0].raw_item
        st.session_state.pending = {
            "state_json": result.to_state().to_string(),
            "tool": getattr(raw, "name", "tool"),
            "args": getattr(raw, "arguments", ""),
        }
    else:
        st.session_state.pending = None
        st.session_state.history.append(("assistant", str(result.final_output)))
    logfire.force_flush()  # make the trace visible in Tempo/Langfuse right away


if "input_items" not in st.session_state:
    st.session_state.input_items = []
    st.session_state.history = []
    st.session_state.pending = None

st.title("Otto — dev chat")
st.caption(
    f"model={settings.llm_model} via {settings.llm_base_url} · "
    f"traces → {settings.otlp_endpoint or 'no OTLP'}"
    f"{' + Langfuse' if settings.langfuse_host else ''}"
)

# One example per capability; the access request exercises the HITL approval.
_EXAMPLES = (
    "Any quick tip for VPN drops on hotel wifi?",
    "What runbooks can you walk me through?",
    "I've joined the Data Platform team and need read access to the Snowflake "
    "reporting warehouse to build the quarterly dashboards. "
    "Justification: quarterly reporting.",
    "This is urgent — my laptop won't boot at all. Please get me a human.",
)
with st.sidebar:
    st.subheader("Example questions")
    for _example in _EXAMPLES:
        if st.button(_example, use_container_width=True, disabled=bool(st.session_state.pending)):
            st.session_state.queued_question = _example

for role, text in st.session_state.history:
    st.chat_message(role).write(text)

if st.session_state.pending:
    pending = st.session_state.pending
    with st.chat_message("assistant"):
        st.warning(f"Human approval required: `{pending['tool']}({pending['args']})`")
        approve_col, reject_col = st.columns(2)
        if approve_col.button("Approve", type="primary"):
            with st.spinner("Resuming…"):
                _absorb(asyncio.run(_resume(pending["state_json"], approved=True)))
            st.rerun()
        if reject_col.button("Reject"):
            with st.spinner("Resuming…"):
                _absorb(asyncio.run(_resume(pending["state_json"], approved=False)))
            st.rerun()

question = st.chat_input(
    "Ask Otto…", disabled=bool(st.session_state.pending)
) or st.session_state.pop("queued_question", None)
if question:
    st.session_state.history.append(("user", question))
    st.chat_message("user").write(question)
    with st.spinner("Otto is thinking…"):
        _absorb(asyncio.run(_run_turn(question)))
    st.rerun()
