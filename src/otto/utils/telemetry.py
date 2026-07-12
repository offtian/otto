"""
Telemetry bootstrap: Logfire as the OpenTelemetry SDK + instrumentation layer.

Spans flow to two independent sinks — enable either, both, or neither:

- Logfire (best UI for OpenAI Agents SDK traces) when a token is provided
- any OTLP collector (Jaeger in dev, the firm APM in prod) when an
  endpoint is provided

``logfire.instrument_openai_agents()`` captures every agent run, tool call
and LLM generation; FastAPI requests are instrumented per-app via
``instrument_app``.
"""

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
) -> None:
    """
    Configure tracing for the process. Idempotent — safe to call from both
    the app factory and test fixtures.

    :param service_name: OTel ``service.name`` resource attribute.
    :param environment: deployment environment tag (dev/staging/prod).
    :param logfire_token: Logfire write token; empty disables the Logfire sink.
    :param otlp_endpoint: OTLP/HTTP collector base URL; empty disables OTLP.
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

    logfire.configure(
        service_name=service_name,
        environment=environment,
        token=logfire_token or None,
        send_to_logfire=bool(logfire_token),
        additional_span_processors=additional_processors,
        console=False,
    )
    logfire.instrument_openai_agents()


def instrument_app(app: object) -> None:
    """
    Attach request-level tracing to a FastAPI app.
    """
    logfire.instrument_fastapi(app)  # type: ignore[arg-type]
