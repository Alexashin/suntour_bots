import asyncio
import logging

import uvicorn

from app.api import create_api
from app.bots.max_bot import MaxClient, run_max_polling
from app.bots.telegram_bot import run_telegram
from app.config import get_settings
from app.db import init_db


async def serve_api(app) -> None:
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("app")

    await init_db()

    max_client = MaxClient(settings)
    await max_client.open()

    api = create_api(settings, max_client)
    tasks: list[asyncio.Task] = [
        asyncio.create_task(serve_api(api), name="api"),
    ]

    if settings.telegram_token and settings.telegram_enable_polling:
        tasks.append(
            asyncio.create_task(
                run_telegram(settings, max_sender=max_client),
                name="telegram",
            )
        )

    if settings.max_token:
        if settings.max_mode == "polling":
            tasks.append(
                asyncio.create_task(
                    run_max_polling(max_client),
                    name="max-polling",
                )
            )
        else:
            if not settings.max_webhook_url:
                raise RuntimeError("PUBLIC_DOMAIN must be set when MAX_MODE=webhook")
            result = await max_client.create_subscription()
            log.info("MAX webhook subscription result: %s", result)

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await max_client.close()


if __name__ == "__main__":
    asyncio.run(main())
