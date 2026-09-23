import asyncio
import base64
import hashlib
import hmac
import logging
import re
from urllib.parse import urlparse
from html import escape
from typing import Any
from datetime import datetime, timezone

import aiohttp

from app.config import Settings
from app.content import AGENCY_CONTACTS, SUBSCRIBE_TEXT, UNSUBSCRIBE_TEXT
from app.db import SessionLocal
from app.scenario import (
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


class MaxClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.max_api_base.rstrip("/")
        self.session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=45, connect=15, sock_read=35)
            self.session = aiohttp.ClientSession(headers={"Authorization": self.settings.max_token}, timeout=timeout)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        await self.open()
        assert self.session is not None
        url = f"{self.base}{path}"
        log.debug("MAX API request method=%s path=%s", method, path)
        async with self.session.request(method, url, **kwargs) as resp:
            body = await resp.text()
            log.debug("MAX API response method=%s path=%s status=%s", method, path, resp.status)
            if resp.status >= 400:
                raise RuntimeError(f"MAX {method} {path}: {resp.status} {body[:1000]}")
            if not body:
                return None
            try:
                return await resp.json()
            except Exception:
                return body

    @staticmethod
    def callback_keyboard(rows: list[list[tuple[str, str]]]) -> list[list[dict[str, Any]]]:
        return [[{"type": "callback", "text": text, "payload": payload} for text, payload in row] for row in rows]

    @staticmethod
    def phone_keyboard() -> list[list[dict[str, Any]]]:
        return [[{"type": "request_contact", "text": "📱 Поделиться номером"}]]

    @staticmethod
    def link_keyboard(text: str, url: str) -> list[list[dict[str, Any]]]:
        return [[{"type": "link", "text": text, "url": url}]]

    @staticmethod
    def _payload(text: str, keyboard: list[list[dict[str, Any]]] | None = None, *, notify: bool = True,
                 attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text, "notify": notify, "format": "html"}
        combined = list(attachments or [])
        if keyboard is not None:
            combined.append({"type": "inline_keyboard", "payload": {"buttons": keyboard}})
        if combined:
            payload["attachments"] = combined
        return payload

    async def send_to_user(self, user_id: int, text: str, *, keyboard=None, notify: bool = True, attachments=None) -> Any:
        return await self._request(
            "POST", "/messages", params={"user_id": user_id},
            json=self._payload(text, keyboard, notify=notify, attachments=attachments)
        )

    async def send_to_chat(self, chat_id: int, text: str, *, keyboard=None, notify: bool = True, attachments=None) -> Any:
        return await self._request(
            "POST", "/messages", params={"chat_id": chat_id},
            json=self._payload(text, keyboard, notify=notify, attachments=attachments)
        )

    async def edit_message(self, message_id: str, text: str, *, keyboard=None, attachments=None) -> Any:
        return await self._request(
            "PUT", "/messages", params={"message_id": message_id},
            json=self._payload(text, keyboard, notify=False, attachments=attachments)
        )

    async def upload_media(self, file_path: str, media_type: str) -> dict[str, Any]:
        """Upload a local image/video and return an attachment request for /messages."""
        info = await self._request("POST", "/uploads", params={"type": media_type})
        if not isinstance(info, dict) or not info.get("url"):
            raise RuntimeError(f"MAX upload URL missing for {media_type}")
        upload_url = str(info["url"])
        from pathlib import Path
        path = Path(file_path)
        form = aiohttp.FormData()
        fh = path.open("rb")
        form.add_field("data", fh, filename=path.name, content_type="application/octet-stream")
        try:
            # Upload URL belongs to a dedicated media host; do not leak the bot Authorization header there.
            timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_read=240)
            async with aiohttp.ClientSession(timeout=timeout) as upload_session:
                async with upload_session.post(upload_url, data=form) as resp:
                    raw = await resp.text()
                    if resp.status >= 400:
                        raise RuntimeError(f"MAX media upload failed: {resp.status} {raw[:500]}")
                    try:
                        result = await resp.json()
                    except Exception:
                        import json as _json
                        result = _json.loads(raw) if raw else {}
        finally:
            fh.close()
        token = None
        if isinstance(result, dict):
            token = result.get("token")
            retval = result.get("retval")
            if not token and isinstance(retval, dict):
                token = retval.get("token")
        token = token or info.get("token")
        if not token:
            raise RuntimeError(f"MAX media token missing for {media_type}")
        return {"type": media_type, "payload": {"token": str(token)}}

    async def answer_callback(self, callback_id: str, text: str, *, keyboard=None) -> Any:
        body = {"message": self._payload(text, keyboard, notify=False)}
        return await self._request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    async def notify_admins(self, text: str) -> int:
        delivered = 0
        for user_id in self.settings.max_admin_user_id_list:
            try:
                await self.send_to_user(user_id, text)
                delivered += 1
            except Exception:
                log.exception("Could not notify MAX admin user id=%s", user_id)
            await asyncio.sleep(self.settings.broadcast_delay_seconds)
        for chat_id in self.settings.max_admin_chat_id_list:
            try:
                await self.send_to_chat(chat_id, text)
                delivered += 1
            except Exception:
                log.exception("Could not notify MAX admin chat id=%s", chat_id)
            await asyncio.sleep(self.settings.broadcast_delay_seconds)
        return delivered

    async def get_updates(self, marker: int | None = None, timeout: int = 30) -> Any:
        params: dict[str, Any] = {"timeout": timeout, "types": "message_created,message_callback,bot_started"}
        if marker is not None:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params)

    async def create_subscription(self) -> Any:
        if not self.settings.max_webhook_url:
            raise RuntimeError("PUBLIC_DOMAIN is required for MAX webhook mode")
        payload = {
            "url": self.settings.max_webhook_url,
            "update_types": ["message_created", "message_callback", "bot_started"],
            "secret": self.settings.max_webhook_secret,
        }
        return await self._request("POST", "/subscriptions", json=payload)


def _extract_update_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("updates"), list):
        return [x for x in payload["updates"] if isinstance(x, dict)]
    if payload.get("update_type") or payload.get("type"):
        return [payload]
    return []


def _message_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    text = body.get("text")
    if isinstance(text, str):
        return text
    text = message.get("text")
    return text if isinstance(text, str) else ""


def _sender(message: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("sender")
    if isinstance(sender, dict):
        return sender
    user = update.get("user")
    return user if isinstance(user, dict) else {}


def _display_name(user: dict[str, Any]) -> str | None:
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    full = " ".join(part for part in (first, last) if part)
    return full or user.get("name")



def _max_profile_kwargs(user: dict[str, Any]) -> dict[str, Any]:
    last_activity = None
    raw = user.get("last_activity_time")
    if isinstance(raw, (int, float)):
        try:
            last_activity = datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
        except Exception:
            last_activity = None
    meta = {k: user.get(k) for k in ("is_bot", "avatar_url", "full_avatar_url", "description") if user.get(k) is not None}
    return {
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "last_activity_at": last_activity,
        "profile_meta": meta,
    }

def _recipient_chat_id(message: dict[str, Any]) -> int | None:
    recipient = message.get("recipient")
    if isinstance(recipient, dict) and recipient.get("chat_id") is not None:
        return int(recipient["chat_id"])
    return None


def _extract_mid(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    body = message.get("body") if isinstance(message, dict) else None
    if isinstance(body, dict) and body.get("mid"):
        return str(body["mid"])
    if isinstance(message, dict) and message.get("mid"):
        return str(message["mid"])
    return None


def _extract_contact_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    body = message.get("body") or {}
    for attachment in body.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("type") == "contact":
            payload = attachment.get("payload")
            return payload if isinstance(payload, dict) else None
    return None


def _phone_from_vcard(vcf_info: str) -> str | None:
    match = re.search(r"^TEL(?:;[^:]*)?:(.+)$", vcf_info, flags=re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _verify_max_contact(token: str, vcf_info: str, received_hash: str) -> bool:
    normalized = vcf_info.replace("\\r\\n", "\r\n")
    digest = hmac.new(token.encode(), normalized.encode(), hashlib.sha256).digest()
    candidates = {digest.hex(), base64.b64encode(digest).decode(), base64.urlsafe_b64encode(digest).decode().rstrip("=")}
    return any(hmac.compare_digest(received_hash, candidate) for candidate in candidates)


def _wizard_keyboard(step: str, data: dict, display_name: str | None) -> list[list[dict[str, Any]]]:
    if step == "review":
        rows = [
            [("✅ Отправить заявку", "wiz|submit")],
            [("✏️ Изменить параметры", "wiz|edit")],
            [("⬅️ Назад", "wiz|back"), ("❌ Отменить", "wiz|cancel")],
        ]
        return MaxClient.callback_keyboard(rows)

    rows: list[list[tuple[str, str]]] = []
    has_manual = False
    for row in choice_rows(step, data, display_name=display_name):
        built = []
        for choice in row:
            if choice.code == "manual":
                payload = "wiz|manual"
                has_manual = True
            else:
                payload = f"wiz|set|{choice.code}"
            built.append((choice.text, payload))
        rows.append(built)
    if step not in {"children_ages", "phone"} and not has_manual:
        rows.append([("✍️ Ввести вручную", "wiz|manual")])
    rows.append([("⬅️ Назад", "wiz|back"), ("❌ Отменить", "wiz|cancel")])
    return MaxClient.callback_keyboard(rows)


def _edit_keyboard() -> list[list[dict[str, Any]]]:
    rows = [[(c.text, f"wiz|editstep|{c.value}") for c in row] for row in review_edit_rows()]
    rows.append([("⬅️ К заявке", "wiz|review")])
    return MaxClient.callback_keyboard(rows)


def _main_menu_keyboard(*, subscribed: bool = False, is_admin: bool = False) -> list[list[dict[str, Any]]]:
    rows = [[("🧳 Подобрать тур", "menu|start")]]
    if subscribed:
        rows.append([("🔕 Отписаться от рассылки", "menu|unsubscribe")])
    else:
        rows.append([("🔥 Подписаться на рассылку", "menu|subscribe")])
    rows.append([("ℹ️ Контакты", "menu|contacts")])
    if is_admin:
        rows.append([("🛠 Админ-панель", "admin|home")])
    return MaxClient.callback_keyboard(rows)


def _admin_keyboard() -> list[list[dict[str, Any]]]:
    return MaxClient.callback_keyboard([
        [("📊 Статистика", "admin|stats"), ("🆕 Новые заявки", "admin|leads")],
        [("📣 Создать рассылку", "admin|broadcast")],
        [("⬅️ Главное меню", "menu|home")],
    ])


def _broadcast_target_keyboard() -> list[list[dict[str, Any]]]:
    return MaxClient.callback_keyboard([
        [("Telegram", "admin|btarget|telegram"), ("MAX", "admin|btarget|max")],
        [("Оба мессенджера", "admin|btarget|both")],
        [("⬅️ Админ-панель", "admin|home")],
    ])


def _broadcast_preview_keyboard(broadcast_id: int, url: str | None = None, button_text: str | None = None) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    if url:
        rows.append([{"type": "link", "text": button_text or "Подробнее", "url": url}])
    rows.extend(MaxClient.callback_keyboard([
        [("🚀 Запустить рассылку", f"broadcast|send|{broadcast_id}")],
        [("❌ Отменить", f"broadcast|cancel|{broadcast_id}")],
    ]))
    return rows


def _broadcast_link_keyboard(broadcast_id: int) -> list[list[dict[str, Any]]]:
    return MaxClient.callback_keyboard([
        [("🔗 Добавить кнопку-ссылку", f"broadcast|addlink|{broadcast_id}")],
        [("Без ссылки", f"broadcast|nolink|{broadcast_id}")],
        [("❌ Отменить", f"broadcast|cancel|{broadcast_id}")],
    ])


def _broadcast_button_text_keyboard(broadcast_id: int) -> list[list[dict[str, Any]]]:
    return MaxClient.callback_keyboard([
        [("Оставить «Подробнее»", f"broadcast|defaulttext|{broadcast_id}")],
        [("❌ Отменить", f"broadcast|cancel|{broadcast_id}")],
    ])


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_admin(settings: Settings, user_id: int) -> bool:
    return user_id in settings.max_admin_user_id_list


async def _menu_state(settings: Settings, user_id: int) -> tuple[bool, bool]:
    async with SessionLocal() as session:
        user = await get_user(session, platform="max", platform_user_id=user_id)
        subscribed = bool(user and user.subscribed)
    return subscribed, _is_admin(settings, user_id)


async def _set_ui_mid(user_id: int, mid: str) -> None:
    async with SessionLocal() as session:
        conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
        data = dict(conv.data or {})
        data["_ui_message_id"] = mid
        await save_conversation(session, conv, step=conv.step, data=data)
        await session.commit()


async def _render_card(client: MaxClient, user_id: int, text: str, step: str, data: dict, display_name: str | None) -> None:
    keyboard = client.phone_keyboard() if step == "phone" else _wizard_keyboard(step, data, display_name)
    mid = data.get("_ui_message_id")
    if mid:
        try:
            await client.edit_message(str(mid), text, keyboard=keyboard)
            return
        except Exception:
            log.debug("MAX wizard card edit failed; sending a new card", exc_info=True)
    result = await client.send_to_user(user_id, text, keyboard=keyboard)
    new_mid = _extract_mid(result)
    if new_mid:
        await _set_ui_mid(user_id, new_mid)


async def _show_menu(client: MaxClient, user_id: int, chat_id: int | None, sender: dict[str, Any], *, callback_id: str | None = None) -> None:
    async with SessionLocal() as session:
        user = await upsert_user(
            session, platform="max", platform_user_id=user_id, chat_id=chat_id,
            username=sender.get("username"), display_name=_display_name(sender), **_max_profile_kwargs(sender),
        )
        await reset_conversation(session, platform="max", platform_user_id=user_id)
        await session.commit()
        subscribed = user.subscribed
    text = "🌴 <b>Солнечные туры</b>\n\nВыберите, что хотите сделать:"
    keyboard = _main_menu_keyboard(subscribed=subscribed, is_admin=_is_admin(client.settings, user_id))
    if callback_id:
        await client.answer_callback(callback_id, text, keyboard=keyboard)
    else:
        await client.send_to_user(user_id, text, keyboard=keyboard)


async def _start_flow(client: MaxClient, user_id: int, chat_id: int | None, sender: dict[str, Any]) -> None:
    async with SessionLocal() as session:
        user = await upsert_user(
            session,
            platform="max",
            platform_user_id=user_id,
            chat_id=chat_id,
            username=sender.get("username"),
            display_name=_display_name(sender), **_max_profile_kwargs(sender),
        )
        await log_user_event(session, user, "wizard_started")
        conv = await reset_conversation(session, platform="max", platform_user_id=user_id)
        await save_conversation(session, conv, step="departure_city", data={"_history": []})
        await session.commit()
    result = start_scenario()
    log.info("MAX scenario started user_id=%s", user_id)
    await _render_card(client, user_id, result.reply, result.next_step, result.data, _display_name(sender))


async def _set_subscription(client: MaxClient, user_id: int, chat_id: int | None, sender: dict[str, Any], enabled: bool) -> None:
    async with SessionLocal() as session:
        user = await upsert_user(
            session,
            platform="max",
            platform_user_id=user_id,
            chat_id=chat_id,
            username=sender.get("username"),
            display_name=_display_name(sender), **_max_profile_kwargs(sender),
        )
        await set_subscription(session, user, enabled=enabled)
        await session.commit()
    log.info("MAX subscription changed user_id=%s enabled=%s", user_id, enabled)
    await client.send_to_user(
        user_id, SUBSCRIBE_TEXT if enabled else UNSUBSCRIBE_TEXT,
        keyboard=_main_menu_keyboard(subscribed=enabled, is_admin=_is_admin(client.settings, user_id)),
    )


async def _apply_value(client: MaxClient, user_id: int, chat_id: int | None, sender: dict[str, Any], value: str) -> None:
    async with SessionLocal() as session:
        user = await upsert_user(
            session,
            platform="max",
            platform_user_id=user_id,
            chat_id=chat_id,
            username=sender.get("username"),
            display_name=_display_name(sender), **_max_profile_kwargs(sender),
        )
        conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
        log.info("MAX wizard input user_id=%s step=%s", user_id, conv.step)
        log.debug("MAX wizard raw input user_id=%s value=%r", user_id, value)
        if conv.step == "review":
            return
        await log_user_event(session, user, "wizard_answer", {"step": conv.step, "value": value})
        result = consume(conv.step, value, conv.data)
        await save_conversation(session, conv, step=result.next_step, data=result.data)
        await session.commit()
    await _render_card(client, user_id, result.reply, result.next_step, result.data, _display_name(sender))


async def _submit(client: MaxClient, notifier, user_id: int, chat_id: int | None, sender: dict[str, Any], callback_id: str | None = None) -> None:
    async with SessionLocal() as session:
        user = await upsert_user(
            session,
            platform="max",
            platform_user_id=user_id,
            chat_id=chat_id,
            username=sender.get("username"),
            display_name=_display_name(sender), **_max_profile_kwargs(sender),
        )
        conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
        if conv.step != "review":
            return
        lead = await create_lead(session, user=user, data=conv.data)
        await reset_conversation(session, platform="max", platform_user_id=user_id)
        await session.commit()
    log.info("MAX lead submitted lead_id=%s user_id=%s", lead.id, user_id)
    text = "✅ <b>Заявка отправлена!</b>\n\nМенеджер «Солнечных туров» свяжется с вами и предложит подходящие варианты."
    menu = _main_menu_keyboard(subscribed=user.subscribed, is_admin=_is_admin(client.settings, user_id))
    if callback_id:
        await client.answer_callback(callback_id, text, keyboard=menu)
    else:
        await client.send_to_user(user_id, text, keyboard=menu)
    await notifier.notify_lead(lead.id)


def _callback_message_mid(update: dict[str, Any]) -> str | None:
    callback = update.get("callback") or {}
    message = update.get("message")
    if not isinstance(message, dict) and isinstance(callback, dict):
        message = callback.get("message")
    if isinstance(message, dict):
        body = message.get("body") or {}
        if isinstance(body, dict) and body.get("mid"):
            return str(body["mid"])
        if message.get("mid"):
            return str(message["mid"])
    return None


def _extract_callback(update: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any], int | None]:
    callback = update.get("callback") or {}
    if not isinstance(callback, dict):
        callback = {}
    callback_id = callback.get("callback_id") or update.get("callback_id")
    payload = callback.get("payload") or update.get("payload")
    user = callback.get("user") or update.get("user") or {}
    message = update.get("message") or callback.get("message") or {}
    if not user and isinstance(message, dict):
        user = message.get("sender") or {}
    chat_id = update.get("chat_id")
    if chat_id is None and isinstance(message, dict):
        chat_id = _recipient_chat_id(message)
    return (
        str(callback_id) if callback_id else None,
        str(payload) if payload is not None else None,
        user if isinstance(user, dict) else {},
        int(chat_id) if chat_id is not None else None,
    )


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


async def _handle_callback(client: MaxClient, notifier, update: dict[str, Any]) -> None:
    callback_id, payload, sender, chat_id = _extract_callback(update)
    user_id_raw = sender.get("user_id")
    log.info("MAX callback received user_id=%s action=%s", user_id_raw, payload)
    log.debug("MAX callback raw payload=%r", update)
    if not callback_id or not payload or user_id_raw is None:
        log.warning("MAX callback missing callback_id/payload/user_id")
        return
    user_id = int(user_id_raw)
    callback_mid = _callback_message_mid(update)

    if payload.startswith("lead|"):
        if not _is_admin(client.settings, user_id):
            await client.answer_callback(callback_id, "Нет доступа")
            return
        _, action, raw_id = payload.split("|", 2)
        status = LEAD_PROCESSED if action == "processed" else LEAD_NEW
        async with SessionLocal() as session:
            lead = await set_lead_status(
                session, lead_id=int(raw_id), status=status, actor_platform="max",
                actor_user_id=user_id, actor_name=_display_name(sender),
            )
            if not lead:
                await client.answer_callback(callback_id, "Заявка не найдена")
                return
            await session.commit()
        await client.answer_callback(callback_id, "Заявка обработана ✅" if status == LEAD_PROCESSED else "Заявка возвращена в новые")
        await notifier.sync_lead(int(raw_id))
        return

    if payload.startswith("broadcast|"):
        if not _is_admin(client.settings, user_id):
            await client.answer_callback(callback_id, "Нет доступа")
            return
        _, action, raw_id = payload.split("|", 2)
        bid = int(raw_id)
        if action == "cancel":
            async with SessionLocal() as session:
                item = await get_broadcast(session, bid)
                if item:
                    await cancel_broadcast(session, item)
                    await reset_conversation(session, platform="max", platform_user_id=user_id)
                    await session.commit()
            await client.answer_callback(callback_id, "Рассылка отменена.", keyboard=_admin_keyboard())
        elif action == "addlink":
            async with SessionLocal() as session:
                conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
                conv.step = "admin_broadcast"
                conv.data = {"_admin_mode": "broadcast_link_url", "_broadcast_id": bid, "_ui_message_id": callback_mid}
                await session.commit()
            await client.answer_callback(callback_id, "🔗 <b>Пришлите ссылку</b> обычным сообщением.\n\nНапример: https://suntur36.ru/")
        elif action == "nolink":
            async with SessionLocal() as session:
                item = await get_broadcast(session, bid)
                await reset_conversation(session, platform="max", platform_user_id=user_id)
                await session.commit()
            if item:
                preview = f"📣 <b>Предпросмотр рассылки #{item.id}</b>\nАудитория: <b>{escape(item.target)}</b>\n\n{escape(item.text or '')}"
                await client.answer_callback(callback_id, preview, keyboard=_broadcast_preview_keyboard(item.id, item.button_url, item.button_text))
        elif action == "defaulttext":
            async with SessionLocal() as session:
                item = await get_broadcast(session, bid)
                if item and item.button_url:
                    await update_broadcast_link(session, item, url=item.button_url, text="Подробнее")
                await reset_conversation(session, platform="max", platform_user_id=user_id)
                await session.commit()
            if item:
                preview = f"📣 <b>Предпросмотр рассылки #{item.id}</b>\nАудитория: <b>{escape(item.target)}</b>\n\n{escape(item.text or '')}"
                await client.answer_callback(callback_id, preview, keyboard=_broadcast_preview_keyboard(item.id, item.button_url, item.button_text))
        elif action == "send":
            async with SessionLocal() as session:
                item = await get_broadcast(session, bid)
                if not item:
                    await client.answer_callback(callback_id, "Рассылка не найдена")
                    return
                try:
                    await queue_broadcast(session, item)
                except ValueError as exc:
                    await client.answer_callback(callback_id, str(exc))
                    return
                await session.commit()
                total = item.total_recipient_count
            await client.answer_callback(
                callback_id,
                f"🚀 <b>Рассылка #{bid} поставлена в очередь</b>\n\nПолучателей: {total}\nБот продолжает работать во время отправки.",
                keyboard=_admin_keyboard(),
            )
        return

    if payload.startswith("admin|"):
        if not _is_admin(client.settings, user_id):
            await client.answer_callback(callback_id, "Нет доступа")
            return
        parts = payload.split("|")
        action = parts[1]
        if action == "home":
            await client.answer_callback(callback_id, "🛠 <b>Админ-панель</b>", keyboard=_admin_keyboard())
        elif action == "stats":
            async with SessionLocal() as session:
                st = await stats(session)
            await client.answer_callback(callback_id, _admin_stats_text(st), keyboard=_admin_keyboard())
        elif action == "leads":
            async with SessionLocal() as session:
                leads = await recent_new_leads(session, 10)
            text = "🆕 <b>Последние новые заявки</b>\n\n" + (
                "\n".join(f"#{x.id} · {escape(x.client_name)} · {escape(x.destination)}" for x in leads) if leads else "Новых заявок сейчас нет."
            )
            await client.answer_callback(callback_id, text, keyboard=_admin_keyboard())
        elif action == "broadcast":
            flag = "включена" if client.settings.max_marketing_broadcast_enabled else "выключена"
            await client.answer_callback(callback_id, f"📣 <b>Новая рассылка</b>\n\nКуда отправляем?\nMAX-рассылка сейчас: <b>{flag}</b>.", keyboard=_broadcast_target_keyboard())
        elif action == "btarget" and len(parts) >= 3:
            target = parts[2]
            async with SessionLocal() as session:
                conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
                conv.step = "admin_broadcast"
                conv.data = {"_admin_mode": "broadcast_content", "_admin_target": target, "_ui_message_id": callback_mid}
                await session.commit()
            await client.answer_callback(
                callback_id,
                "✍️ <b>Отправьте текст рассылки обычным сообщением.</b>\n\n"
                "Rich-media рассылку (фото/видео) удобнее создавать из Telegram-админки; получатели MAX её получат нормально.",
                keyboard=MaxClient.callback_keyboard([[("❌ Отменить", "admin|home")]]),
            )
        return

    if payload.startswith("menu|"):
        action = payload.split("|", 1)[1]
        if action == "home":
            await _show_menu(client, user_id, chat_id, sender, callback_id=callback_id)
            return
        if action == "start":
            async with SessionLocal() as session:
                conv = await reset_conversation(session, platform="max", platform_user_id=user_id)
                data = dict(conv.data or {})
                if callback_mid:
                    data["_ui_message_id"] = callback_mid
                await save_conversation(session, conv, step="departure_city", data=data)
                await session.commit()
            result = start_scenario()
            if callback_mid:
                result.data["_ui_message_id"] = callback_mid
            await client.answer_callback(callback_id, result.reply, keyboard=_wizard_keyboard(result.next_step, result.data, _display_name(sender)))
            return
        if action in {"subscribe", "unsubscribe"}:
            enabled = action == "subscribe"
            async with SessionLocal() as session:
                user = await upsert_user(
                    session,
                    platform="max",
                    platform_user_id=user_id,
                    chat_id=chat_id,
                    username=sender.get("username"),
                    display_name=_display_name(sender), **_max_profile_kwargs(sender),
                )
                await set_subscription(session, user, enabled=enabled)
                await session.commit()
            await client.answer_callback(
                callback_id, SUBSCRIBE_TEXT if enabled else UNSUBSCRIBE_TEXT,
                keyboard=_main_menu_keyboard(subscribed=enabled, is_admin=_is_admin(client.settings, user_id)),
            )
            return
        if action == "contacts":
            subscribed, admin = await _menu_state(client.settings, user_id)
            await client.answer_callback(callback_id, AGENCY_CONTACTS, keyboard=_main_menu_keyboard(subscribed=subscribed, is_admin=admin))
            return

    if not payload.startswith("wiz|"):
        return

    parts = payload.split("|")
    action = parts[1]
    async with SessionLocal() as session:
        conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
        step = conv.step
        data = dict(conv.data or {})
        if callback_mid:
            data["_ui_message_id"] = callback_mid

        if action == "set" and len(parts) >= 3:
            value = resolve_choice(step, parts[2], data, display_name=_display_name(sender))
            if value is None:
                log.warning("MAX stale/unknown choice user_id=%s step=%s code=%s", user_id, step, parts[2])
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
            await client.answer_callback(
                callback_id,
                "✏️ <b>Что хотите изменить?</b>\n\nПосле изменения вернёмся к проверке заявки.",
                keyboard=_edit_keyboard(),
            )
            return
        elif action == "editstep" and len(parts) >= 3:
            result = edit_step(data, parts[2])
        elif action == "manual":
            await client.answer_callback(
                callback_id,
                render_step(step, data) + "\n\n✍️ <i>Напишите ответ обычным сообщением.</i>",
                keyboard=_wizard_keyboard(step, data, _display_name(sender)),
            )
            return
        elif action == "cancel":
            await reset_conversation(session, platform="max", platform_user_id=user_id)
            await session.commit()
            subscribed, admin = await _menu_state(client.settings, user_id)
            await client.answer_callback(callback_id, "Подбор тура отменён. Можно начать заново в любой момент.", keyboard=_main_menu_keyboard(subscribed=subscribed, is_admin=admin))
            return
        elif action == "submit":
            await session.rollback()
            await _submit(client, notifier, user_id, chat_id, sender, callback_id)
            return
        else:
            return

        await save_conversation(session, conv, step=result.next_step, data=result.data)
        await session.commit()

    keyboard = client.phone_keyboard() if result.next_step == "phone" else _wizard_keyboard(result.next_step, result.data, _display_name(sender))
    await client.answer_callback(callback_id, result.reply, keyboard=keyboard)


async def process_max_update(client: MaxClient, update: dict[str, Any], notifier) -> None:
    update_type = update.get("update_type") or update.get("type")
    log.debug("MAX raw update type=%s payload=%r", update_type, update)

    if update_type == "message_callback":
        await _handle_callback(client, notifier, update)
        return

    if update_type == "bot_started":
        sender = update.get("user") or {}
        user_id = sender.get("user_id")
        chat_id = update.get("chat_id")
        log.info("MAX bot_started user_id=%s username=%s chat_id=%s", user_id, sender.get("username"), chat_id)
        if user_id is not None:
            await _show_menu(client, int(user_id), int(chat_id) if chat_id is not None else None, sender)
        return

    if update_type != "message_created":
        log.debug("MAX update ignored type=%s", update_type)
        return

    message = update.get("message") or {}
    if not isinstance(message, dict):
        log.warning("MAX message_created without message object")
        return
    sender = _sender(message, update)
    user_id_raw = sender.get("user_id")
    if user_id_raw is None:
        log.warning("MAX message without sender user_id")
        return
    user_id = int(user_id_raw)
    chat_id = _recipient_chat_id(message)
    text = _message_text(message).strip()
    contact_payload = _extract_contact_payload(message)

    log.info("MAX message received user_id=%s username=%s has_text=%s has_contact=%s", user_id, sender.get("username"), bool(text), bool(contact_payload))
    log.debug("MAX message text user_id=%s text=%r", user_id, text)

    if contact_payload:
        vcf_info = str(contact_payload.get("vcf_info") or "")
        received_hash = str(contact_payload.get("hash") or "")
        phone = _phone_from_vcard(vcf_info)
        valid = bool(vcf_info and received_hash and _verify_max_contact(client.settings.max_token, vcf_info, received_hash))
        log.info("MAX contact received user_id=%s verified=%s", user_id, valid)
        if valid and phone:
            await _apply_value(client, user_id, chat_id, sender, phone)
        else:
            log.warning("MAX contact verification failed user_id=%s", user_id)
            await client.send_to_user(user_id, "Не удалось проверить контакт. Введите номер телефона вручную, пожалуйста.")
        return

    if not text:
        return
    if text == "/whoami":
        await client.send_to_user(user_id, f"MAX user_id: {user_id}\nchat_id: {chat_id}")
        return
    if text == "/start":
        await _show_menu(client, user_id, chat_id, sender)
        return
    if text == "/subscribe":
        await _set_subscription(client, user_id, chat_id, sender, True)
        return
    if text == "/unsubscribe":
        await _set_subscription(client, user_id, chat_id, sender, False)
        return
    if text == "/admin" and _is_admin(client.settings, user_id):
        await client.send_to_user(user_id, "🛠 <b>Админ-панель</b>", keyboard=_admin_keyboard())
        return

    async with SessionLocal() as session:
        conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
        data = dict(conv.data or {})
        if _is_admin(client.settings, user_id) and conv.step == "admin_broadcast":
            mode = data.get("_admin_mode")
            if mode == "broadcast_content":
                item = await create_broadcast(
                    session, platform="max", user_id=user_id, name=_display_name(sender),
                    target=str(data.get("_admin_target", "both")), text=text,
                )
                conv.data = {"_admin_mode": "broadcast_link_choice", "_broadcast_id": item.id, "_ui_message_id": data.get("_ui_message_id")}
                await session.commit()
                await client.send_to_user(user_id, "🔗 <b>Добавить кнопку-ссылку?</b>", keyboard=_broadcast_link_keyboard(item.id))
                return
            if mode == "broadcast_link_url":
                bid = int(data.get("_broadcast_id"))
                if not _valid_http_url(text):
                    await client.send_to_user(user_id, "Ссылка должна начинаться с http:// или https://. Попробуйте ещё раз.")
                    return
                item = await get_broadcast(session, bid)
                if not item:
                    await client.send_to_user(user_id, "Черновик рассылки не найден.")
                    return
                await update_broadcast_link(session, item, url=text.strip(), text="Подробнее")
                conv.data = {"_admin_mode": "broadcast_button_text", "_broadcast_id": bid, "_ui_message_id": data.get("_ui_message_id")}
                await session.commit()
                await client.send_to_user(user_id, "✏️ <b>Как назвать кнопку?</b>\n\nНапишите свой текст или оставьте «Подробнее».", keyboard=_broadcast_button_text_keyboard(bid))
                return
            if mode == "broadcast_button_text":
                bid = int(data.get("_broadcast_id"))
                item = await get_broadcast(session, bid)
                if not item or not item.button_url:
                    await client.send_to_user(user_id, "Черновик рассылки не найден.")
                    return
                await update_broadcast_link(session, item, url=item.button_url, text=text.strip()[:64] or "Подробнее")
                await reset_conversation(session, platform="max", platform_user_id=user_id)
                await session.commit()
                preview = f"📣 <b>Предпросмотр рассылки #{item.id}</b>\nАудитория: <b>{escape(item.target)}</b>\n\n{escape(item.text or '')}"
                await client.send_to_user(user_id, preview, keyboard=_broadcast_preview_keyboard(item.id, item.button_url, item.button_text))
                return
        if conv.step == "idle":
            await _show_menu(client, user_id, chat_id, sender)
            return
        if conv.step == "review":
            await client.send_to_user(user_id, "Используйте кнопки под заявкой: отправить или изменить параметры.")
            return
    await _apply_value(client, user_id, chat_id, sender, text)


async def run_max_polling(client: MaxClient, notifier) -> None:
    marker: int | None = None
    retry_delay = 3
    log.info("MAX long polling started (development mode)")
    while True:
        try:
            payload = await client.get_updates(marker=marker)
            log.debug("MAX polling payload=%r", payload)
            if isinstance(payload, dict) and payload.get("marker") is not None:
                marker = int(payload["marker"])
            for update in _extract_update_list(payload):
                await process_max_update(client, update, notifier)
        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError:
            log.warning("MAX network error; retrying in %ss", retry_delay, exc_info=True)
            await asyncio.sleep(retry_delay)
        except Exception:
            log.exception("MAX polling error; retrying in %ss", retry_delay)
            await asyncio.sleep(retry_delay)
