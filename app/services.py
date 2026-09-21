from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Lead, User
from app.scenario import CONTACT_CHAT, clean_data


async def upsert_user(
    session: AsyncSession,
    *,
    platform: str,
    platform_user_id: int,
    chat_id: int | None,
    username: str | None = None,
    display_name: str | None = None,
) -> User:
    result = await session.execute(
        select(User).where(User.platform == platform, User.platform_user_id == platform_user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            platform=platform,
            platform_user_id=platform_user_id,
            chat_id=chat_id,
            username=username,
            display_name=display_name,
        )
        session.add(user)
    else:
        user.chat_id = chat_id
        if username:
            user.username = username
        if display_name:
            user.display_name = display_name
    await session.flush()
    return user


async def get_or_create_conversation(session: AsyncSession, *, platform: str, platform_user_id: int) -> Conversation:
    result = await session.execute(
        select(Conversation).where(
            Conversation.platform == platform,
            Conversation.platform_user_id == platform_user_id,
        )
    )
    conv = result.scalar_one_or_none()
    if conv is None:
        conv = Conversation(platform=platform, platform_user_id=platform_user_id, step="departure_city", data={"_history": []})
        session.add(conv)
        await session.flush()
    return conv


async def reset_conversation(session: AsyncSession, *, platform: str, platform_user_id: int) -> Conversation:
    conv = await get_or_create_conversation(session, platform=platform, platform_user_id=platform_user_id)
    conv.step = "departure_city"
    conv.data = {"_history": []}
    await session.flush()
    return conv


async def save_conversation(session: AsyncSession, conv: Conversation, *, step: str, data: dict[str, Any]) -> None:
    conv.step = step
    conv.data = data
    await session.flush()


async def create_lead(session: AsyncSession, *, user: User, data: dict[str, Any]) -> Lead:
    clean = clean_data(data)
    phone = clean.get("phone")
    if phone:
        user.phone = str(phone)

    travelers = f"Взрослых: {clean.get('adults', '—')}; детей: {clean.get('children_count', '—')}"
    if clean.get("children_ages"):
        travelers += f"; возраст: {clean['children_ages']}"

    lead = Lead(
        user_id=user.id,
        destination=str(clean.get("destination", "")),
        travel_dates=str(clean.get("travel_dates", "")),
        travelers=travelers,
        nights=str(clean.get("nights", "")),
        client_name=str(clean.get("client_name", user.display_name or "Клиент")),
        contact_method=str(clean.get("contact_method", CONTACT_CHAT)),
        phone=str(phone) if phone else None,
        raw_payload=clean,
    )
    session.add(lead)
    await session.flush()
    return lead


def format_lead(lead: Lead, user: User) -> str:
    d = lead.raw_payload or {}
    platform_label = "Telegram" if user.platform == "telegram" else "MAX"
    account = f"@{user.username}" if user.username else f"ID {user.platform_user_id}"
    children = str(d.get("children_count", "—"))
    if d.get("children_ages"):
        children += f" (возраст: {d['children_ages']})"
    contact = "чат" if lead.contact_method == "chat" else f"телефон: {lead.phone or 'не указан'}"

    return (
        "🔥 Новая заявка на подбор тура\n\n"
        f"Источник: {platform_label}\n"
        f"Клиент: {lead.client_name}\n"
        f"Аккаунт: {account}\n"
        f"✈️ Вылет: {d.get('departure_city', '—')}\n"
        f"🌍 Направление: {d.get('destination', '—')}\n"
        f"👤 Взрослых: {d.get('adults', '—')}\n"
        f"👶 Дети: {children}\n"
        f"🏨 Отель: {d.get('hotel_category', '—')}\n"
        f"🍽 Питание: {d.get('meal_plan', '—')}\n"
        f"🌙 Ночей: {d.get('nights', '—')}\n"
        f"📅 Даты: {d.get('travel_dates', '—')}\n"
        f"☎️ Связь: {contact}"
    )


async def stats(session: AsyncSession) -> dict[str, int]:
    users = await session.scalar(select(func.count(User.id)))
    leads = await session.scalar(select(func.count(Lead.id)))
    subscribers = await session.scalar(select(func.count(User.id)).where(User.subscribed.is_(True)))
    return {"users": int(users or 0), "leads": int(leads or 0), "subscribers": int(subscribers or 0)}


async def subscribed_users(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User).where(User.subscribed.is_(True)))
    return list(result.scalars().all())


async def set_subscription(session: AsyncSession, user: User, *, enabled: bool) -> None:
    now = datetime.now(timezone.utc)
    user.subscribed = enabled
    if enabled:
        user.subscribed_at = now
        user.unsubscribed_at = None
    else:
        user.unsubscribed_at = now
    await session.flush()
