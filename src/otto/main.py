"""
Application entry point: ``python -m otto``.
"""

from otto import config
from otto.utils import logs


def main() -> None:
    """
    Run the application.
    """
    app_config = config.get_config()
    logs.configure_logging(level=app_config.settings.log_level)
    logs.log_event("app_started", params={"debug": app_config.settings.debug})


if __name__ == "__main__":
    main()
