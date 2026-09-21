import asyncio
import logging
from typing import Any

import aiohttp

from app.config import Settings
from app.db import SessionLocal
from app.scenario import consume, start_scenario
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
            self.session = aiohttp.ClientSession(
                headers={"Authorization": self.settings.max_token}
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        await self.open()
        assert self.session is not None
        async with self.session.request(method, f"{self.base}{path}", **kwargs) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"MAX {method} {path}: {resp.status} {body}")
            if not body:
                return None
            try:
                return await resp.json()
            except Exception:
                return body

    async def send_to_user(self, user_id: int, text: str, *, keyboard: list[list[dict]] | None = None):
        payload: dict[str, Any] = {"text": text}
        if keyboard:
            payload["attachments"] = [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": keyboard},
                }
            ]
        return await self._request(
            "POST",
            "/messages",
            params={"user_id": user_id},
            json=payload,
        )

    async def send_to_chat(self, chat_id: int, text: str, *, keyboard: list[list[dict]] | None = None):
        payload: dict[str, Any] = {"text": text}
        if keyboard:
            payload["attachments"] = [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": keyboard},
                }
            ]
        return await self._request(
            "POST",
            "/messages",
            params={"chat_id": chat_id},
            json=payload,
        )

    async def notify_admins(self, text: str) -> None:
        for user_id in self.settings.max_admin_user_id_list:
            try:
                await self.send_to_user(user_id, text)
            except Exception:
                log.exception("Could not notify MAX admin user %s", user_id)
            await asyncio.sleep(self.settings.broadcast_delay_seconds)

        for chat_id in self.settings.max_admin_chat_id_list:
            try:
                await self.send_to_chat(chat_id, text)
            except Exception:
                log.exception("Could not notify MAX admin chat %s", chat_id)
            await asyncio.sleep(self.settings.broadcast_delay_seconds)

    async def get_updates(self, marker: int | None = None, timeout: int = 30) -> Any:
        params: dict[str, Any] = {"timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params)

    async def create_subscription(self) -> Any:
        if not self.settings.max_webhook_url:
            raise RuntimeError("PUBLIC_DOMAIN is required for MAX webhook mode")
        payload = {
            "url": self.settings.max_webhook_url,
            "update_types": ["message_created", "bot_started"],
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


def _recipient_chat_id(message: dict[str, Any]) -> int | None:
    recipient = message.get("recipient")
    if isinstance(recipient, dict):
        value = recipient.get("chat_id")
        if value is not None:
            return int(value)
    return None


async def process_max_update(client: MaxClient, update: dict[str, Any]) -> None:
    update_type = update.get("update_type") or update.get("type")
    message = update.get("message") or {}

    if update_type == "bot_started":
        user = update.get("user") or {}
        user_id = user.get("user_id")
        chat_id = update.get("chat_id")
        if user_id is None:
            return

        async with SessionLocal() as session:
            await upsert_user(
                session,
                platform="max",
                platform_user_id=int(user_id),
                chat_id=int(chat_id) if chat_id is not None else None,
                username=user.get("username"),
                display_name=user.get("name"),
            )
            await reset_conversation(
                session,
                platform="max",
                platform_user_id=int(user_id),
            )
            await session.commit()

        result = start_scenario()
        await client.send_to_user(int(user_id), result.reply)
        return

    if update_type != "message_created" or not isinstance(message, dict):
        return

    sender = _sender(message, update)
    user_id = sender.get("user_id")
    if user_id is None:
        return

    text = _message_text(message).strip()
    if not text:
        return

    chat_id = _recipient_chat_id(message)

    if text == "/whoami":
        await client.send_to_user(
            int(user_id),
            f"MAX user_id: {user_id}\nchat_id: {chat_id}",
        )
        return

    if text == "/subscribe":
        async with SessionLocal() as session:
            user = await upsert_user(
                session,
                platform="max",
                platform_user_id=int(user_id),
                chat_id=chat_id,
                username=sender.get("username"),
                display_name=sender.get("name"),
            )
            await set_subscription(session, user, enabled=True)
            await session.commit()
        await client.send_to_user(
            int(user_id),
            "Подписка на рассылку включена ✅\n"
            "Отписаться можно в любой момент командой /unsubscribe.",
        )
        return

    if text == "/unsubscribe":
        async with SessionLocal() as session:
            user = await upsert_user(
                session,
                platform="max",
                platform_user_id=int(user_id),
                chat_id=chat_id,
                username=sender.get("username"),
                display_name=sender.get("name"),
            )
            await set_subscription(session, user, enabled=False)
            await session.commit()
        await client.send_to_user(
            int(user_id),
            "Рассылка отключена. Вернуть её можно командой /subscribe.",
        )
        return

    if text == "/start":
        async with SessionLocal() as session:
            await upsert_user(
                session,
                platform="max",
                platform_user_id=int(user_id),
                chat_id=chat_id,
                username=sender.get("username"),
                display_name=sender.get("name"),
            )
            await reset_conversation(
                session,
                platform="max",
                platform_user_id=int(user_id),
            )
            await session.commit()
        result = start_scenario()
        await client.send_to_user(int(user_id), result.reply)
        return

    async with SessionLocal() as session:
        user = await upsert_user(
            session,
            platform="max",
            platform_user_id=int(user_id),
            chat_id=chat_id,
            username=sender.get("username"),
            display_name=sender.get("name"),
        )
        conv = await get_or_create_conversation(
            session,
            platform="max",
            platform_user_id=int(user_id),
        )

        result = consume(conv.step, text, conv.data)

        if result.completed:
            lead = await create_lead(session, user=user, data=result.data)
            await reset_conversation(
                session,
                platform="max",
                platform_user_id=int(user_id),
            )
            await session.commit()

            await client.send_to_user(int(user_id), result.reply)
            await client.notify_admins(format_lead(lead, user))
            return

        await save_conversation(
            session,
            conv,
            step=result.next_step or "destination",
            data=result.data,
        )
        await session.commit()

    if result.next_step == "contact_method":
        keyboard = [[
            {"type": "callback", "text": "В чате", "payload": "contact:chat"},
            {"type": "callback", "text": "По телефону", "payload": "contact:phone"},
        ]]
        # Пока одновременно даём текстовый вариант: обработку message_callback добавим в следующем слое.
        await client.send_to_user(
            int(user_id),
            result.reply + "\n\nОтветьте: «В чате» или «По телефону».",
            keyboard=keyboard,
        )
    else:
        await client.send_to_user(int(user_id), result.reply)


async def run_max_polling(client: MaxClient) -> None:
    marker: int | None = None
    log.info("MAX long polling started (development mode)")
    while True:
        try:
            payload = await client.get_updates(marker=marker)
            if isinstance(payload, dict) and payload.get("marker") is not None:
                marker = int(payload["marker"])
            for update in _extract_update_list(payload):
                await process_max_update(client, update)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("MAX polling error")
            await asyncio.sleep(3)
