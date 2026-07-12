"""
Model construction for the OpenAI Agents SDK against an OpenAI-compatible
gateway (LiteLLM, firm proxy) or api.openai.com directly.
"""

import agents
import openai


def build_model(
    *,
    base_url: str,
    api_key: str,
    model_name: str,
) -> agents.OpenAIChatCompletionsModel:
    """
    Return the Agents SDK model wired to the configured gateway.

    Chat Completions (not Responses) is the transport because most internal
    gateways only proxy ``/v1/chat/completions``; switch to
    ``agents.OpenAIResponsesModel`` if the gateway supports ``/v1/responses``.
    """
    client = openai.AsyncOpenAI(
        base_url=base_url or None,
        api_key=api_key or "unset",
    )
    # The SDK's built-in tracing uploads to OpenAI's traces endpoint, which
    # a gateway won't accept — Logfire/OTel is the tracing path instead
    # (see utils/telemetry.py).
    agents.set_tracing_disabled(disabled=True)
    return agents.OpenAIChatCompletionsModel(model=model_name, openai_client=client)
