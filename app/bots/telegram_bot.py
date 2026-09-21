import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.config import Settings
from app.db import SessionLocal
from app.scenario import CONTACT_CHAT, CONTACT_PHONE, consume, start_scenario
from app.services import (
    create_lead,
    format_lead,
    get_or_create_conversation,
    reset_conversation,
    save_conversation,
    set_subscription,
    stats,
    subscribed_users,
    upsert_user,
)

log = logging.getLogger(__name__)


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="В чате"), KeyboardButton(text="По телефону")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def create_router(settings: Settings, max_sender=None) -> Router:
    router = Router()

    def is_admin(user_id: int) -> bool:
        return user_id in settings.telegram_admin_id_list

    @router.message(Command("whoami"))
    async def whoami(message: Message) -> None:
        await message.answer(f"Telegram user_id: {message.from_user.id}\nchat_id: {message.chat.id}")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        async with SessionLocal() as session:
            await upsert_user(
                session,
                platform="telegram",
                platform_user_id=message.from_user.id,
                chat_id=message.chat.id,
                username=message.from_user.username,
                display_name=message.from_user.full_name,
            )
            await reset_conversation(
                session,
                platform="telegram",
                platform_user_id=message.from_user.id,
            )
            await session.commit()

        result = start_scenario()
        await message.answer(result.reply, reply_markup=ReplyKeyboardRemove())

    @router.message(Command("subscribe"))
    async def subscribe(message: Message) -> None:
        async with SessionLocal() as session:
            user = await upsert_user(
                session,
                platform="telegram",
                platform_user_id=message.from_user.id,
                chat_id=message.chat.id,
                username=message.from_user.username,
                display_name=message.from_user.full_name,
            )
            await set_subscription(session, user, enabled=True)
            await session.commit()
        await message.answer("Подписка на рассылку включена ✅\nОтписаться можно в любой момент командой /unsubscribe.")

    @router.message(Command("unsubscribe"))
    async def unsubscribe(message: Message) -> None:
        async with SessionLocal() as session:
            user = await upsert_user(
                session,
                platform="telegram",
                platform_user_id=message.from_user.id,
                chat_id=message.chat.id,
                username=message.from_user.username,
                display_name=message.from_user.full_name,
            )
            await set_subscription(session, user, enabled=False)
            await session.commit()
        await message.answer("Рассылка отключена. Вернуть её можно командой /subscribe.")

    @router.message(Command("stats"))
    async def show_stats(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        async with SessionLocal() as session:
            s = await stats(session)
        await message.answer(
            f"Пользователей: {s['users']}\n"
            f"Заявок: {s['leads']}\n"
            f"Подписчиков: {s['subscribers']}"
        )

    @router.message(Command("broadcast"))
    async def broadcast(message: Message, bot: Bot) -> None:
        if not is_admin(message.from_user.id):
            return

        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            await message.answer("Использование: /broadcast текст рассылки")
            return

        async with SessionLocal() as session:
            users = await subscribed_users(session)

        sent = 0
        failed = 0
        for user in users:
            try:
                if user.platform == "telegram" and user.chat_id:
                    await bot.send_message(user.chat_id, text)
                    sent += 1
                elif (
                    user.platform == "max"
                    and max_sender
                    and settings.max_marketing_broadcast_enabled
                ):
                    await max_sender.send_to_user(user.platform_user_id, text)
                    sent += 1
            except Exception:
                failed += 1
                log.exception("Broadcast failed for %s:%s", user.platform, user.platform_user_id)
            await asyncio.sleep(settings.broadcast_delay_seconds)

        await message.answer(f"Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}")

    @router.message(F.text)
    async def text_handler(message: Message, bot: Bot) -> None:
        text = message.text.strip()

        async with SessionLocal() as session:
            user = await upsert_user(
                session,
                platform="telegram",
                platform_user_id=message.from_user.id,
                chat_id=message.chat.id,
                username=message.from_user.username,
                display_name=message.from_user.full_name,
            )
            conv = await get_or_create_conversation(
                session,
                platform="telegram",
                platform_user_id=message.from_user.id,
            )

            result = consume(conv.step, text, conv.data)

            if result.completed:
                lead = await create_lead(session, user=user, data=result.data)
                await reset_conversation(
                    session,
                    platform="telegram",
                    platform_user_id=message.from_user.id,
                )
                await session.commit()

                await message.answer(result.reply, reply_markup=ReplyKeyboardRemove())
                lead_text = format_lead(lead, user)
                for admin_id in settings.telegram_admin_id_list:
                    try:
                        await bot.send_message(admin_id, lead_text)
                    except Exception:
                        log.exception("Could not notify Telegram admin %s", admin_id)
                if max_sender:
                    await max_sender.notify_admins(lead_text)
                return

            await save_conversation(
                session,
                conv,
                step=result.next_step or "destination",
                data=result.data,
            )
            await session.commit()

        markup = contact_keyboard() if result.next_step == "contact_method" else ReplyKeyboardRemove()
        await message.answer(result.reply, reply_markup=markup)

    return router


async def run_telegram(settings: Settings, max_sender=None) -> None:
    if not settings.telegram_token or not settings.telegram_enable_polling:
        log.warning("Telegram disabled: token missing or polling disabled")
        return

    bot = Bot(settings.telegram_token)
    dp = Dispatcher()
    dp.include_router(create_router(settings, max_sender=max_sender))
    log.info("Telegram polling started")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
