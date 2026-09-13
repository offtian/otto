"""
Model construction for the OpenAI Agents SDK against an OpenAI-compatible
gateway (LiteLLM, firm proxy) or api.openai.com directly, plus the zero-shot
classifiers the routing seams run on.
"""

import json
import typing

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


INTENT_ACCESS = "access_request"

# The full intent taxonomy. The flow graph currently routes only
# access_request to its own node; the service-owned intents (provisioning,
# troubleshooting, …) route to team-profile flows once those exist — the
# classifier already reads the services that resolution will key on.
INTENTS = (
    INTENT_ACCESS,
    "provisioning",
    "troubleshooting",
    "how_to",
    "desktop_support",
    "feature_request",
    "engineering_change",
)


@attrs.frozen
class IntentReading:
    """
    One classified inbound request: the intent label (None = unclassified —
    callers fall back to the general flow) and the service/tool names the
    request mentions, the key owning-team resolution routes on. The services
    are derived from untrusted message text: never log them (NFR2).
    """

    intent: str | None = None
    services: tuple[str, ...] = ()


@attrs.frozen
class LLMIntentClassifier:
    """
    Routes each inbound request to a support flow with one zero-shot JSON
    completion. Fail-open like the team classifier: any error or
    off-taxonomy reply yields an empty reading, never a blocked request.
    """

    client: openai.AsyncOpenAI
    model_name: str

    async def classify(self, *, text: str) -> IntentReading:
        """
        Return the request's intent reading; empty on any failure.
        """
        prompt = (
            "Classify this tech-support request.\n"
            f"intent: exactly one of {', '.join(INTENTS)}.\n"
            "services: the product/tool/system names the request mentions "
            '(e.g. ["coder", "jenkins"]), lowercase, [] if none.\n'
            'Reply with JSON only: {"intent": "...", "services": [...]}\n\n'
            f"Request:\n{text}"
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                # Generous for local thinking models; fail-open covers a miss.
                timeout=30.0,
            )
            data = _lenient_json((response.choices[0].message.content or "").strip())
        except Exception as exc:
            logs.log_exception(exc, params={"classifier": "intent"})
            return IntentReading()
        intent = data.get("intent")
        services = data.get("services")
        return IntentReading(
            intent=intent if intent in INTENTS else None,
            services=(
                tuple(str(service).lower() for service in services)
                if isinstance(services, list)
                else ()
            ),
        )


def _lenient_json(reply: str) -> dict[str, typing.Any]:
    """
    Return the reply's JSON object, tolerating chatter or code fences around
    it (local thinking models decorate their answers).

    :raises ValueError: if the reply carries no JSON object.
    :raises TypeError: if the reply's JSON is not an object.
    """
    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in classifier reply")
    data = json.loads(reply[start : end + 1])
    if not isinstance(data, dict):
        raise TypeError("classifier reply is not a JSON object")
    return data


def build_intent_classifier(
    *,
    base_url: str,
    api_key: str,
    model_name: str,
) -> LLMIntentClassifier:
    """
    Return the LLM intent classifier against the configured gateway.
    """
    return LLMIntentClassifier(
        client=openai.AsyncOpenAI(base_url=base_url or None, api_key=api_key or "unset"),
        model_name=model_name,
    )
