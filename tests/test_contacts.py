from app.models import Lead, User
from app.services import _lead_contact_snapshot, format_lead


def test_telegram_contact_snapshot_prefers_public_username():
    user = User(platform="telegram", platform_user_id=123, username="traveler", display_name="Иван")
    value, url, platform = _lead_contact_snapshot(user, "chat", None)
    assert value == "@traveler"
    assert url == "https://t.me/traveler"
    assert platform == "telegram"


def test_max_contact_is_clickable_inside_max():
    user = User(platform="max", platform_user_id=456, username=None, display_name="Анна")
    lead = Lead(
        id=1,
        user_id=1,
        destination="Турция",
        travel_dates="июнь",
        travelers="2",
        nights="7",
        client_name="Анна",
        contact_method="chat",
        raw_payload={"departure_city": "Москва", "destination": "Турция"},
        status="new",
    )
    text = format_lead(lead, user, render_platform="max")
    assert 'max://user/456' in text


def test_telegram_contact_rendered_for_max_uses_external_link_not_mention():
    user = User(platform="telegram", platform_user_id=123, username="traveler", display_name="Иван")
    lead = Lead(
        id=2,
        user_id=1,
        destination="Турция",
        travel_dates="июнь",
        travelers="2",
        nights="7",
        client_name="Иван",
        contact_method="chat",
        contact_value="@traveler",
        contact_url="https://t.me/traveler",
        contact_platform="telegram",
        raw_payload={"departure_city": "Москва", "destination": "Турция"},
        status="new",
    )
    text = format_lead(lead, user, render_platform="max")
    assert '<a href="https://t.me/traveler">ссылка</a>' in text
    assert '<a href="https://t.me/traveler">ссылка на Telegram</a>' in text
    assert '@traveler' not in text
