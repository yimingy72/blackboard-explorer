"""Migrate the database before serving the blackboard API."""

from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings


def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]
    service_root = Path(__file__).resolve().parents[2]
    config = Config(str(service_root / "alembic.ini"))
    config.set_main_option("script_location", str(service_root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        settings.database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    command.upgrade(config, "head")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
