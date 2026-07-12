"""
Application entry point: ``python -m otto`` serves the Otto API.
"""

import uvicorn

from otto import config
from otto.utils import logs


def main() -> None:
    """
    Boot the FastAPI service (Slack events, interactions, health).
    """
    app_config = config.get_config()
    logs.configure_logging(level=app_config.settings.log_level)
    uvicorn.run(
        "otto.interfaces.app:app",
        host="0.0.0.0",
        port=8000,
        log_level=app_config.settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
