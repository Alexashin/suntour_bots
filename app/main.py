import asyncio
import logging

import uvicorn

from app.api import create_api
from app.bots.max_bot import MaxClient, run_max_polling
from app.bots.telegram_bot import run_telegram
from app.config import get_settings
from app.db import init_db
from app.logging_config import configure_logging
from app.notifier import AdminNotifier
from app.telegram_factory import create_telegram_bot

log = logging.getLogger("app")


async def serve_api(app, log_level: str) -> None:
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level=log_level.lower(),
        loop="asyncio",
        access_log=log_level.upper() == "DEBUG",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def log_bot_info(settings, telegram_bot, max_client) -> None:
    if telegram_bot:
        try:
            me = await telegram_bot.get_me()
            log.info("Telegram bot: @%s | https://t.me/%s", me.username, me.username)
        except Exception:
            log.warning("Could not fetch Telegram bot info", exc_info=True)

    if max_client:
        try:
            me = await max_client._request("GET", "/me")
            username = me.get("username") if isinstance(me, dict) else None
            name = me.get("name") if isinstance(me, dict) else None
            log.info("MAX bot: %s | username=%s", name, username)
            if username:
                log.info("MAX link: https://max.ru/%s", username)
        except Exception:
            log.warning("Could not fetch MAX bot info", exc_info=True)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)

    log.info("Starting Suntour Bots environment=%s log_level=%s", settings.app_env, settings.log_level)
    log.info(
        "Telegram proxy: %s",
        settings.telegram_proxy if settings.telegram_proxy else "disabled",
    )

    if not settings.telegram_admin_id_list and not settings.max_admin_user_id_list and not settings.max_admin_chat_id_list:
        log.warning(
            "No admin notification destinations configured. "
            "Use /whoami and set TELEGRAM_ADMIN_IDS or MAX_ADMIN_USER_IDS/MAX_ADMIN_CHAT_IDS before testing leads."
        )
    else:
        log.info(
            "Admin destinations configured: telegram=%s max_users=%s max_chats=%s",
            len(settings.telegram_admin_id_list),
            len(settings.max_admin_user_id_list),
            len(settings.max_admin_chat_id_list),
        )

    await init_db()
    log.info("Database initialized")

    max_client = MaxClient(settings) if settings.max_token else None
    if max_client:
        await max_client.open()

    telegram_bot = create_telegram_bot(settings) if settings.telegram_token else None
    notifier = AdminNotifier(settings, telegram_bot, max_client)

    await log_bot_info(settings, telegram_bot, max_client)

    if max_client is None:
        # API ожидает MaxClient, но endpoint MAX всё равно недоступен без токена.
        max_client = MaxClient(settings)

    api = create_api(settings, max_client, notifier)
    tasks: list[asyncio.Task] = [
        asyncio.create_task(serve_api(api, settings.log_level), name="api"),
    ]

    if telegram_bot and settings.telegram_enable_polling:
        tasks.append(
            asyncio.create_task(
                run_telegram(settings, telegram_bot, notifier, max_sender=max_client if settings.max_token else None),
                name="telegram",
            )
        )

    if settings.max_token:
        if settings.max_mode == "polling":
            tasks.append(asyncio.create_task(run_max_polling(max_client, notifier), name="max-polling"))
        else:
            if not settings.max_webhook_url:
                raise RuntimeError("PUBLIC_DOMAIN must be set when MAX_MODE=webhook")
            result = await max_client.create_subscription()
            log.info("MAX webhook subscription result: %s", result)

    try:
        await asyncio.gather(*tasks)
    finally:
        log.info("Shutting down")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if telegram_bot:
            await telegram_bot.session.close()
        if max_client:
            await max_client.close()


if __name__ == "__main__":
    asyncio.run(main())
