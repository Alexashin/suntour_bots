import logging

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        force=True,
    )

    # Наши модули следуют LOG_LEVEL, сторонние библиотеки в production не засоряют лог.
    third_party_level = logging.INFO if level <= logging.DEBUG else logging.WARNING
    for logger_name in (
        "aiogram",
        "aiohttp.access",
        "asyncio",
        "sqlalchemy.engine",
    ):
        logging.getLogger(logger_name).setLevel(third_party_level)

    logging.getLogger("uvicorn.access").setLevel(
        logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    )
