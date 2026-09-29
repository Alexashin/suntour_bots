import asyncio
from datetime import datetime, timezone

from app.models import User
from app.services import set_subscription


class FakeSession:
    def __init__(self):
        self.added = []
        self.flush_count = 0

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flush_count += 1


def test_repeated_auto_subscription_is_idempotent():
    old_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    user = User(
        id=1,
        platform="telegram",
        platform_user_id=123,
        chat_id=123,
        subscribed=True,
        subscribed_at=old_time,
    )
    session = FakeSession()

    changed = asyncio.run(set_subscription(session, user, enabled=True))

    assert changed is False
    assert user.subscribed is True
    assert user.subscribed_at == old_time
    assert session.added == []
    assert session.flush_count == 0


def test_new_tour_resubscribes_after_manual_unsubscribe():
    user = User(
        id=1,
        platform="telegram",
        platform_user_id=123,
        chat_id=123,
        subscribed=False,
        unsubscribed_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )
    session = FakeSession()

    changed = asyncio.run(set_subscription(session, user, enabled=True))

    assert changed is True
    assert user.subscribed is True
    assert user.subscribed_at is not None
    assert user.unsubscribed_at is None
    assert len(session.added) == 1  # UserEvent("subscribed")
