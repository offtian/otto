"""
Telemetry bootstrap: Logfire as the OpenTelemetry SDK + instrumentation layer.

Spans flow to independent sinks — enable any subset:

- Logfire (best UI for OpenAI Agents SDK traces) when a token is provided
- any OTLP collector (the Grafana LGTM stack in dev, Alloy in prod) when
  an endpoint is provided
- Langfuse via its native OTLP ingestion endpoint when a host + key pair
  is provided — no Langfuse SDK needed, which would double-instrument the
  agent spans next to Logfire

``logfire.instrument_openai_agents()`` captures every agent run, tool call
and LLM generation; FastAPI requests are instrumented per-app via
``instrument_app``.
"""

import base64

import agents
import logfire
from opentelemetry.exporter.otlp.proto.http import trace_exporter
from opentelemetry.sdk.trace import export as trace_export


_configured = False


def setup_telemetry(
    *,
    service_name: str,
    environment: str,
    logfire_token: str,
    otlp_endpoint: str,
    langfuse_host: str = "",
    langfuse_public_key: str = "",
    langfuse_secret_key: str = "",
) -> None:
    """
    Configure tracing for the process. Idempotent — safe to call from both
    the app factory and test fixtures.

    :param service_name: OTel ``service.name`` resource attribute.
    :param environment: deployment environment tag (dev/staging/prod).
    :param logfire_token: Logfire write token; empty disables the Logfire sink.
    :param otlp_endpoint: OTLP/HTTP collector base URL; empty disables OTLP.
    :param langfuse_host: Langfuse base URL; empty disables the Langfuse sink.
    :param langfuse_public_key: Langfuse project public key (``pk-lf-...``).
    :param langfuse_secret_key: Langfuse project secret key (``sk-lf-...``).
    """
    global _configured  # noqa: PLW0603
    if _configured:
        return
    _configured = True

    additional_processors = []
    if otlp_endpoint:
        exporter = trace_exporter.OTLPSpanExporter(
            endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces",
        )
        additional_processors.append(trace_export.BatchSpanProcessor(exporter))
    if langfuse_host and langfuse_public_key and langfuse_secret_key:
        credentials = base64.b64encode(
            f"{langfuse_public_key}:{langfuse_secret_key}".encode()
        ).decode()
        langfuse_exporter = trace_exporter.OTLPSpanExporter(
            endpoint=f"{langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {credentials}"},
        )
        additional_processors.append(trace_export.BatchSpanProcessor(langfuse_exporter))

    logfire.configure(
        service_name=service_name,
        environment=environment,
        token=logfire_token or None,
        send_to_logfire=bool(logfire_token),
        additional_span_processors=additional_processors,
        console=False,
    )
    logfire.instrument_system_metrics()
    logfire.instrument_openai_agents()
    # Empty on purpose: this drops the SDK's default processor, which uploads
    # traces to the OpenAI dashboard and warns when OPENAI_API_KEY is unset
    # (we talk to Ollama/the gateway, not OpenAI). Do NOT pass the OTel
    # processors here — the SDK expects its own TracingProcessor interface
    # (on_trace_start/…), and ours are already registered with the OTel
    # pipeline via logfire.configure above. Logfire captures agent spans at
    # the trace-provider level, so it is unaffected by the empty list.
    agents.set_trace_processors([])


def instrument_app(app: object) -> None:
    """
    Attach request-level tracing to a FastAPI app.
    """
    logfire.instrument_fastapi(app)  # type: ignore[arg-type]
