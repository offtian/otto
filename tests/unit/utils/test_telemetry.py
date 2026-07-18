"""
The Langfuse sink rides the existing OTLP pipeline — these tests pin the
endpoint path and Basic-auth header that Langfuse's OTel ingestion expects.
"""

import base64
from unittest import mock

from otto.utils import telemetry


class TestSetupTelemetry:
    def test_registers_a_langfuse_otlp_exporter_with_basic_auth(self, monkeypatch):
        # Given a fresh telemetry bootstrap with Logfire captured
        monkeypatch.setattr(telemetry, "_configured", False)
        monkeypatch.setattr(telemetry, "logfire", mock.Mock())
        with mock.patch.object(telemetry.trace_exporter, "OTLPSpanExporter") as exporter_class:
            # When telemetry is set up with a Langfuse host and key pair
            telemetry.setup_telemetry(
                service_name="otto",
                environment="test",
                logfire_token="",
                otlp_endpoint="",
                langfuse_host="http://localhost:3000/",
                langfuse_public_key="pk-lf-otto-dev",
                langfuse_secret_key="sk-lf-otto-dev",
            )

        # Then the exporter targets Langfuse's OTel ingestion path with Basic auth
        expected_auth = base64.b64encode(b"pk-lf-otto-dev:sk-lf-otto-dev").decode()
        exporter_class.assert_called_once_with(
            endpoint="http://localhost:3000/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {expected_auth}"},
        )

    def test_skips_the_langfuse_sink_when_no_keys_are_configured(self, monkeypatch):
        # Given a fresh telemetry bootstrap with Logfire captured
        monkeypatch.setattr(telemetry, "_configured", False)
        monkeypatch.setattr(telemetry, "logfire", mock.Mock())
        with mock.patch.object(telemetry.trace_exporter, "OTLPSpanExporter") as exporter_class:
            # When telemetry is set up with a Langfuse host but no keys
            telemetry.setup_telemetry(
                service_name="otto",
                environment="test",
                logfire_token="",
                otlp_endpoint="",
                langfuse_host="http://localhost:3000",
            )

        # Then no OTLP exporter is created at all
        exporter_class.assert_not_called()
