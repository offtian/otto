"""
NFR2 boundary: INFO logs carry structured ids only — never prompt/completion
content. `instrument_openai_agents()` records chat text in *spans* (accepted
for the prototype, D4); the INFO-log helpers here are the content boundary, so
these tests pin that they emit only the event name and the params they are
given.
"""

from unittest import mock

from otto.utils import logs


class TestLogEvent:
    def test_passes_only_the_event_name_and_params(self, monkeypatch):
        # Given the module logger is captured
        captured = mock.Mock()
        monkeypatch.setattr(logs, "_logger", captured)

        # When a hot-path event is logged the way the app logs it
        logs.log_event("support_request_handled", params={"request_id": "Ev-1", "paused": False})

        # Then only the event name and its explicit params reach the logger
        captured.info.assert_called_once_with(
            "support_request_handled", request_id="Ev-1", paused=False
        )

    def test_logs_just_the_event_when_no_params_are_given(self, monkeypatch):
        # Given the module logger is captured
        captured = mock.Mock()
        monkeypatch.setattr(logs, "_logger", captured)

        # When an event without params is logged
        logs.log_event("app_started")

        # Then nothing but the event name is emitted
        captured.info.assert_called_once_with("app_started")


class TestLogException:
    def test_names_the_exception_type_and_keeps_the_message_out_of_the_event(self, monkeypatch):
        # Given the module logger is captured and an exception with a sensitive message
        captured = mock.Mock()
        monkeypatch.setattr(logs, "_logger", captured)
        exc = ValueError("user@example.com failed with token sk-secret")

        # When it is logged with params
        logs.log_exception(exc, params={"request_id": "Ev-1"})

        # Then the event name is the exception TYPE, not its message text
        (event_name,), kwargs = captured.error.call_args
        assert event_name == "ValueError"
        assert "sk-secret" not in event_name
        # Then the message rides in exc_info (traceback), and params are preserved
        assert kwargs["exc_info"] is exc
        assert kwargs["request_id"] == "Ev-1"
