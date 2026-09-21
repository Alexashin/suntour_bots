from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from app.config import Settings


def create_telegram_bot(settings: Settings) -> Bot:
    if settings.telegram_proxy:
        return Bot(
            settings.telegram_token,
            session=AiohttpSession(proxy=settings.telegram_proxy),
        )
    return Bot(settings.telegram_token)
