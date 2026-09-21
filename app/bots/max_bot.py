import asyncio
import base64
import hashlib
import hmac
import logging
import re
from typing import Any

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
    create_lead,
    format_lead,
    get_or_create_conversation,
    reset_conversation,
    save_conversation,
    set_subscription,
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
    def _payload(text: str, keyboard: list[list[dict[str, Any]]] | None = None, *, notify: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text, "notify": notify, "format": "html"}
        if keyboard is not None:
            payload["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": keyboard}}]
        return payload

    async def send_to_user(self, user_id: int, text: str, *, keyboard=None, notify: bool = True) -> Any:
        return await self._request(
            "POST", "/messages", params={"user_id": user_id}, json=self._payload(text, keyboard, notify=notify)
        )

    async def send_to_chat(self, chat_id: int, text: str, *, keyboard=None, notify: bool = True) -> Any:
        return await self._request(
            "POST", "/messages", params={"chat_id": chat_id}, json=self._payload(text, keyboard, notify=notify)
        )

    async def edit_message(self, message_id: str, text: str, *, keyboard=None) -> Any:
        return await self._request(
            "PUT", "/messages", params={"message_id": message_id}, json=self._payload(text, keyboard, notify=False)
        )

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


def _main_menu_keyboard() -> list[list[dict[str, Any]]]:
    return MaxClient.callback_keyboard([
        [("🧳 Подобрать тур", "menu|start")],
        [("🔥 Подписаться на рассылку", "menu|subscribe")],
        [("🔕 Отписаться от рассылки", "menu|unsubscribe")],
        [("ℹ️ Контакты", "menu|contacts")],
    ])


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


async def _start_flow(client: MaxClient, user_id: int, chat_id: int | None, sender: dict[str, Any]) -> None:
    async with SessionLocal() as session:
        await upsert_user(
            session,
            platform="max",
            platform_user_id=user_id,
            chat_id=chat_id,
            username=sender.get("username"),
            display_name=_display_name(sender),
        )
        await reset_conversation(session, platform="max", platform_user_id=user_id)
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
            display_name=_display_name(sender),
        )
        await set_subscription(session, user, enabled=enabled)
        await session.commit()
    log.info("MAX subscription changed user_id=%s enabled=%s", user_id, enabled)
    await client.send_to_user(user_id, SUBSCRIBE_TEXT if enabled else UNSUBSCRIBE_TEXT, keyboard=_main_menu_keyboard())


async def _apply_value(client: MaxClient, user_id: int, chat_id: int | None, sender: dict[str, Any], value: str) -> None:
    async with SessionLocal() as session:
        await upsert_user(
            session,
            platform="max",
            platform_user_id=user_id,
            chat_id=chat_id,
            username=sender.get("username"),
            display_name=_display_name(sender),
        )
        conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
        log.info("MAX wizard input user_id=%s step=%s", user_id, conv.step)
        log.debug("MAX wizard raw input user_id=%s value=%r", user_id, value)
        if conv.step == "review":
            return
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
            display_name=_display_name(sender),
        )
        conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
        if conv.step != "review":
            return
        lead = await create_lead(session, user=user, data=conv.data)
        await reset_conversation(session, platform="max", platform_user_id=user_id)
        await session.commit()
    log.info("MAX lead submitted lead_id=%s user_id=%s", lead.id, user_id)
    text = "✅ <b>Заявка отправлена!</b>\n\nМенеджер «Солнечных туров» свяжется с вами и предложит подходящие варианты."
    if callback_id:
        await client.answer_callback(callback_id, text, keyboard=_main_menu_keyboard())
    else:
        await client.send_to_user(user_id, text, keyboard=_main_menu_keyboard())
    await notifier.notify(format_lead(lead, user))


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

    if payload.startswith("menu|"):
        action = payload.split("|", 1)[1]
        if action == "start":
            async with SessionLocal() as session:
                conv = await reset_conversation(session, platform="max", platform_user_id=user_id)
                if callback_mid:
                    data = dict(conv.data or {})
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
                    display_name=_display_name(sender),
                )
                await set_subscription(session, user, enabled=enabled)
                await session.commit()
            await client.answer_callback(callback_id, SUBSCRIBE_TEXT if enabled else UNSUBSCRIBE_TEXT, keyboard=_main_menu_keyboard())
            return
        if action == "contacts":
            await client.answer_callback(callback_id, AGENCY_CONTACTS, keyboard=_main_menu_keyboard())
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
            await client.answer_callback(callback_id, "Подбор тура отменён. Можно начать заново в любой момент.", keyboard=_main_menu_keyboard())
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
            await _start_flow(client, int(user_id), int(chat_id) if chat_id is not None else None, sender)
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
        await _start_flow(client, user_id, chat_id, sender)
        return
    if text == "/subscribe":
        await _set_subscription(client, user_id, chat_id, sender, True)
        return
    if text == "/unsubscribe":
        await _set_subscription(client, user_id, chat_id, sender, False)
        return

    async with SessionLocal() as session:
        conv = await get_or_create_conversation(session, platform="max", platform_user_id=user_id)
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
