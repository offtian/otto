"""
Model construction for the OpenAI Agents SDK against an OpenAI-compatible
gateway (LiteLLM, firm proxy) or api.openai.com directly.
"""

import agents
import attrs
import openai

from otto.utils import logs


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


@attrs.frozen
class LLMTeamClassifier:
    """
    Dev stand-in for the firm ticket-classification API (D24): one zero-shot
    completion assigns an escalation to a team from a fixed taxonomy, or None
    when no team confidently applies. Fail-open — any error or off-taxonomy
    reply means "unclassified", never a blocked escalation. The firm API
    replaces this class at graduation behind the same ``classify`` seam.
    """

    client: openai.AsyncOpenAI
    model_name: str
    teams: tuple[str, ...]

    async def classify(self, *, text: str) -> str | None:
        """
        Return the assigned team for this escalation text, or None.
        """
        prompt = (
            "Assign this tech-support escalation to exactly one team from the "
            f"list: {', '.join(self.teams)}. Reply with the team name only, or "
            "'unknown' if none clearly applies.\n\n"
            f"Escalation:\n{text}"
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                # Generous for local thinking models (qwen emits reasoning
                # tokens before the one-word answer); fail-open covers a miss.
                timeout=30.0,
            )
            reply = (response.choices[0].message.content or "").strip().lower()
        except Exception as exc:
            logs.log_exception(exc, params={"classifier": "team"})
            return None
        return next((team for team in self.teams if team.lower() == reply), None)


def build_team_classifier(
    *,
    base_url: str,
    api_key: str,
    model_name: str,
    teams: tuple[str, ...],
) -> LLMTeamClassifier:
    """
    Return the LLM team classifier against the configured gateway.
    """
    return LLMTeamClassifier(
        client=openai.AsyncOpenAI(base_url=base_url or None, api_key=api_key or "unset"),
        model_name=model_name,
        teams=teams,
    )
