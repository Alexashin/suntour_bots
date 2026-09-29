from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Broadcast, BroadcastDelivery, Conversation, Lead, LeadMessage, User, UserEvent
from app.scenario import CONTACT_CHAT, clean_data

LEAD_NEW = "new"
LEAD_IN_PROGRESS = "in_progress"
LEAD_PROCESSED = "processed"
LEAD_CANCELLED = "cancelled"

BROADCAST_DRAFT = "draft"
BROADCAST_QUEUED = "queued"
BROADCAST_SENDING = "sending"
BROADCAST_SENT = "sent"
BROADCAST_CANCELLED = "cancelled"
BROADCAST_FAILED = "failed"


async def upsert_user(
    session: AsyncSession,
    *,
    platform: str,
    platform_user_id: int,
    chat_id: int | None,
    username: str | None = None,
    display_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
    is_premium: bool | None = None,
    last_activity_at: datetime | None = None,
    profile_meta: dict[str, Any] | None = None,
    start_param: str | None = None,
    acquisition_source: str | None = None,
) -> User:
    result = await session.execute(select(User).where(User.platform == platform, User.platform_user_id == platform_user_id))
    user = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if user is None:
        user = User(platform=platform, platform_user_id=platform_user_id, chat_id=chat_id)
        session.add(user)
    user.chat_id = chat_id
    if username is not None:
        user.username = username
    if display_name is not None:
        user.display_name = display_name
    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    if language_code is not None:
        user.language_code = language_code
    if is_premium is not None:
        user.is_premium = is_premium
    if last_activity_at is not None:
        user.last_activity_at = last_activity_at
    if profile_meta:
        current = dict(user.profile_meta or {})
        current.update(profile_meta)
        user.profile_meta = current
    if start_param and not user.start_param:
        user.start_param = start_param[:255]
    if acquisition_source and not user.acquisition_source:
        user.acquisition_source = acquisition_source[:255]
    user.last_seen_at = now
    await session.flush()
    return user


async def log_user_event(session: AsyncSession, user: User, event_type: str, data: dict[str, Any] | None = None) -> UserEvent:
    item = UserEvent(user_id=user.id, event_type=event_type, event_data=data or {})
    session.add(item)
    await session.flush()
    return item


async def get_user(session: AsyncSession, *, platform: str, platform_user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.platform == platform, User.platform_user_id == platform_user_id))


async def get_or_create_conversation(session: AsyncSession, *, platform: str, platform_user_id: int) -> Conversation:
    result = await session.execute(select(Conversation).where(
        Conversation.platform == platform, Conversation.platform_user_id == platform_user_id))
    conv = result.scalar_one_or_none()
    if conv is None:
        conv = Conversation(platform=platform, platform_user_id=platform_user_id, step="idle", data={"_history": []})
        session.add(conv)
        await session.flush()
    return conv


# Интервалы считаются от момента последней активности пользователя / предыдущего напоминания.
# 5 минут -> ещё 30 минут -> ещё 3 часа -> ещё 24 часа, после чего напоминания прекращаются.
WIZARD_REMINDER_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=3),
    timedelta(hours=24),
)


async def reset_conversation(session: AsyncSession, *, platform: str, platform_user_id: int) -> Conversation:
    conv = await get_or_create_conversation(session, platform=platform, platform_user_id=platform_user_id)
    conv.step = "idle"
    conv.data = {"_history": []}
    conv.reminder_stage = 0
    conv.reminder_next_at = None
    conv.reminder_last_sent_at = None
    await session.flush()
    return conv


async def save_conversation(session: AsyncSession, conv: Conversation, *, step: str, data: dict[str, Any]) -> None:
    conv.step = step
    conv.data = data
    await session.flush()


async def arm_wizard_reminder(session: AsyncSession, conv: Conversation, *, reset_stage: bool = True) -> None:
    """Schedule a restart-safe reminder for an unfinished wizard.

    The schedule is stored in PostgreSQL rather than in asyncio.sleep tasks, so a
    process restart or deploy does not lose the reminder state and no bot handler
    waits on a timer.
    """
    if conv.step in {"idle", "complete", "admin_broadcast"}:
        conv.reminder_next_at = None
        await session.flush()
        return
    now = datetime.now(timezone.utc)
    if reset_stage:
        conv.reminder_stage = 0
        conv.reminder_last_sent_at = None
    stage = min(max(int(conv.reminder_stage or 0), 0), len(WIZARD_REMINDER_DELAYS) - 1)
    conv.reminder_next_at = now + WIZARD_REMINDER_DELAYS[stage]
    await session.flush()


async def claim_due_wizard_reminders(
    session: AsyncSession, *, limit: int = 25
) -> list[tuple[int, User, int, str, dict[str, Any]]]:
    """Atomically claim due reminders.

    FOR UPDATE SKIP LOCKED prevents duplicate claims if the app is ever scaled to
    more than one worker. The next schedule is persisted before network I/O.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Conversation, User)
        .join(
            User,
            and_(
                User.platform == Conversation.platform,
                User.platform_user_id == Conversation.platform_user_id,
            ),
        )
        .where(
            Conversation.reminder_next_at.is_not(None),
            Conversation.reminder_next_at <= now,
            Conversation.step.notin_(["idle", "complete", "admin_broadcast"]),
        )
        .order_by(Conversation.reminder_next_at.asc())
        .with_for_update(of=Conversation, skip_locked=True)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    claimed: list[tuple[int, User, int, str, dict[str, Any]]] = []
    for conv, user in rows:
        stage = int(conv.reminder_stage or 0)
        if stage >= len(WIZARD_REMINDER_DELAYS):
            conv.reminder_next_at = None
            continue
        claimed.append((conv.id, user, stage, conv.step, dict(conv.data or {})))
        conv.reminder_stage = stage + 1
        conv.reminder_last_sent_at = now
        if conv.reminder_stage < len(WIZARD_REMINDER_DELAYS):
            conv.reminder_next_at = now + WIZARD_REMINDER_DELAYS[conv.reminder_stage]
        else:
            conv.reminder_next_at = None
    await session.flush()
    return claimed


def _lead_contact_snapshot(user: User, contact_method: str, phone: str | None) -> tuple[str | None, str | None, str]:
    if contact_method != CONTACT_CHAT and phone:
        return phone, f"tel:{phone}", "phone"
    if user.platform == "telegram":
        if user.username:
            return f"@{user.username}", f"https://t.me/{user.username}", "telegram"
        return user.display_name or "Telegram", f"tg://user?id={user.platform_user_id}", "telegram"
    if user.platform == "max":
        label = f"@{user.username}" if user.username else (user.display_name or "Пользователь MAX")
        return label, f"max://user/{user.platform_user_id}", "max"
    return user.display_name, None, user.platform


async def create_lead(session: AsyncSession, *, user: User, data: dict[str, Any]) -> Lead:
    clean = clean_data(data)
    phone = clean.get("phone")
    if phone:
        user.phone = str(phone)
    travelers = f"Взрослых: {clean.get('adults', '—')}; детей: {clean.get('children_count', '—')}"
    if clean.get("children_ages"):
        travelers += f"; возраст: {clean['children_ages']}"
    contact_method = str(clean.get("contact_method", CONTACT_CHAT))
    contact_value, contact_url, contact_platform = _lead_contact_snapshot(user, contact_method, str(phone) if phone else None)
    lead = Lead(
        user_id=user.id,
        destination=str(clean.get("destination", "")),
        travel_dates=str(clean.get("travel_dates", "")),
        travelers=travelers,
        nights=str(clean.get("nights", "")),
        client_name=str(clean.get("client_name", user.display_name or "Клиент")),
        contact_method=contact_method,
        phone=str(phone) if phone else None,
        contact_value=contact_value,
        contact_url=contact_url,
        contact_platform=contact_platform,
        raw_payload=clean,
        status=LEAD_NEW,
    )
    session.add(lead)
    await session.flush()
    await log_user_event(session, user, "lead_created", {"lead_id": lead.id, "destination": lead.destination, "departure_city": clean.get("departure_city")})
    return lead


def _status_block(lead: Lead) -> str:
    if lead.status == LEAD_PROCESSED:
        who = escape(lead.processed_by_name or "менеджер")
        when = lead.processed_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M") if lead.processed_at else ""
        return f"\n\n✅ <b>ОБРАБОТАНА</b>\n👤 {who}" + (f"\n🕒 {when}" if when else "")
    if lead.status == LEAD_IN_PROGRESS:
        return "\n\n🟡 <b>В РАБОТЕ</b>"
    if lead.status == LEAD_CANCELLED:
        return "\n\n⚫ <b>ОТМЕНЕНА</b>"
    return "\n\n🆕 <b>НОВАЯ</b>"


def _contact_block(lead: Lead, user: User, render_platform: str) -> str:
    if lead.contact_method != CONTACT_CHAT:
        phone = escape(lead.phone or user.phone or "не указан")
        return f"☎️ Связь: телефон <b>{phone}</b>"

    name = escape(lead.contact_value or user.display_name or "клиент")
    if user.platform == "telegram":
        if user.username:
            url = f"https://t.me/{user.username}"
            # В MAX не показываем сырой @username: клиент MAX может интерпретировать
            # его как упоминание внутреннего пользователя MAX. Вместо этого всегда
            # показываем обычный HTML-link на публичный профиль Telegram.
            if render_platform == "max":
                return f'💬 Связь: Telegram — <a href="{url}">ссылка</a>'
            return f'💬 Связь: Telegram — <a href="{url}">{name}</a>'
        if render_platform == "telegram":
            return f'💬 Связь: Telegram — <a href="tg://user?id={user.platform_user_id}">{name}</a>'
        return "💬 Связь: Telegram — публичная ссылка недоступна\n<i>Открыть диалог можно из копии заявки в Telegram.</i>"

    if user.platform == "max":
        if render_platform == "max":
            full_name = escape(user.display_name or lead.client_name or "Клиент")
            return f'💬 Связь: MAX — <a href="max://user/{user.platform_user_id}">{full_name}</a>'
        handle = f"@{escape(user.username)}" if user.username else escape(user.display_name or "пользователь MAX")
        return f"💬 Связь: MAX — {handle}\n<i>Открыть диалог удобнее из копии заявки в MAX.</i>"

    return f"💬 Связь: {name}"


def format_lead(lead: Lead, user: User, *, render_platform: str = "telegram") -> str:
    d = lead.raw_payload or {}
    platform_label = "Telegram" if user.platform == "telegram" else "MAX"
    children = str(d.get("children_count", "—"))
    if d.get("children_ages"):
        children += f" (возраст: {escape(str(d['children_ages']))})"
    if user.platform == "telegram" and user.username and render_platform == "max":
        # Не оставляем @username в тексте MAX: MAX превращает его в локальное mention.
        account = f'<a href="https://t.me/{escape(user.username)}">ссылка на Telegram</a>'
    else:
        account = f"@{escape(user.username)}" if user.username else escape(user.display_name or "без публичного username")
    return (
        f"🔥 <b>Заявка #{lead.id} на подбор тура</b>\n\n"
        f"Источник: {platform_label}\nКлиент: {escape(lead.client_name)}\nПрофиль: {account}\n"
        f"✈️ Вылет: {escape(str(d.get('departure_city', '—')))}\n🌍 Направление: {escape(str(d.get('destination', '—')))}\n"
        f"👤 Взрослых: {escape(str(d.get('adults', '—')))}\n👶 Дети: {children}\n"
        f"🌙 Ночей: {escape(str(d.get('nights', '—')))}\n📅 Даты: {escape(str(d.get('travel_dates', '—')))}\n"
        f"{_contact_block(lead, user, render_platform)}"
        + _status_block(lead)
    )


async def get_lead(session: AsyncSession, lead_id: int) -> Lead | None:
    return await session.get(Lead, lead_id)


async def get_lead_and_user(session: AsyncSession, lead_id: int) -> tuple[Lead, User] | None:
    row = (await session.execute(select(Lead, User).join(User, User.id == Lead.user_id).where(Lead.id == lead_id))).first()
    return (row[0], row[1]) if row else None


async def set_lead_status(session: AsyncSession, *, lead_id: int, status: str, actor_platform: str,
                          actor_user_id: int, actor_name: str | None) -> Lead | None:
    lead = await session.get(Lead, lead_id)
    if not lead:
        return None
    lead.status = status
    if status == LEAD_PROCESSED:
        lead.processed_at = datetime.now(timezone.utc)
        lead.processed_by_platform = actor_platform
        lead.processed_by_user_id = actor_user_id
        lead.processed_by_name = actor_name
    elif status == LEAD_NEW:
        lead.processed_at = None
        lead.processed_by_platform = None
        lead.processed_by_user_id = None
        lead.processed_by_name = None
    await session.flush()
    return lead


async def add_lead_message(session: AsyncSession, *, lead_id: int, platform: str,
                           chat_id: int | None, message_id: str) -> LeadMessage:
    item = LeadMessage(lead_id=lead_id, platform=platform, chat_id=chat_id, message_id=message_id)
    session.add(item)
    await session.flush()
    return item


async def get_lead_messages(session: AsyncSession, lead_id: int) -> list[LeadMessage]:
    result = await session.execute(select(LeadMessage).where(LeadMessage.lead_id == lead_id))
    return list(result.scalars().all())


async def stats(session: AsyncSession) -> dict[str, int]:
    users = await session.scalar(select(func.count(User.id)))
    leads = await session.scalar(select(func.count(Lead.id)))
    subscribers = await session.scalar(select(func.count(User.id)).where(User.subscribed.is_(True)))
    tg_subscribers = await session.scalar(select(func.count(User.id)).where(User.subscribed.is_(True), User.platform == "telegram"))
    max_subscribers = await session.scalar(select(func.count(User.id)).where(User.subscribed.is_(True), User.platform == "max"))
    new_leads = await session.scalar(select(func.count(Lead.id)).where(Lead.status == LEAD_NEW))
    processed_leads = await session.scalar(select(func.count(Lead.id)).where(Lead.status == LEAD_PROCESSED))
    return {
        "users": int(users or 0), "leads": int(leads or 0), "subscribers": int(subscribers or 0),
        "tg_subscribers": int(tg_subscribers or 0), "max_subscribers": int(max_subscribers or 0),
        "new_leads": int(new_leads or 0), "processed_leads": int(processed_leads or 0),
    }


async def recent_new_leads(session: AsyncSession, limit: int = 10) -> list[Lead]:
    result = await session.execute(select(Lead).where(Lead.status == LEAD_NEW).order_by(Lead.created_at.desc()).limit(limit))
    return list(result.scalars().all())


async def subscribed_users(session: AsyncSession, target: str = "both") -> list[User]:
    stmt = select(User).where(User.subscribed.is_(True))
    if target in {"telegram", "max"}:
        stmt = stmt.where(User.platform == target)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def set_subscription(session: AsyncSession, user: User, *, enabled: bool) -> bool:
    """Idempotently change subscription state.

    Starting the tour wizard can happen many times. If a user is already
    subscribed we do not rewrite subscribed_at or create duplicate events. If
    they previously unsubscribed, starting a new tour subscribes them again.
    """
    if bool(user.subscribed) == enabled:
        return False
    now = datetime.now(timezone.utc)
    user.subscribed = enabled
    if enabled:
        user.subscribed_at = now
        user.unsubscribed_at = None
    else:
        user.unsubscribed_at = now
    await log_user_event(session, user, "subscribed" if enabled else "unsubscribed")
    await session.flush()
    return True


async def create_broadcast(
    session: AsyncSession,
    *,
    platform: str,
    user_id: int,
    name: str | None,
    target: str,
    text: str,
    media_type: str | None = None,
    media_path: str | None = None,
    media_source_platform: str | None = None,
    media_source_id: str | None = None,
) -> Broadcast:
    item = Broadcast(
        created_by_platform=platform,
        created_by_user_id=user_id,
        created_by_name=name,
        target=target,
        text=text,
        media_type=media_type,
        media_path=media_path,
        media_source_platform=media_source_platform,
        media_source_id=media_source_id,
        status=BROADCAST_DRAFT,
    )
    session.add(item)
    await session.flush()
    return item


async def update_broadcast_link(session: AsyncSession, broadcast: Broadcast, *, url: str | None, text: str | None = None) -> None:
    broadcast.button_url = url
    broadcast.button_text = (text or "Подробнее")[:64] if url else None
    await session.flush()


async def get_broadcast(session: AsyncSession, broadcast_id: int) -> Broadcast | None:
    return await session.get(Broadcast, broadcast_id)


async def queue_broadcast(session: AsyncSession, broadcast: Broadcast) -> None:
    if broadcast.status != BROADCAST_DRAFT:
        raise ValueError("Broadcast is not a draft")
    users = await subscribed_users(session, broadcast.target)
    broadcast.total_recipient_count = len(users)
    broadcast.status = BROADCAST_QUEUED
    for user in users:
        session.add(BroadcastDelivery(broadcast_id=broadcast.id, user_id=user.id, platform=user.platform, status="pending"))
    await session.flush()


async def next_queued_broadcast(session: AsyncSession) -> Broadcast | None:
    stmt = select(Broadcast).where(Broadcast.status.in_([BROADCAST_QUEUED, BROADCAST_SENDING])).order_by(Broadcast.created_at.asc()).limit(1)
    item = await session.scalar(stmt)
    if item and item.status == BROADCAST_QUEUED:
        item.status = BROADCAST_SENDING
        item.started_at = datetime.now(timezone.utc)
        await session.flush()
    return item


async def pending_broadcast_deliveries(session: AsyncSession, broadcast_id: int) -> list[tuple[BroadcastDelivery, User]]:
    rows = await session.execute(
        select(BroadcastDelivery, User)
        .join(User, User.id == BroadcastDelivery.user_id)
        .where(BroadcastDelivery.broadcast_id == broadcast_id, BroadcastDelivery.status == "pending")
        .order_by(BroadcastDelivery.id.asc())
    )
    return [(row[0], row[1]) for row in rows]


async def mark_delivery(session: AsyncSession, delivery: BroadcastDelivery, *, status: str, error: str | None = None, message_id: str | None = None) -> None:
    delivery.status = status
    delivery.error = error[:4000] if error else None
    delivery.platform_message_id = message_id
    if status == "sent":
        delivery.sent_at = datetime.now(timezone.utc)
    await session.flush()


async def finalize_broadcast(session: AsyncSession, broadcast: Broadcast) -> None:
    rows = await session.execute(
        select(BroadcastDelivery.status, func.count(BroadcastDelivery.id))
        .where(BroadcastDelivery.broadcast_id == broadcast.id)
        .group_by(BroadcastDelivery.status)
    )
    counts = {status: int(count) for status, count in rows.all()}
    broadcast.sent_count = counts.get("sent", 0)
    broadcast.failed_count = counts.get("failed", 0)
    broadcast.skipped_count = counts.get("skipped", 0)
    broadcast.status = BROADCAST_SENT if broadcast.failed_count == 0 else BROADCAST_SENT
    broadcast.sent_at = datetime.now(timezone.utc)
    await session.flush()


async def cancel_broadcast(session: AsyncSession, broadcast: Broadcast) -> None:
    if broadcast.status in {BROADCAST_DRAFT, BROADCAST_QUEUED}:
        broadcast.status = BROADCAST_CANCELLED
        await session.flush()
