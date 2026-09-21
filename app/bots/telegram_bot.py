import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.config import Settings
from app.content import AGENCY_CONTACTS, SUBSCRIBE_TEXT, UNSUBSCRIBE_TEXT
from app.db import SessionLocal
from app.scenario import (
    MAIN_ACTION_CONTACTS,
    MAIN_ACTION_START,
    MAIN_ACTION_SUBSCRIBE,
    MAIN_ACTION_UNSUBSCRIBE,
    choice_rows,
    consume,
    edit_step,
    go_back,
    render_review,
    render_step,
    resolve_choice,
    review_edit_rows,
    start_scenario,
)
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


async def _safe_delete_user_message(message: Message) -> None:
    """Best-effort cleanup for manual wizard input in private chat.

    The wizard itself lives in one editable bot message. If Telegram allows
    deleting the user's just-entered answer, remove it so the chat keeps a
    clean "single card" look. Failure to delete must never break the flow.
    """
    try:
        await message.delete()
        log.debug(
            "Telegram manual input message deleted user_id=%s message_id=%s",
            message.from_user.id if message.from_user else None,
            message.message_id,
        )
    except TelegramBadRequest as exc:
        log.debug(
            "Telegram manual input message could not be deleted message_id=%s reason=%s",
            message.message_id,
            exc,
        )
    except Exception:
        log.debug(
            "Telegram manual input cleanup failed message_id=%s",
            message.message_id,
            exc_info=True,
        )


def _ikb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return _ikb([
        [InlineKeyboardButton(text=MAIN_ACTION_START, callback_data="menu|start")],
        [InlineKeyboardButton(text=MAIN_ACTION_SUBSCRIBE, callback_data="menu|subscribe")],
        [InlineKeyboardButton(text=MAIN_ACTION_UNSUBSCRIBE, callback_data="menu|unsubscribe")],
        [InlineKeyboardButton(text=MAIN_ACTION_CONTACTS, callback_data="menu|contacts")],
    ])


def wizard_keyboard(step: str, data: dict, *, display_name: str | None = None) -> InlineKeyboardMarkup | None:
    if step == "review":
        return _ikb([
            [InlineKeyboardButton(text="✅ Отправить заявку", callback_data="wiz|submit")],
            [InlineKeyboardButton(text="✏️ Изменить параметры", callback_data="wiz|edit")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="wiz|back"), InlineKeyboardButton(text="❌ Отменить", callback_data="wiz|cancel")],
        ])

    rows: list[list[InlineKeyboardButton]] = []
    for row in choice_rows(step, data, display_name=display_name):
        buttons = []
        for choice in row:
            callback = "wiz|manual" if choice.code == "manual" else f"wiz|set|{choice.code}"
            buttons.append(InlineKeyboardButton(text=choice.text, callback_data=callback))
        rows.append(buttons)

    if step not in {"children_ages", "phone"} and not any(
        button.callback_data == "wiz|manual" for row in rows for button in row
    ):
        rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="wiz|manual")])

    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="wiz|back"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="wiz|cancel"),
    ])
    return _ikb(rows)


def edit_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=c.text, callback_data=f"wiz|editstep|{c.value}") for c in row]
        for row in review_edit_rows()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ К заявке", callback_data="wiz|review")])
    return _ikb(rows)


def phone_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Или введите номер вручную",
    )


async def _upsert_tg_user(session, message: Message):
    return await upsert_user(
        session,
        platform="telegram",
        platform_user_id=message.from_user.id,
        chat_id=message.chat.id,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
    )


async def _set_ui_id(platform_user_id: int, message_id: int) -> None:
    async with SessionLocal() as session:
        conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=platform_user_id)
        data = dict(conv.data or {})
        data["_ui_message_id"] = message_id
        await save_conversation(session, conv, step=conv.step, data=data)
        await session.commit()


async def _render_card(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    text: str,
    step: str,
    data: dict,
    display_name: str | None,
    preferred_message_id: int | None = None,
) -> int:
    markup = wizard_keyboard(step, data, display_name=display_name)
    message_id = preferred_message_id or data.get("_ui_message_id")
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            await _set_ui_id(user_id, int(message_id))
            return int(message_id)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return int(message_id)
            log.debug("Telegram wizard card edit failed; sending new card", exc_info=True)

    sent = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    await _set_ui_id(user_id, sent.message_id)
    return sent.message_id


async def _subscribe_user(message: Message, enabled: bool) -> None:
    async with SessionLocal() as session:
        user = await _upsert_tg_user(session, message)
        await set_subscription(session, user, enabled=enabled)
        await session.commit()
    log.info("Telegram subscription changed user_id=%s enabled=%s", message.from_user.id, enabled)
    await message.answer(SUBSCRIBE_TEXT if enabled else UNSUBSCRIBE_TEXT, reply_markup=main_menu_keyboard())


async def _subscribe_callback(callback: CallbackQuery, enabled: bool) -> None:
    msg = callback.message
    async with SessionLocal() as session:
        user = await upsert_user(
            session,
            platform="telegram",
            platform_user_id=callback.from_user.id,
            chat_id=msg.chat.id,
            username=callback.from_user.username,
            display_name=callback.from_user.full_name,
        )
        await set_subscription(session, user, enabled=enabled)
        await session.commit()
    await callback.answer("Подписка включена" if enabled else "Рассылка отключена")
    await msg.edit_text(SUBSCRIBE_TEXT if enabled else UNSUBSCRIBE_TEXT, reply_markup=main_menu_keyboard())


async def _start_flow_from_message(message: Message, bot: Bot) -> None:
    async with SessionLocal() as session:
        await _upsert_tg_user(session, message)
        conv = await reset_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
        await session.commit()
        data = dict(conv.data)
    result = start_scenario()
    log.info("Telegram scenario started user_id=%s", message.from_user.id)
    await _render_card(
        bot,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=result.reply,
        step=result.next_step,
        data=result.data,
        display_name=message.from_user.full_name,
    )


async def _start_flow_from_callback(callback: CallbackQuery, bot: Bot) -> None:
    async with SessionLocal() as session:
        await upsert_user(
            session,
            platform="telegram",
            platform_user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            username=callback.from_user.username,
            display_name=callback.from_user.full_name,
        )
        conv = await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
        data = dict(conv.data)
        data["_ui_message_id"] = callback.message.message_id
        await save_conversation(session, conv, step="departure_city", data=data)
        await session.commit()
    result = start_scenario()
    result.data["_ui_message_id"] = callback.message.message_id
    await callback.answer()
    await _render_card(
        bot,
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        text=result.reply,
        step=result.next_step,
        data=result.data,
        display_name=callback.from_user.full_name,
        preferred_message_id=callback.message.message_id,
    )


async def _apply_value(
    *,
    bot: Bot,
    user_id: int,
    chat_id: int,
    display_name: str | None,
    username: str | None,
    value: str,
    notifier,
    preferred_message_id: int | None = None,
) -> None:
    async with SessionLocal() as session:
        user = await upsert_user(
            session,
            platform="telegram",
            platform_user_id=user_id,
            chat_id=chat_id,
            username=username,
            display_name=display_name,
        )
        conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=user_id)
        log.info("Telegram wizard input user_id=%s step=%s", user_id, conv.step)
        log.debug("Telegram wizard raw input user_id=%s value=%r", user_id, value)

        if conv.step == "review":
            return
        previous_step = conv.step
        result = consume(conv.step, value, conv.data)
        await save_conversation(session, conv, step=result.next_step, data=result.data)
        await session.commit()

    await _render_card(
        bot,
        chat_id=chat_id,
        user_id=user_id,
        text=result.reply,
        step=result.next_step,
        data=result.data,
        display_name=display_name,
        preferred_message_id=preferred_message_id,
    )

    if result.next_step == "phone":
        await bot.send_message(
            chat_id,
            "Для звонка можете безопасно отправить номер кнопкой ниже или написать его вручную.",
            reply_markup=phone_reply_keyboard(),
        )
    elif previous_step == "phone":
        await bot.send_message(chat_id, "Номер получен ✅", reply_markup=ReplyKeyboardRemove())


async def _submit_lead(callback: CallbackQuery, notifier) -> None:
    async with SessionLocal() as session:
        user = await upsert_user(
            session,
            platform="telegram",
            platform_user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            username=callback.from_user.username,
            display_name=callback.from_user.full_name,
        )
        conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
        if conv.step != "review":
            await callback.answer("Заявка ещё не заполнена", show_alert=True)
            return
        lead = await create_lead(session, user=user, data=conv.data)
        await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
        await session.commit()

    log.info("Telegram lead submitted lead_id=%s user_id=%s", lead.id, callback.from_user.id)
    await callback.answer("Заявка отправлена ✅")
    await callback.message.edit_text(
        "✅ <b>Заявка отправлена!</b>\n\nМенеджер «Солнечных туров» свяжется с вами и предложит подходящие варианты.",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )
    await notifier.notify(format_lead(lead, user))


def create_router(settings: Settings, notifier, max_sender=None) -> Router:
    router = Router()

    def is_admin(user_id: int) -> bool:
        return user_id in settings.telegram_admin_id_list

    @router.message(Command("whoami"))
    async def whoami(message: Message) -> None:
        await message.answer(f"Telegram user_id: {message.from_user.id}\nchat_id: {message.chat.id}")

    @router.message(CommandStart())
    async def start(message: Message, bot: Bot) -> None:
        await _start_flow_from_message(message, bot)

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        await message.answer("Что хотите сделать?", reply_markup=main_menu_keyboard())

    @router.message(Command("subscribe"))
    async def subscribe(message: Message) -> None:
        await _subscribe_user(message, True)

    @router.message(Command("unsubscribe"))
    async def unsubscribe(message: Message) -> None:
        await _subscribe_user(message, False)

    @router.message(Command("stats"))
    async def show_stats(message: Message) -> None:
        if not is_admin(message.from_user.id):
            log.warning("Unauthorized Telegram /stats attempt user_id=%s", message.from_user.id)
            return
        async with SessionLocal() as session:
            s = await stats(session)
        await message.answer(f"Пользователей: {s['users']}\nЗаявок: {s['leads']}\nПодписчиков: {s['subscribers']}")

    @router.message(Command("broadcast"))
    async def broadcast(message: Message, bot: Bot) -> None:
        if not is_admin(message.from_user.id):
            log.warning("Unauthorized Telegram /broadcast attempt user_id=%s", message.from_user.id)
            return
        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            await message.answer("Использование: /broadcast текст рассылки")
            return
        async with SessionLocal() as session:
            users = await subscribed_users(session)
        sent = failed = skipped = 0
        for user in users:
            try:
                if user.platform == "telegram" and user.chat_id:
                    await bot.send_message(user.chat_id, text)
                    sent += 1
                elif user.platform == "max" and max_sender and settings.max_marketing_broadcast_enabled:
                    await max_sender.send_to_user(user.platform_user_id, text)
                    sent += 1
                else:
                    skipped += 1
            except Exception:
                failed += 1
                log.exception("Broadcast failed platform=%s user_id=%s", user.platform, user.platform_user_id)
            await asyncio.sleep(settings.broadcast_delay_seconds)
        log.info("Broadcast finished sent=%s failed=%s skipped=%s", sent, failed, skipped)
        await message.answer(f"Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}\nПропущено: {skipped}")

    @router.callback_query(F.data.startswith("menu|"))
    async def menu_callback(callback: CallbackQuery, bot: Bot) -> None:
        action = callback.data.split("|", 1)[1]
        if action == "start":
            await _start_flow_from_callback(callback, bot)
        elif action == "subscribe":
            await _subscribe_callback(callback, True)
        elif action == "unsubscribe":
            await _subscribe_callback(callback, False)
        elif action == "contacts":
            await callback.answer()
            await callback.message.edit_text(AGENCY_CONTACTS, reply_markup=main_menu_keyboard())

    @router.callback_query(F.data.startswith("wiz|"))
    async def wizard_callback(callback: CallbackQuery, bot: Bot) -> None:
        parts = callback.data.split("|")
        action = parts[1]

        async with SessionLocal() as session:
            conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
            step = conv.step
            data = dict(conv.data or {})
            data["_ui_message_id"] = callback.message.message_id

            if action == "set" and len(parts) >= 3:
                value = resolve_choice(step, parts[2], data, display_name=callback.from_user.full_name)
                if value is None:
                    await callback.answer("Этот вариант уже неактуален", show_alert=True)
                    return
                result = consume(step, value, data)
            elif action == "back":
                result = go_back(step, data)
            elif action == "review":
                data.pop("_editing", None)
                result = type("R", (), {"reply": render_review(data), "next_step": "review", "data": data})()
            elif action == "edit":
                await save_conversation(session, conv, step="review", data=data)
                await session.commit()
                await callback.answer()
                await callback.message.edit_text(
                    "✏️ <b>Что хотите изменить?</b>\n\nПосле изменения бот вернёт вас к проверке заявки.",
                    reply_markup=edit_menu_keyboard(),
                    parse_mode="HTML",
                )
                return
            elif action == "editstep" and len(parts) >= 3:
                result = edit_step(data, parts[2])
            elif action == "manual":
                await callback.answer("Напишите свой вариант обычным сообщением")
                await callback.message.edit_text(
                    render_step(step, data) + "\n\n✍️ <i>Напишите ответ обычным сообщением.</i>",
                    reply_markup=wizard_keyboard(step, data, display_name=callback.from_user.full_name),
                    parse_mode="HTML",
                )
                return
            elif action == "cancel":
                await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
                await session.commit()
                await callback.answer("Подбор отменён")
                await callback.message.edit_text("Подбор тура отменён. Можно начать заново в любой момент.", reply_markup=main_menu_keyboard())
                return
            elif action == "submit":
                await session.rollback()
                await _submit_lead(callback, notifier)
                return
            else:
                await callback.answer()
                return

            await save_conversation(session, conv, step=result.next_step, data=result.data)
            await session.commit()

        await callback.answer()
        await _render_card(
            bot,
            chat_id=callback.message.chat.id,
            user_id=callback.from_user.id,
            text=result.reply,
            step=result.next_step,
            data=result.data,
            display_name=callback.from_user.full_name,
            preferred_message_id=callback.message.message_id,
        )
        if result.next_step == "phone":
            await callback.message.answer(
                "Для звонка отправьте номер кнопкой ниже или введите вручную.",
                reply_markup=phone_reply_keyboard(),
            )

    @router.message(F.contact)
    async def contact_handler(message: Message, bot: Bot) -> None:
        async with SessionLocal() as session:
            conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
            if conv.step != "phone":
                log.debug("Telegram contact ignored outside phone step user_id=%s", message.from_user.id)
                return
        if message.contact.user_id is not None and message.contact.user_id != message.from_user.id:
            log.warning("Telegram foreign contact rejected user_id=%s", message.from_user.id)
            await message.answer("Пожалуйста, отправьте свой номер или введите его вручную.")
            return
        phone_number = message.contact.phone_number
        await _safe_delete_user_message(message)
        await _apply_value(
            bot=bot,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            display_name=message.from_user.full_name,
            username=message.from_user.username,
            value=phone_number,
            notifier=notifier,
        )

    @router.message(F.text)
    async def text_handler(message: Message, bot: Bot) -> None:
        if message.text.startswith("/"):
            return
        async with SessionLocal() as session:
            conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
            if conv.step == "review":
                await message.answer("Используйте кнопки под заявкой: отправить или изменить параметры.")
                return
        value = message.text.strip()
        await _safe_delete_user_message(message)
        await _apply_value(
            bot=bot,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            display_name=message.from_user.full_name,
            username=message.from_user.username,
            value=value,
            notifier=notifier,
        )

    return router


async def run_telegram(settings: Settings, bot: Bot, notifier, max_sender=None) -> None:
    if not settings.telegram_token or not settings.telegram_enable_polling:
        log.warning("Telegram disabled: token missing or polling disabled")
        return
    retry_delay = 3
    while True:
        dp = Dispatcher()
        dp.include_router(create_router(settings, notifier=notifier, max_sender=max_sender))
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("Telegram polling started proxy=%s", "enabled" if settings.telegram_proxy else "disabled")
            await dp.start_polling(bot, close_bot_session=False)
        except asyncio.CancelledError:
            raise
        except TelegramNetworkError:
            log.warning("Telegram network error; retrying in %ss", retry_delay, exc_info=True)
            await asyncio.sleep(retry_delay)
        except Exception:
            log.exception("Telegram polling crashed; retrying in %ss", retry_delay)
            await asyncio.sleep(retry_delay)
