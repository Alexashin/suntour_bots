import asyncio
import logging
from html import escape
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.db import SessionLocal
from app.services import (
    BROADCAST_CANCELLED,
    BROADCAST_SENDING,
    LEAD_PROCESSED,
    add_lead_message,
    finalize_broadcast,
    format_lead,
    get_lead_and_user,
    get_lead_messages,
    mark_delivery,
    next_queued_broadcast,
    pending_broadcast_deliveries,
)

log = logging.getLogger(__name__)


def _extract_max_mid(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    body = message.get("body") if isinstance(message, dict) else None
    if isinstance(body, dict) and body.get("mid"):
        return str(body["mid"])
    if isinstance(message, dict) and message.get("mid"):
        return str(message["mid"])
    return None


class AdminNotifier:
    def __init__(self, settings: Settings, telegram_bot: Bot | None, max_client):
        self.settings = settings
        self.telegram_bot = telegram_bot
        self.max_client = max_client

    @staticmethod
    def telegram_lead_keyboard(lead_id: int, status: str) -> InlineKeyboardMarkup:
        if status == LEAD_PROCESSED:
            rows = [[InlineKeyboardButton(text="↩️ Вернуть в новые", callback_data=f"lead|new|{lead_id}")]]
        else:
            rows = [[InlineKeyboardButton(text="✅ Обработана", callback_data=f"lead|processed|{lead_id}")]]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def max_lead_keyboard(self, lead_id: int, status: str):
        if status == LEAD_PROCESSED:
            rows = [[("↩️ Вернуть в новые", f"lead|new|{lead_id}")]]
        else:
            rows = [[("✅ Обработана", f"lead|processed|{lead_id}")]]
        return self.max_client.callback_keyboard(rows) if self.max_client else None

    async def notify_lead(self, lead_id: int) -> None:
        async with SessionLocal() as session:
            pair = await get_lead_and_user(session, lead_id)
            if not pair:
                log.error("Cannot notify: lead_id=%s not found", lead_id)
                return
            lead, user = pair
            status = lead.status
            tg_text = format_lead(lead, user, render_platform="telegram")
            max_text = format_lead(lead, user, render_platform="max")

        delivered = 0
        stored: list[tuple[str, int | None, str]] = []

        if self.telegram_bot:
            for admin_id in self.settings.telegram_admin_id_list:
                try:
                    msg = await self.telegram_bot.send_message(
                        admin_id, tg_text, parse_mode="HTML",
                        reply_markup=self.telegram_lead_keyboard(lead_id, status),
                    )
                    stored.append(("telegram", admin_id, str(msg.message_id)))
                    delivered += 1
                except Exception:
                    log.exception("Failed to notify Telegram admin id=%s lead_id=%s", admin_id, lead_id)

        if self.max_client:
            keyboard = self.max_lead_keyboard(lead_id, status)
            for admin_id in self.settings.max_admin_user_id_list:
                try:
                    payload = await self.max_client.send_to_user(admin_id, max_text, keyboard=keyboard)
                    mid = _extract_max_mid(payload)
                    if mid:
                        stored.append(("max", admin_id, mid))
                    delivered += 1
                except Exception:
                    log.exception("Failed to notify MAX admin user id=%s lead_id=%s", admin_id, lead_id)
                await asyncio.sleep(self.settings.broadcast_delay_seconds)
            for chat_id in self.settings.max_admin_chat_id_list:
                try:
                    payload = await self.max_client.send_to_chat(chat_id, max_text, keyboard=keyboard)
                    mid = _extract_max_mid(payload)
                    if mid:
                        stored.append(("max", chat_id, mid))
                    delivered += 1
                except Exception:
                    log.exception("Failed to notify MAX admin chat id=%s lead_id=%s", chat_id, lead_id)
                await asyncio.sleep(self.settings.broadcast_delay_seconds)

        if stored:
            async with SessionLocal() as session:
                for platform, chat_id, message_id in stored:
                    await add_lead_message(session, lead_id=lead_id, platform=platform, chat_id=chat_id, message_id=message_id)
                await session.commit()

        if delivered:
            log.info("Lead notification delivered lead_id=%s destinations=%s", lead_id, delivered)
        else:
            log.warning("Lead created but no admin notification destination succeeded lead_id=%s", lead_id)

    async def sync_lead(self, lead_id: int) -> None:
        async with SessionLocal() as session:
            pair = await get_lead_and_user(session, lead_id)
            messages = await get_lead_messages(session, lead_id)
            if not pair:
                return
            lead, user = pair
            status = lead.status

        for item in messages:
            try:
                if item.platform == "telegram" and self.telegram_bot and item.chat_id is not None:
                    await self.telegram_bot.edit_message_text(
                        chat_id=item.chat_id, message_id=int(item.message_id),
                        text=format_lead(lead, user, render_platform="telegram"), parse_mode="HTML",
                        reply_markup=self.telegram_lead_keyboard(lead_id, status),
                    )
                elif item.platform == "max" and self.max_client:
                    await self.max_client.edit_message(
                        item.message_id, format_lead(lead, user, render_platform="max"),
                        keyboard=self.max_lead_keyboard(lead_id, status),
                    )
            except Exception:
                log.warning("Could not sync admin lead message lead_id=%s platform=%s message_id=%s",
                            lead_id, item.platform, item.message_id, exc_info=True)

    @staticmethod
    def _telegram_broadcast_keyboard(url: str | None, text: str | None):
        if not url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text or "Подробнее", url=url)]])

    async def _send_telegram_broadcast(self, user, broadcast):
        if not self.telegram_bot or not user.chat_id:
            raise RuntimeError("Telegram destination unavailable")
        keyboard = self._telegram_broadcast_keyboard(broadcast.button_url, broadcast.button_text)
        if broadcast.media_type == "image" and broadcast.media_path:
            msg = await self.telegram_bot.send_photo(
                user.chat_id, FSInputFile(broadcast.media_path), caption=broadcast.text or None,
                reply_markup=keyboard,
            )
        elif broadcast.media_type == "video" and broadcast.media_path:
            msg = await self.telegram_bot.send_video(
                user.chat_id, FSInputFile(broadcast.media_path), caption=broadcast.text or None,
                reply_markup=keyboard,
            )
        else:
            msg = await self.telegram_bot.send_message(user.chat_id, broadcast.text or " ", reply_markup=keyboard)
        return str(msg.message_id)

    async def _send_max_broadcast(self, user, broadcast, *, media_attachment=None):
        if not self.settings.max_marketing_broadcast_enabled or not self.max_client:
            raise PermissionError("MAX marketing broadcast disabled")
        keyboard = self.max_client.link_keyboard(broadcast.button_text or "Подробнее", broadcast.button_url) if broadcast.button_url else None
        attachments = [media_attachment] if media_attachment else None
        payload = await self.max_client.send_to_user(
            user.platform_user_id,
            escape(broadcast.text or " "),
            keyboard=keyboard,
            attachments=attachments,
        )
        return _extract_max_mid(payload)

    async def process_broadcast(self, broadcast_id: int) -> None:
        async with SessionLocal() as session:
            from app.models import Broadcast
            broadcast = await session.get(Broadcast, broadcast_id)
            if not broadcast or broadcast.status != BROADCAST_SENDING:
                return
            deliveries = await pending_broadcast_deliveries(session, broadcast_id)

        max_media_attachment = None
        if (
            self.max_client and broadcast.media_type in {"image", "video"} and broadcast.media_path
            and any(user.platform == "max" for _, user in deliveries)
            and self.settings.max_marketing_broadcast_enabled
        ):
            try:
                max_media_attachment = await self.max_client.upload_media(broadcast.media_path, broadcast.media_type)
            except Exception:
                log.exception("Could not upload broadcast media to MAX broadcast_id=%s", broadcast_id)

        for delivery, user in deliveries:
            status = "sent"
            error = None
            message_id = None
            try:
                if user.platform == "telegram":
                    message_id = await self._send_telegram_broadcast(user, broadcast)
                elif user.platform == "max":
                    if broadcast.media_type and broadcast.media_path and max_media_attachment is None:
                        raise RuntimeError("MAX media upload unavailable")
                    message_id = await self._send_max_broadcast(user, broadcast, media_attachment=max_media_attachment)
                else:
                    status = "skipped"
                    error = f"Unsupported platform: {user.platform}"
            except PermissionError as exc:
                status = "skipped"
                error = str(exc)
            except Exception as exc:
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
                log.exception("Broadcast delivery failed broadcast_id=%s platform=%s user_id=%s",
                              broadcast_id, user.platform, user.platform_user_id)

            async with SessionLocal() as session:
                from app.models import BroadcastDelivery
                current = await session.get(BroadcastDelivery, delivery.id)
                if current:
                    await mark_delivery(session, current, status=status, error=error, message_id=message_id)
                    await session.commit()
            await asyncio.sleep(self.settings.broadcast_delay_seconds)

        async with SessionLocal() as session:
            from app.models import Broadcast
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast and broadcast.status != BROADCAST_CANCELLED:
                await finalize_broadcast(session, broadcast)
                await session.commit()
                log.info("Broadcast completed id=%s sent=%s failed=%s skipped=%s",
                         broadcast.id, broadcast.sent_count, broadcast.failed_count, broadcast.skipped_count)

    async def broadcast_worker(self) -> None:
        log.info("Broadcast worker started")
        while True:
            try:
                async with SessionLocal() as session:
                    item = await next_queued_broadcast(session)
                    if item:
                        bid = item.id
                        await session.commit()
                    else:
                        bid = None
                if bid is None:
                    await asyncio.sleep(1.0)
                    continue
                await self.process_broadcast(bid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Broadcast worker error")
                await asyncio.sleep(3.0)
