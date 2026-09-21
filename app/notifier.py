import logging

from aiogram import Bot

from app.config import Settings

log = logging.getLogger(__name__)


class AdminNotifier:
    def __init__(self, settings: Settings, telegram_bot: Bot | None, max_client):
        self.settings = settings
        self.telegram_bot = telegram_bot
        self.max_client = max_client

    async def notify(self, text: str) -> None:
        delivered = 0

        if self.telegram_bot:
            for admin_id in self.settings.telegram_admin_id_list:
                try:
                    await self.telegram_bot.send_message(admin_id, text)
                    delivered += 1
                except Exception:
                    log.exception("Failed to notify Telegram admin id=%s", admin_id)

        if self.max_client:
            delivered += await self.max_client.notify_admins(text)

        if delivered:
            log.info("Lead notification delivered to %s admin destination(s)", delivered)
        else:
            log.warning("Lead created but no admin notification destination succeeded")
