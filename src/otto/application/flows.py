"""
The support flow graph: one classify node routes each inbound request to the
agent node that owns it. Agent nodes are the only ones that may suspend
(HITL) — ``support`` persists the suspended node name on the approval, and a
human decision re-enters the graph exactly there.

State keys the nodes read: ``text`` (the raw request, for the classifier),
``input`` (the rebuilt conversation input), ``ctx`` (the ``SupportContext``),
and optionally ``resume`` (a paused run's serialized state plus the human
decision).
"""

import agents

from otto import config
from otto.application import graph
from otto.domain.support import agent as support_agent
from otto.utils import logs
from otto.vendors import llm


ACCESS = "access"
GENERAL = "general"


async def _classify(state: graph.State) -> graph.NodeResult:
    """
    Route the request to a flow node. Fail-open: no classifier, no access
    agent, a classifier error, or a non-access intent all land on the
    general agent — misrouting can only cost focus, never capability.
    """
    cfg = config.get_config()
    if cfg.intent_classifier is None or cfg.access_agent is None:
        return graph.Goto(GENERAL)
    reading = await cfg.intent_classifier.classify(text=state["text"])
    node = ACCESS if reading.intent == llm.INTENT_ACCESS else GENERAL
    # Only the intent is logged, never the extracted services — they are
    # derived from untrusted message text (NFR2).
    logs.log_event("intent_routed", params={"intent": reading.intent, "node": node})
    return graph.Goto(node)


async def _access(state: graph.State) -> graph.NodeResult:
    cfg = config.get_config()
    # A pause can outlive a deploy: if the access agent is gone on resume,
    # the general agent (same gated tools) still carries the decision out.
    return await _run_agent(cfg.access_agent or cfg.agent, state)


async def _general(state: graph.State) -> graph.NodeResult:
    return await _run_agent(config.get_config().agent, state)


async def _run_agent(
    agent: agents.Agent[support_agent.SupportContext], state: graph.State
) -> graph.NodeResult:
    """
    Run one agent as a graph node: a fresh run on ``input``, or — when
    ``resume`` is present — a paused run continued with the human decision
    applied. Interruptions suspend the node with the ``RunResult`` as
    payload; anything else completes it.
    """
    resume = state.pop("resume", None)
    if resume is None:
        result = await agents.Runner.run(agent, state["input"], context=state["ctx"])
    else:
        # The context must be re-supplied via from_string, NOT via Runner.run:
        # a context passed to run() replaces the state's context wrapper, which
        # is where approve()/reject() decisions are recorded — the run would
        # re-interrupt forever.
        run_state = await agents.RunState.from_string(
            agent,
            resume["run_state_json"],
            context_override=agents.RunContextWrapper(context=state["ctx"]),
        )
        for interruption in run_state.get_interruptions():
            if resume["approved"]:
                run_state.approve(interruption)
            else:
                run_state.reject(interruption)
        result = await agents.Runner.run(agent, run_state)
    if result.interruptions:
        return graph.Suspend(payload=result)
    return graph.Done(output=result)


SUPPORT = graph.Graph(
    name="support",
    start="classify",
    nodes={"classify": _classify, ACCESS: _access, GENERAL: _general},
)
