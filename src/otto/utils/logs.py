"""
structlog configuration and logging helpers.

stdlib ``logging`` is forbidden everywhere else in the package (enforced by
import-linter) — import this module instead.
"""

import logging

import structlog


def configure_logging(*, level: str = "INFO") -> None:
    """
    Configure structlog for the process. Call once, at the entry point.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()],
        ),
        cache_logger_on_first_use=True,
    )


_logger = structlog.get_logger()


def log_event(event: str, *, params: dict[str, object] | None = None) -> None:
    """
    Log a structured event: ``log_event("order_created", params={"order_id": order.id})``.
    """
    _logger.info(event, **(params or {}))


def log_exception(exc: BaseException, *, params: dict[str, object] | None = None) -> None:
    """
    Log an exception with traceback. Never format the message into the event name.
    """
    _logger.error(type(exc).__name__, exc_info=exc, **(params or {}))
