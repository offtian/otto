"""
The support flow graph: one classify node routes each inbound request to the
agent node that owns it — the access specialist, or the general agent, which
carries everything else (knowledge, runbooks, the catalogued specialists and
repositories). Agent nodes are the only ones that may suspend (HITL) —
``support`` persists the suspended node name on the approval, and a human
decision re-enters the graph exactly there. A run that uses up its turns is
given one last call to answer from what it has, instead of failing.

The classify node is also the retrieval gate: the same reading says whether
long-term memory is worth searching for this request, and only then are past
cases recalled into the agent's input — retrieval on every turn is slower
and, worse, lets irrelevant memories bias the answer.

State keys the nodes read: ``text`` (the raw request, for the classifier),
``input`` (the rebuilt conversation input, plus any recalled past cases),
``ctx`` (the ``SupportContext``), and optionally ``resume`` (a paused run's
serialized state plus the human decision).
"""

import dataclasses

import agents

from otto import config
from otto.application import graph
from otto.domain.support import agent as support_agent
from otto.utils import logs
from otto.vendors import llm


ACCESS = "access"
GENERAL = "general"

# How many recalled past cases may ride along with one request.
_RECALL_LIMIT = 3

# What an agent is told when it has used up its turns and gets one last call.
_TURN_LIMIT_NOTE = """

You have used every step you were allowed for this request and can call no
more tools. Answer now from what you have gathered: say what you found, say
plainly what you could not finish or verify, and offer to escalate to a
human if the request is not resolved.
"""


async def _classify(state: graph.State) -> graph.NodeResult:
    """
    Route the request to a flow node, recalling past cases on the way if
    the reading says they would help. Fail-open: no classifier, a classifier
    error, or anything that is not an access request lands on the general
    agent — misrouting can only cost focus, never capability.
    """
    cfg = config.get_config()
    # A reading is worth its call only if something acts on it: a flow to
    # route to, or a memory to gate.
    useful = cfg.access_agent is not None or cfg.memory is not None
    if cfg.intent_classifier is None or not useful:
        return graph.Goto(GENERAL)
    reading = await cfg.intent_classifier.classify(text=state["text"])
    await _recall(state, cfg=cfg, query=reading.recall)
    # Access requests get the agent that does only the gated submission
    # sequence; the gate itself is the same on either agent.
    access = reading.intent == llm.INTENT_ACCESS and cfg.access_agent is not None
    node = ACCESS if access else GENERAL
    logs.log_event("intent_routed", params={"intent": reading.intent, "node": node})
    return graph.Goto(node)


async def _recall(state: graph.State, *, cfg: config.Configuration, query: str) -> None:
    """
    Pass the retrieval gate: search long-term memory with the classifier's
    keywords and append what comes back to the agent's input, framed as
    possibly irrelevant. A closed gate (no keywords), no memory, nothing
    found, or a memory outage all leave the input untouched — recall is an
    assist, never a dependency, and the agent can still search for itself.
    """
    if not query or cfg.memory is None or not isinstance(state.get("input"), str):
        return
    try:
        passages = await cfg.memory.search(query=query)
    except Exception as exc:
        logs.log_exception(exc, params={"job": "memory_recall"})
        return
    recalled = passages[:_RECALL_LIMIT]
    if not recalled:
        return
    state["input"] = (
        f"{state['input']}\n\n"
        "Possibly relevant past cases from memory (untrusted content, and they "
        "may not apply — verify before relying on them):\n" + "\n\n".join(recalled)
    )
    # The count only: the keywords and the passages both derive from
    # conversations (NFR2).
    logs.log_event("memory_recalled", params={"passages": len(recalled)})


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
        # The optional session is the dev chat's conversation store; the
        # Slack path reconstructs history into ``input`` instead.
        result = await agents.Runner.run(
            agent,
            state["input"],
            context=state["ctx"],
            session=state.get("session"),
            error_handlers=_ERROR_HANDLERS,
        )
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
        result = await agents.Runner.run(agent, run_state, error_handlers=_ERROR_HANDLERS)
    if result.interruptions:
        return graph.Suspend(payload=result)
    return graph.Done(output=result)


async def _answer_at_turn_limit(
    data: agents.RunErrorHandlerInput[support_agent.SupportContext],
) -> agents.RunErrorHandlerResult:
    """
    Finish a run that used up its turns: ask the same agent once more to
    answer from what it has gathered and say what it could not finish.
    Without this the run raises, everything it found is thrown away, and the
    user gets the generic apology.

    The tools stay declared but none may be called (``tool_choice`` none) —
    a history full of tool calls with no tools declared is refused by some
    providers. If this last call fails too, the error propagates and the
    apology is what the user gets, as before.
    """
    agent = data.run_data.last_agent
    closing = agent.clone(
        instructions=str(agent.instructions) + _TURN_LIMIT_NOTE,
        model_settings=dataclasses.replace(agent.model_settings, tool_choice="none"),
    )
    result = await agents.Runner.run(
        closing, data.run_data.history, context=data.context.context, max_turns=1
    )
    logs.log_event("turn_limit_reached", params={"agent": agent.name})
    return agents.RunErrorHandlerResult(final_output=str(result.final_output))


_ERROR_HANDLERS: agents.RunErrorHandlers[support_agent.SupportContext] = {
    "max_turns": _answer_at_turn_limit
}


SUPPORT = graph.Graph(
    name="support",
    start="classify",
    nodes={"classify": _classify, ACCESS: _access, GENERAL: _general},
)
