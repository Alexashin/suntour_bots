import asyncio
import logging
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    FSInputFile,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.config import Settings
from app.content import AGENCY_CONTACTS, SUBSCRIBE_TEXT, UNSUBSCRIBE_TEXT
from app.db import SessionLocal
from app.scenario import (
    MAIN_ACTION_CONTACTS,
    MAIN_ACTION_START,
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
    LEAD_NEW,
    LEAD_PROCESSED,
    arm_wizard_reminder,
    cancel_broadcast,
    create_broadcast,
    create_lead,
    get_broadcast,
    get_or_create_conversation,
    get_user,
    log_user_event,
    queue_broadcast,
    recent_new_leads,
    reset_conversation,
    save_conversation,
    set_lead_status,
    set_subscription,
    stats,
    update_broadcast_link,
    upsert_user,
)

log = logging.getLogger(__name__)


async def _safe_delete_user_message(message: Message) -> None:
    try:
        await message.delete()
        log.debug("Telegram user message deleted user_id=%s message_id=%s", message.from_user.id if message.from_user else None, message.message_id)
    except TelegramBadRequest as exc:
        log.debug("Telegram user message not deleted message_id=%s reason=%s", message.message_id, exc)
    except Exception:
        log.debug("Telegram user message cleanup failed message_id=%s", message.message_id, exc_info=True)


def _ikb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard(*, subscribed: bool = False, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=MAIN_ACTION_START, callback_data="menu|start")]]
    if subscribed:
        rows.append([InlineKeyboardButton(text="🔕 Отписаться от рассылки", callback_data="menu|unsubscribe")])
    else:
        rows.append([InlineKeyboardButton(text="🔥 Подписаться на рассылку", callback_data="menu|subscribe")])
    rows.append([InlineKeyboardButton(text=MAIN_ACTION_CONTACTS, callback_data="menu|contacts")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin|home")])
    return _ikb(rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    return _ikb([
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin|stats"), InlineKeyboardButton(text="🆕 Новые заявки", callback_data="admin|leads")],
        [InlineKeyboardButton(text="📣 Создать рассылку", callback_data="admin|broadcast")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu|home")],
    ])


def broadcast_target_keyboard() -> InlineKeyboardMarkup:
    return _ikb([
        [InlineKeyboardButton(text="Telegram", callback_data="admin|btarget|telegram"), InlineKeyboardButton(text="MAX", callback_data="admin|btarget|max")],
        [InlineKeyboardButton(text="Оба мессенджера", callback_data="admin|btarget|both")],
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin|home")],
    ])


def broadcast_preview_keyboard(broadcast_id: int, *, url: str | None = None, button_text: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text=button_text or "Подробнее", url=url)])
    rows.extend([
        [InlineKeyboardButton(text="🚀 Запустить рассылку", callback_data=f"broadcast|send|{broadcast_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"broadcast|cancel|{broadcast_id}")],
    ])
    return _ikb(rows)


def broadcast_link_keyboard(broadcast_id: int) -> InlineKeyboardMarkup:
    return _ikb([
        [InlineKeyboardButton(text="🔗 Добавить кнопку-ссылку", callback_data=f"broadcast|addlink|{broadcast_id}")],
        [InlineKeyboardButton(text="Без ссылки", callback_data=f"broadcast|nolink|{broadcast_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"broadcast|cancel|{broadcast_id}")],
    ])


def broadcast_button_text_keyboard(broadcast_id: int) -> InlineKeyboardMarkup:
    return _ikb([
        [InlineKeyboardButton(text="Оставить «Подробнее»", callback_data=f"broadcast|defaulttext|{broadcast_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"broadcast|cancel|{broadcast_id}")],
    ])


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


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
    if step not in {"children_ages", "phone"} and not any(button.callback_data == "wiz|manual" for row in rows for button in row):
        rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="wiz|manual")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="wiz|back"), InlineKeyboardButton(text="❌ Отменить", callback_data="wiz|cancel")])
    return _ikb(rows)


def edit_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=c.text, callback_data=f"wiz|editstep|{c.value}") for c in row] for row in review_edit_rows()]
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
    u = message.from_user
    return await upsert_user(
        session, platform="telegram", platform_user_id=u.id, chat_id=message.chat.id,
        username=u.username, display_name=u.full_name, first_name=u.first_name, last_name=u.last_name,
        language_code=u.language_code, is_premium=getattr(u, "is_premium", None),
        profile_meta={"is_bot": bool(u.is_bot)},
    )


async def _menu_state(user_id: int, settings: Settings) -> tuple[bool, bool]:
    async with SessionLocal() as session:
        user = await get_user(session, platform="telegram", platform_user_id=user_id)
        subscribed = bool(user and user.subscribed)
    return subscribed, user_id in settings.telegram_admin_id_list


async def _show_menu_message(message: Message, settings: Settings) -> None:
    async with SessionLocal() as session:
        user = await _upsert_tg_user(session, message)
        await reset_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
        await session.commit()
        subscribed = user.subscribed
    await message.answer(
        "🌴 <b>Солнечные туры</b>\n\nВыберите, что хотите сделать:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(subscribed=subscribed, is_admin=message.from_user.id in settings.telegram_admin_id_list),
    )


async def _replace_with_text(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str = "HTML",
) -> Message:
    """Replace the current UI message with a text card.

    Text messages are edited in place. Media messages cannot be converted into
    ordinary text with edit_text(), so we remove them best-effort and send a new
    message instead. This keeps admin navigation working after rich-media
    broadcast previews/results.
    """
    if message.text is not None:
        try:
            return await message.edit_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return message
            log.debug(
                "Telegram text edit failed message_id=%s reason=%s",
                message.message_id,
                exc,
            )

    try:
        await message.delete()
    except TelegramBadRequest as exc:
        log.debug(
            "Telegram old UI message not deleted message_id=%s reason=%s",
            message.message_id,
            exc,
        )
    except Exception:
        log.warning(
            "Telegram old UI message cleanup failed message_id=%s",
            message.message_id,
            exc_info=True,
        )

    return await message.answer(
        text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


async def _show_menu_callback(callback: CallbackQuery, settings: Settings) -> None:
    async with SessionLocal() as session:
        await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
        await session.commit()
    subscribed, admin = await _menu_state(callback.from_user.id, settings)
    await callback.answer()
    await _replace_with_text(
        callback.message,
        "🌴 <b>Солнечные туры</b>\n\nВыберите, что хотите сделать:",
        reply_markup=main_menu_keyboard(subscribed=subscribed, is_admin=admin),
    )


async def _set_ui_id(platform_user_id: int, message_id: int) -> None:
    async with SessionLocal() as session:
        conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=platform_user_id)
        data = dict(conv.data or {})
        data["_ui_message_id"] = message_id
        await save_conversation(session, conv, step=conv.step, data=data)
        await session.commit()


async def _render_card(bot: Bot, *, chat_id: int, user_id: int, text: str, step: str, data: dict,
                       display_name: str | None, preferred_message_id: int | None = None) -> int:
    markup = wizard_keyboard(step, data, display_name=display_name)
    message_id = preferred_message_id or data.get("_ui_message_id")
    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=int(message_id), text=text, reply_markup=markup, parse_mode="HTML")
            await _set_ui_id(user_id, int(message_id))
            return int(message_id)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return int(message_id)
            log.debug("Telegram wizard card edit failed; sending new card", exc_info=True)
    sent = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    await _set_ui_id(user_id, sent.message_id)
    return sent.message_id


async def _subscribe_user(message: Message, enabled: bool, settings: Settings) -> None:
    async with SessionLocal() as session:
        user = await _upsert_tg_user(session, message)
        await set_subscription(session, user, enabled=enabled)
        await session.commit()
    log.info("Telegram subscription changed user_id=%s enabled=%s", message.from_user.id, enabled)
    await message.answer(SUBSCRIBE_TEXT if enabled else UNSUBSCRIBE_TEXT,
                         reply_markup=main_menu_keyboard(subscribed=enabled, is_admin=message.from_user.id in settings.telegram_admin_id_list))


async def _subscribe_callback(callback: CallbackQuery, enabled: bool, settings: Settings) -> None:
    msg = callback.message
    async with SessionLocal() as session:
        user = await upsert_user(session, platform="telegram", platform_user_id=callback.from_user.id, chat_id=msg.chat.id,
                                 username=callback.from_user.username, display_name=callback.from_user.full_name)
        await set_subscription(session, user, enabled=enabled)
        await session.commit()
    await callback.answer("Подписка включена" if enabled else "Рассылка отключена")
    await msg.edit_text(SUBSCRIBE_TEXT if enabled else UNSUBSCRIBE_TEXT,
                        reply_markup=main_menu_keyboard(subscribed=enabled, is_admin=callback.from_user.id in settings.telegram_admin_id_list))


async def _start_flow_from_callback(callback: CallbackQuery, bot: Bot) -> None:
    result = start_scenario()
    async with SessionLocal() as session:
        user = await upsert_user(
            session, platform="telegram", platform_user_id=callback.from_user.id, chat_id=callback.message.chat.id,
            username=callback.from_user.username, display_name=callback.from_user.full_name,
            first_name=callback.from_user.first_name, last_name=callback.from_user.last_name,
            language_code=callback.from_user.language_code, is_premium=getattr(callback.from_user, "is_premium", None),
        )
        # Подбор тура всегда включает рассылку. Повторный подбор безопасен:
        # set_subscription идемпотентен и не создаёт повторных событий.
        await set_subscription(session, user, enabled=True)
        await log_user_event(session, user, "wizard_started")
        conv = await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
        result.data["_ui_message_id"] = callback.message.message_id
        await save_conversation(session, conv, step=result.next_step, data=result.data)
        await arm_wizard_reminder(session, conv)
        await session.commit()
    await callback.answer()
    await _render_card(
        bot, chat_id=callback.message.chat.id, user_id=callback.from_user.id, text=result.reply,
        step=result.next_step, data=result.data, display_name=callback.from_user.full_name,
        preferred_message_id=callback.message.message_id,
    )


async def _show_lead_sent(
    bot: Bot, *, chat_id: int, user_id: int, settings: Settings, subscribed: bool,
    preferred_message_id: int | None = None, remove_phone_keyboard: bool = False,
) -> None:
    if remove_phone_keyboard:
        await bot.send_message(chat_id, "Номер получен ✅", reply_markup=ReplyKeyboardRemove())
    text = (
        "✅ <b>Заявка отправлена!</b>\n\n"
        "Менеджер «Солнечных туров» свяжется с вами и предложит подходящие варианты."
    )
    markup = main_menu_keyboard(subscribed=subscribed, is_admin=user_id in settings.telegram_admin_id_list)
    if preferred_message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=int(preferred_message_id), text=text,
                reply_markup=markup, parse_mode="HTML",
            )
            return
        except TelegramBadRequest:
            log.debug("Telegram completion card edit failed; sending new card", exc_info=True)
    await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


async def _apply_value(
    *, bot: Bot, notifier, settings: Settings, user_id: int, chat_id: int,
    display_name: str | None, username: str | None, value: str,
    preferred_message_id: int | None = None,
) -> None:
    lead_id: int | None = None
    subscribed = True
    previous_step = ""
    ui_mid = preferred_message_id
    async with SessionLocal() as session:
        user = await upsert_user(
            session, platform="telegram", platform_user_id=user_id, chat_id=chat_id,
            username=username, display_name=display_name,
        )
        conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=user_id)
        log.info("Telegram wizard input user_id=%s step=%s", user_id, conv.step)
        log.debug("Telegram wizard raw input user_id=%s value=%r", user_id, value)
        if conv.step in {"idle", "complete"}:
            return
        if conv.step == "review":
            return
        previous_step = conv.step
        ui_mid = ui_mid or (conv.data or {}).get("_ui_message_id")
        await log_user_event(session, user, "wizard_answer", {"step": previous_step, "value": value})
        result = consume(conv.step, value, conv.data)
        if result.next_step == "complete":
            lead = await create_lead(session, user=user, data=result.data)
            lead_id = lead.id
            subscribed = bool(user.subscribed)
            await reset_conversation(session, platform="telegram", platform_user_id=user_id)
        else:
            await save_conversation(session, conv, step=result.next_step, data=result.data)
            await arm_wizard_reminder(session, conv)
        await session.commit()

    if lead_id is not None:
        log.info("Telegram lead auto-submitted lead_id=%s user_id=%s", lead_id, user_id)
        await _show_lead_sent(
            bot, chat_id=chat_id, user_id=user_id, settings=settings, subscribed=subscribed,
            preferred_message_id=ui_mid, remove_phone_keyboard=previous_step == "phone",
        )
        await notifier.notify_lead(lead_id)
        return

    await _render_card(
        bot, chat_id=chat_id, user_id=user_id, text=result.reply, step=result.next_step,
        data=result.data, display_name=display_name, preferred_message_id=preferred_message_id,
    )
    if result.next_step == "phone":
        await bot.send_message(
            chat_id, "Для звонка можете отправить номер кнопкой ниже или написать его вручную.",
            reply_markup=phone_reply_keyboard(),
        )


async def _submit_lead(callback: CallbackQuery, notifier, settings: Settings) -> None:
    async with SessionLocal() as session:
        user = await upsert_user(session, platform="telegram", platform_user_id=callback.from_user.id, chat_id=callback.message.chat.id,
                                 username=callback.from_user.username, display_name=callback.from_user.full_name)
        conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
        if conv.step != "review":
            await callback.answer("Заявка ещё не заполнена", show_alert=True)
            return
        lead = await create_lead(session, user=user, data=conv.data)
        subscribed = user.subscribed
        await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
        await session.commit()
        lead_id = lead.id
    log.info("Telegram lead submitted lead_id=%s user_id=%s", lead_id, callback.from_user.id)
    await callback.answer("Заявка отправлена ✅")
    await callback.message.edit_text(
        "✅ <b>Заявка отправлена!</b>\n\nМенеджер «Солнечных туров» свяжется с вами и предложит подходящие варианты.",
        reply_markup=main_menu_keyboard(subscribed=subscribed, is_admin=callback.from_user.id in settings.telegram_admin_id_list), parse_mode="HTML")
    await notifier.notify_lead(lead_id)


def _admin_stats_text(s: dict[str, int]) -> str:
    return (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b>\n"
        f"🔥 Заявок всего: <b>{s['leads']}</b>\n"
        f"🆕 Новых заявок: <b>{s['new_leads']}</b>\n"
        f"✅ Обработано: <b>{s['processed_leads']}</b>\n\n"
        f"📣 Подписчиков: <b>{s['subscribers']}</b>\n"
        f"• Telegram: {s['tg_subscribers']}\n• MAX: {s['max_subscribers']}"
    )


async def _send_broadcast_preview(bot: Bot, chat_id: int, item) -> None:
    audience = {"telegram": "Telegram", "max": "MAX", "both": "Telegram + MAX"}.get(item.target, item.target)
    header = f"📣 <b>Предпросмотр рассылки #{item.id}</b>\nАудитория: <b>{escape(audience)}</b>\n\n"
    caption = (item.text or "").strip()
    keyboard = broadcast_preview_keyboard(item.id, url=item.button_url, button_text=item.button_text)
    if item.media_type == "image" and item.media_path:
        await bot.send_photo(chat_id, FSInputFile(item.media_path), caption=header + escape(caption), parse_mode="HTML", reply_markup=keyboard)
    elif item.media_type == "video" and item.media_path:
        await bot.send_video(chat_id, FSInputFile(item.media_path), caption=header + escape(caption), parse_mode="HTML", reply_markup=keyboard)
    else:
        await bot.send_message(chat_id, header + (escape(caption) if caption else "<i>Без текста</i>"), parse_mode="HTML", reply_markup=keyboard)


async def _store_tg_broadcast_content(message: Message, bot: Bot, settings: Settings, target: str, ui_mid: int | None):
    text = (message.caption or message.text or "").strip()
    media_type = None
    media_path = None
    media_source_id = None
    if message.photo:
        media_type = "image"
        media_source_id = message.photo[-1].file_id
        ext = ".jpg"
    elif message.video:
        media_type = "video"
        media_source_id = message.video.file_id
        ext = ".mp4"
    else:
        ext = ""
    if not text and not media_type:
        return None
    if media_type:
        root = Path(settings.media_storage_dir)
        root.mkdir(parents=True, exist_ok=True)
        media_path = str(root / f"broadcast_tg_{message.from_user.id}_{message.message_id}{ext}")
        await bot.download(media_source_id, destination=media_path)
    async with SessionLocal() as session:
        item = await create_broadcast(
            session, platform="telegram", user_id=message.from_user.id, name=message.from_user.full_name, target=target,
            text=text, media_type=media_type, media_path=media_path, media_source_platform="telegram" if media_type else None,
            media_source_id=media_source_id,
        )
        conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
        conv.step = "admin_broadcast"
        conv.data = {"_admin_mode": "broadcast_link_choice", "_broadcast_id": item.id, "_ui_message_id": ui_mid}
        await session.commit()
        return item


def create_router(settings: Settings, notifier, max_sender=None) -> Router:
    router = Router()
    is_admin = lambda user_id: user_id in settings.telegram_admin_id_list

    @router.message(Command("whoami"))
    async def whoami(message: Message) -> None:
        await message.answer(f"Telegram user_id: {message.from_user.id}\nchat_id: {message.chat.id}")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await _show_menu_message(message, settings)

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        await _show_menu_message(message, settings)

    @router.message(Command("admin"))
    async def admin_command(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        await message.answer("🛠 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_keyboard())

    @router.message(Command("subscribe"))
    async def subscribe(message: Message) -> None:
        await _subscribe_user(message, True, settings)

    @router.message(Command("unsubscribe"))
    async def unsubscribe(message: Message) -> None:
        await _subscribe_user(message, False, settings)

    @router.callback_query(F.data.startswith("menu|"))
    async def menu_callback(callback: CallbackQuery, bot: Bot) -> None:
        action = callback.data.split("|", 1)[1]
        if action == "home":
            await _show_menu_callback(callback, settings)
        elif action == "start":
            await _start_flow_from_callback(callback, bot)
        elif action == "subscribe":
            await _subscribe_callback(callback, True, settings)
        elif action == "unsubscribe":
            await _subscribe_callback(callback, False, settings)
        elif action == "contacts":
            subscribed, admin = await _menu_state(callback.from_user.id, settings)
            await callback.answer()
            await callback.message.edit_text(AGENCY_CONTACTS, reply_markup=main_menu_keyboard(subscribed=subscribed, is_admin=admin))

    @router.callback_query(F.data.startswith("admin|"))
    async def admin_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        parts = callback.data.split("|")
        action = parts[1]
        if action == "home":
            await callback.answer()
            await callback.message.edit_text("🛠 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_keyboard())
        elif action == "stats":
            async with SessionLocal() as session:
                s = await stats(session)
            await callback.answer()
            await callback.message.edit_text(_admin_stats_text(s), parse_mode="HTML", reply_markup=admin_keyboard())
        elif action == "leads":
            async with SessionLocal() as session:
                leads = await recent_new_leads(session, 10)
            if leads:
                lines = ["🆕 <b>Последние новые заявки</b>\n"] + [f"#{x.id} · {escape(x.client_name)} · {escape(x.destination)}" for x in leads]
                text = "\n".join(lines)
            else:
                text = "🆕 Новых заявок сейчас нет."
            await callback.answer()
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_keyboard())
        elif action == "broadcast":
            await callback.answer()
            flag = "включена" if settings.max_marketing_broadcast_enabled else "выключена"
            await callback.message.edit_text(
                f"📣 <b>Новая рассылка</b>\n\nКуда отправляем?\nMAX-рассылка сейчас: <b>{flag}</b>.",
                parse_mode="HTML", reply_markup=broadcast_target_keyboard())
        elif action == "btarget" and len(parts) >= 3:
            target = parts[2]
            async with SessionLocal() as session:
                conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
                conv.step = "admin_broadcast"
                conv.data = {"_admin_mode": "broadcast_content", "_admin_target": target, "_ui_message_id": callback.message.message_id}
                await session.commit()
            await callback.answer("Теперь отправьте сообщение")
            await callback.message.edit_text(
                "✍️ <b>Отправьте содержимое рассылки одним сообщением.</b>\n\n"
                "Можно отправить:\n• текст;\n• фото + подпись;\n• видео + подпись.\n\n"
                "Следующим шагом предложу добавить красивую кнопку-ссылку.",
                parse_mode="HTML", reply_markup=_ikb([[InlineKeyboardButton(text="❌ Отменить", callback_data="admin|home")]]))

    @router.callback_query(F.data.startswith("broadcast|"))
    async def broadcast_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        _, action, raw_id = callback.data.split("|", 2)
        bid = int(raw_id)
        if action == "cancel":
            async with SessionLocal() as session:
                item = await get_broadcast(session, bid)
                if item:
                    await cancel_broadcast(session, item)
                    await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
                    await session.commit()
            await callback.answer("Рассылка отменена")
            try:
                await callback.message.edit_text("Рассылка отменена.", reply_markup=admin_keyboard())
            except TelegramBadRequest:
                await callback.message.answer("Рассылка отменена.", reply_markup=admin_keyboard())
            return

        if action == "addlink":
            async with SessionLocal() as session:
                conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
                conv.step = "admin_broadcast"
                conv.data = {"_admin_mode": "broadcast_link_url", "_broadcast_id": bid, "_ui_message_id": callback.message.message_id}
                await session.commit()
            await callback.answer()
            await callback.message.edit_text("🔗 <b>Пришлите ссылку</b> обычным сообщением.\n\nНапример: <code>https://suntur36.ru/</code>", parse_mode="HTML")
            return

        if action == "nolink":
            async with SessionLocal() as session:
                item = await get_broadcast(session, bid)
                await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
                await session.commit()
            await callback.answer()
            if item:
                await _send_broadcast_preview(bot, callback.message.chat.id, item)
            return

        if action == "defaulttext":
            async with SessionLocal() as session:
                item = await get_broadcast(session, bid)
                if item and item.button_url:
                    await update_broadcast_link(session, item, url=item.button_url, text="Подробнее")
                await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
                await session.commit()
            await callback.answer()
            if item:
                await _send_broadcast_preview(bot, callback.message.chat.id, item)
            return

        if action == "send":
            async with SessionLocal() as session:
                item = await get_broadcast(session, bid)
                if not item:
                    await callback.answer("Рассылка не найдена", show_alert=True)
                    return
                try:
                    await queue_broadcast(session, item)
                except ValueError as exc:
                    await callback.answer(str(exc), show_alert=True)
                    return
                await session.commit()
                total = item.total_recipient_count
            await callback.answer("Рассылка поставлена в очередь ✅")
            text = (
                f"🚀 <b>Рассылка #{bid} поставлена в очередь</b>\n\n"
                f"Получателей: {total}\nБот продолжает работать во время отправки."
            )
            try:
                if callback.message.photo or callback.message.video:
                    await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=admin_keyboard())
                else:
                    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_keyboard())
            except Exception:
                await callback.message.answer(text, parse_mode="HTML", reply_markup=admin_keyboard())

    @router.callback_query(F.data.startswith("lead|"))
    async def lead_status_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        _, action, raw_id = callback.data.split("|", 2)
        status = LEAD_PROCESSED if action == "processed" else LEAD_NEW
        async with SessionLocal() as session:
            lead = await set_lead_status(
                session, lead_id=int(raw_id), status=status, actor_platform="telegram",
                actor_user_id=callback.from_user.id, actor_name=callback.from_user.full_name,
            )
            if not lead:
                await callback.answer("Заявка не найдена", show_alert=True)
                return
            await session.commit()
        await callback.answer("Заявка обработана ✅" if status == LEAD_PROCESSED else "Заявка возвращена в новые")
        await notifier.sync_lead(int(raw_id))

    @router.callback_query(F.data.startswith("wiz|"))
    async def wizard_callback(callback: CallbackQuery, bot: Bot) -> None:
        parts = callback.data.split("|")
        action = parts[1]
        lead_id: int | None = None
        subscribed = True

        async with SessionLocal() as session:
            user = await upsert_user(
                session, platform="telegram", platform_user_id=callback.from_user.id, chat_id=callback.message.chat.id,
                username=callback.from_user.username, display_name=callback.from_user.full_name,
                first_name=callback.from_user.first_name, last_name=callback.from_user.last_name,
                language_code=callback.from_user.language_code, is_premium=getattr(callback.from_user, "is_premium", None),
            )
            conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
            step = conv.step
            data = dict(conv.data or {})
            data["_ui_message_id"] = callback.message.message_id

            if action == "resume":
                if step in {"idle", "complete", "admin_broadcast"}:
                    await session.commit()
                    await callback.answer("Подбор уже завершён")
                    subscribed_now, admin = await _menu_state(callback.from_user.id, settings)
                    await _replace_with_text(
                        callback.message,
                        "🌴 <b>Солнечные туры</b>\n\nВыберите, что хотите сделать:",
                        reply_markup=main_menu_keyboard(subscribed=subscribed_now, is_admin=admin),
                    )
                    return
                await save_conversation(session, conv, step=step, data=data)
                await arm_wizard_reminder(session, conv)
                await session.commit()
                await callback.answer()
                text = render_review(data) if step == "review" else render_step(step, data)
                await _render_card(
                    bot, chat_id=callback.message.chat.id, user_id=callback.from_user.id, text=text,
                    step=step, data=data, display_name=callback.from_user.full_name,
                    preferred_message_id=callback.message.message_id,
                )
                if step == "phone":
                    await callback.message.answer(
                        "Для звонка отправьте номер кнопкой ниже или введите вручную.",
                        reply_markup=phone_reply_keyboard(),
                    )
                return

            if action == "set" and len(parts) >= 3:
                value = resolve_choice(step, parts[2], data, display_name=callback.from_user.full_name)
                if value is None:
                    await callback.answer("Этот вариант уже неактуален", show_alert=True)
                    return
                await log_user_event(session, user, "wizard_answer", {"step": step, "value": value})
                result = consume(step, value, data)
            elif action == "back":
                result = go_back(step, data)
            elif action == "review":
                data.pop("_editing", None)
                result = type("R", (), {"reply": render_review(data), "next_step": "review", "data": data})()
            elif action == "edit":
                await save_conversation(session, conv, step="review", data=data)
                await arm_wizard_reminder(session, conv)
                await session.commit()
                await callback.answer()
                await callback.message.edit_text(
                    "✏️ <b>Что хотите изменить?</b>\n\nПосле изменения бот вернёт вас к проверке заявки.",
                    reply_markup=edit_menu_keyboard(), parse_mode="HTML",
                )
                return
            elif action == "editstep" and len(parts) >= 3:
                result = edit_step(data, parts[2])
            elif action == "manual":
                await save_conversation(session, conv, step=step, data=data)
                await arm_wizard_reminder(session, conv)
                await session.commit()
                await callback.answer("Напишите свой вариант обычным сообщением")
                await callback.message.edit_text(
                    render_step(step, data) + "\n\n✍️ <i>Напишите ответ обычным сообщением.</i>",
                    reply_markup=wizard_keyboard(step, data, display_name=callback.from_user.full_name), parse_mode="HTML",
                )
                return
            elif action == "cancel":
                await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
                await session.commit()
                await callback.answer("Подбор отменён")
                subscribed_now, admin = await _menu_state(callback.from_user.id, settings)
                await callback.message.edit_text(
                    "Подбор тура отменён.",
                    reply_markup=main_menu_keyboard(subscribed=subscribed_now, is_admin=admin),
                )
                return
            elif action == "submit":
                # Совместимость с заявками, которые дошли до review в старой версии.
                await session.rollback()
                await _submit_lead(callback, notifier, settings)
                return
            else:
                await callback.answer()
                return

            if result.next_step == "complete":
                lead = await create_lead(session, user=user, data=result.data)
                lead_id = lead.id
                subscribed = bool(user.subscribed)
                await reset_conversation(session, platform="telegram", platform_user_id=callback.from_user.id)
            else:
                await save_conversation(session, conv, step=result.next_step, data=result.data)
                await arm_wizard_reminder(session, conv)
            await session.commit()

        if lead_id is not None:
            log.info("Telegram lead auto-submitted lead_id=%s user_id=%s", lead_id, callback.from_user.id)
            await callback.answer("Заявка отправлена ✅")
            await _show_lead_sent(
                bot, chat_id=callback.message.chat.id, user_id=callback.from_user.id, settings=settings,
                subscribed=subscribed, preferred_message_id=callback.message.message_id,
            )
            await notifier.notify_lead(lead_id)
            return

        await callback.answer()
        await _render_card(
            bot, chat_id=callback.message.chat.id, user_id=callback.from_user.id, text=result.reply,
            step=result.next_step, data=result.data, display_name=callback.from_user.full_name,
            preferred_message_id=callback.message.message_id,
        )
        if result.next_step == "phone":
            await callback.message.answer(
                "Для звонка отправьте номер кнопкой ниже или введите вручную.",
                reply_markup=phone_reply_keyboard(),
            )

    @router.message(F.photo | F.video)
    async def broadcast_media_handler(message: Message, bot: Bot) -> None:
        if not is_admin(message.from_user.id):
            return
        async with SessionLocal() as session:
            conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
            data = dict(conv.data or {})
        if conv.step != "admin_broadcast" or data.get("_admin_mode") != "broadcast_content":
            return
        item = await _store_tg_broadcast_content(
            message, bot, settings, str(data.get("_admin_target", "both")), data.get("_ui_message_id")
        )
        if not item:
            return
        await _safe_delete_user_message(message)
        ui_mid = data.get("_ui_message_id")
        text = "🔗 <b>Добавить кнопку-ссылку?</b>\n\nОна будет отображаться прямо под сообщением рассылки."
        if ui_mid:
            try:
                await bot.edit_message_text(chat_id=message.chat.id, message_id=int(ui_mid), text=text, parse_mode="HTML", reply_markup=broadcast_link_keyboard(item.id))
                return
            except Exception:
                pass
        await message.answer(text, parse_mode="HTML", reply_markup=broadcast_link_keyboard(item.id))

    @router.message(F.contact)
    async def contact_handler(message: Message, bot: Bot) -> None:
        async with SessionLocal() as session:
            conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
            if conv.step != "phone": return
        if message.contact.user_id is not None and message.contact.user_id != message.from_user.id:
            await message.answer("Пожалуйста, отправьте свой номер или введите его вручную."); return
        await _safe_delete_user_message(message)
        await _apply_value(
            bot=bot, notifier=notifier, settings=settings, user_id=message.from_user.id, chat_id=message.chat.id,
            display_name=message.from_user.full_name, username=message.from_user.username,
            value=message.contact.phone_number,
        )

    @router.message(F.text)
    async def text_handler(message: Message, bot: Bot) -> None:
        if message.text.startswith("/"): return
        async with SessionLocal() as session:
            conv = await get_or_create_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
            data = dict(conv.data or {})
            if is_admin(message.from_user.id) and conv.step == "admin_broadcast":
                mode = data.get("_admin_mode")
                if mode == "broadcast_content":
                    target = str(data.get("_admin_target", "both"))
                    ui_mid = data.get("_ui_message_id")
                    # Commit/close this session before the helper opens its own transaction.
                    await session.commit()
                    item = await _store_tg_broadcast_content(message, bot, settings, target, ui_mid)
                    if not item:
                        await message.answer("Пришлите текст, фото или видео.")
                        return
                    await _safe_delete_user_message(message)
                    prompt = "🔗 <b>Добавить кнопку-ссылку?</b>\n\nОна будет отображаться прямо под сообщением рассылки."
                    if ui_mid:
                        try:
                            await bot.edit_message_text(chat_id=message.chat.id, message_id=int(ui_mid), text=prompt, parse_mode="HTML", reply_markup=broadcast_link_keyboard(item.id))
                            return
                        except Exception:
                            pass
                    await message.answer(prompt, parse_mode="HTML", reply_markup=broadcast_link_keyboard(item.id))
                    return
                if mode == "broadcast_link_url":
                    bid = int(data.get("_broadcast_id"))
                    value = message.text.strip()
                    if not _valid_http_url(value):
                        await message.answer("Ссылка должна начинаться с http:// или https://. Попробуйте ещё раз.")
                        return
                    item = await get_broadcast(session, bid)
                    if not item:
                        await message.answer("Черновик рассылки не найден.")
                        return
                    await update_broadcast_link(session, item, url=value, text="Подробнее")
                    conv.data = {"_admin_mode": "broadcast_button_text", "_broadcast_id": bid, "_ui_message_id": data.get("_ui_message_id")}
                    await session.commit()
                    await _safe_delete_user_message(message)
                    await message.answer(
                        "✏️ <b>Как назвать кнопку?</b>\n\nНапишите свой текст или оставьте «Подробнее».",
                        parse_mode="HTML", reply_markup=broadcast_button_text_keyboard(bid),
                    )
                    return
                if mode == "broadcast_button_text":
                    bid = int(data.get("_broadcast_id"))
                    item = await get_broadcast(session, bid)
                    if not item or not item.button_url:
                        await message.answer("Черновик рассылки не найден.")
                        return
                    button_text = message.text.strip()[:64] or "Подробнее"
                    await update_broadcast_link(session, item, url=item.button_url, text=button_text)
                    await reset_conversation(session, platform="telegram", platform_user_id=message.from_user.id)
                    await session.commit()
                    await _safe_delete_user_message(message)
                    await _send_broadcast_preview(bot, message.chat.id, item)
                    return
            if conv.step == "idle":
                menu_user = await get_user(session, platform="telegram", platform_user_id=message.from_user.id)
                subscribed = bool(menu_user and menu_user.subscribed)
                await message.answer("Выберите действие в меню:", reply_markup=main_menu_keyboard(subscribed=subscribed, is_admin=is_admin(message.from_user.id)))
                return
            if conv.step == "review":
                await message.answer("Используйте кнопки под заявкой: отправить или изменить параметры."); return
        value = message.text.strip()
        await _safe_delete_user_message(message)
        await _apply_value(
            bot=bot, notifier=notifier, settings=settings, user_id=message.from_user.id, chat_id=message.chat.id,
            display_name=message.from_user.full_name, username=message.from_user.username, value=value,
        )

    return router


async def run_telegram(settings: Settings, bot: Bot, notifier, max_sender=None) -> None:
    if not settings.telegram_token or not settings.telegram_enable_polling:
        log.warning("Telegram disabled: token missing or polling disabled"); return
    retry_delay = 3
    while True:
        dp = Dispatcher(); dp.include_router(create_router(settings, notifier=notifier, max_sender=max_sender))
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("Telegram polling started proxy=%s", "enabled" if settings.telegram_proxy else "disabled")
            await dp.start_polling(bot, close_bot_session=False)
        except asyncio.CancelledError: raise
        except TelegramNetworkError:
            log.warning("Telegram network error; retrying in %ss", retry_delay, exc_info=True); await asyncio.sleep(retry_delay)
        except Exception:
            log.exception("Telegram polling crashed; retrying in %ss", retry_delay); await asyncio.sleep(retry_delay)
