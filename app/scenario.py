from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

CONTACT_CHAT = "chat"
CONTACT_PHONE = "phone"

MAIN_ACTION_START = "🧳 Подобрать тур"
MAIN_ACTION_SUBSCRIBE = "🔥 Подписаться на рассылку"
MAIN_ACTION_UNSUBSCRIBE = "🔕 Отписаться от рассылки"
MAIN_ACTION_CONTACTS = "ℹ️ Контакты"

# review оставлен для совместимости с незавершёнными заявками старой версии.
# Новые заявки после последнего ответа отправляются автоматически.
STEP_ORDER = (
    "name",
    "destination",
    "departure_city",
    "adults",
    "children_count",
    "children_ages",
    "nights",
    "dates",
    "contact_method",
    "phone",
    "review",
)

STEP_TITLES = {
    "name": "Имя",
    "destination": "Направление",
    "departure_city": "Город вылета",
    "adults": "Взрослые",
    "children_count": "Дети",
    "children_ages": "Возраст детей",
    "nights": "Ночи",
    "dates": "Даты",
    "contact_method": "Связь",
    "phone": "Телефон",
    "review": "Проверка заявки",
}

PROMPTS = {
    "name": "Как я могу к Вам обращаться? 🙂",
    "destination": "Куда хотите отправиться? 🌍",
    "departure_city": "Откуда планируете вылет? ✈️\nВыберите вариант или напишите свой город.",
    "adults": "Сколько взрослых едет? 👤",
    "children_count": "Сколько детей едет с вами? 👶",
    "children_ages": "Укажите возраст детей через запятую.\nНапример: 5, 9",
    "nights": "На какое количество ночей планируете поездку? 🌙\nМожно выбрать вариант или написать своё значение.",
    "dates": "Когда хотите поехать? 📅\nНапишите точные или примерные даты либо выберите вариант.",
    "contact_method": "Как удобнее продолжить общение?",
    "phone": "Оставьте номер телефона 📞\nМожно отправить его кнопкой или ввести вручную.",
}


@dataclass(frozen=True, slots=True)
class Choice:
    text: str
    code: str
    value: str


@dataclass(slots=True)
class ScenarioResult:
    reply: str
    next_step: str
    data: dict[str, Any]
    validation_error: bool = False


def clean_data(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if not k.startswith("_")}


def start_scenario() -> ScenarioResult:
    data: dict[str, Any] = {"_history": []}
    return ScenarioResult(render_step("name", data), "name", data)


def _progress(step: str) -> str:
    visible = [s for s in STEP_ORDER if s not in {"children_ages", "phone", "review"}]
    if step in visible:
        idx = visible.index(step) + 1
        return f"Шаг {idx} из {len(visible)}"
    if step == "children_ages":
        return "Уточняем состав туристов"
    if step == "phone":
        return "Контакт для связи"
    return "Подбор тура"


def render_step(step: str, data: dict[str, Any]) -> str:
    if step == "review":
        return render_review(data)
    title = STEP_TITLES.get(step, "Подбор тура")
    prompt = PROMPTS.get(step, "Введите значение")
    # Имя спрашиваем первым и сразу используем дальше, но без навязчивого
    # повторения на каждом шаге.
    if step == "destination" and data.get("client_name"):
        prompt = f"{escape(str(data['client_name']))}, куда хотите отправиться? 🌍"
    return f"🧳 Подбор тура · {_progress(step)}\n\n<b>{title}</b>\n{prompt}"


def choice_rows(step: str, data: dict[str, Any], *, display_name: str | None = None) -> list[list[Choice]]:
    if step == "departure_city":
        return [
            [Choice("Москва", "msk", "Москва"), Choice("Минеральные Воды", "mrw", "Минеральные Воды")],
            [Choice("Краснодар", "krr", "Краснодар")],
            [Choice("Другой город", "manual", "")],
        ]
    if step == "destination":
        return [
            [Choice("🇹🇷 Турция", "tr", "Турция"), Choice("🇪🇬 Египет", "eg", "Египет")],
            [Choice("🇹🇭 Таиланд", "th", "Таиланд"), Choice("🇲🇻 Мальдивы", "mv", "Мальдивы")],
            [Choice("🇨🇳 Китай", "cn", "Китай"), Choice("🇱🇰 Шри-Ланка", "lk", "Шри-Ланка")],
            [Choice("🇷🇺 Россия", "ru", "Россия")],
            [Choice("💬 Нужна консультация", "consult", "Нужна консультация")],
        ]
    if step == "adults":
        return [[Choice(str(n), str(n), str(n)) for n in (1, 2, 3, 4)]]
    if step == "children_count":
        return [
            [Choice("Без детей", "0", "0"), Choice("1 ребёнок", "1", "1")],
            [Choice("2 ребёнка", "2", "2"), Choice("3+ детей", "3plus", "3+")],
        ]
    if step == "nights":
        return [[
            Choice("6–8", "6_8", "6–8"),
            Choice("9–11", "9_11", "9–11"),
            Choice("12–14", "12_14", "12–14"),
        ]]
    if step == "dates":
        return [
            [Choice("В ближайшие 2 недели", "2w", "В ближайшие 2 недели")],
            [Choice("В течение месяца", "1m", "В течение месяца"), Choice("Даты не определены", "unknown", "Даты не определены")],
        ]
    if step == "name" and display_name:
        return [[Choice(f"Оставить «{display_name}»", "profile", display_name)]]
    if step == "contact_method":
        return [[Choice("💬 В чате", "chat", CONTACT_CHAT), Choice("📞 По телефону", "phone", CONTACT_PHONE)]]
    return []


def resolve_choice(step: str, code: str, data: dict[str, Any], *, display_name: str | None = None) -> str | None:
    for row in choice_rows(step, data, display_name=display_name):
        for choice in row:
            if choice.code == code:
                return choice.value
    return None


def _is_zero_children(value: str) -> bool:
    lowered = value.strip().casefold()
    return lowered in {"0", "без детей", "нет", "не будет"}


def _display_contact_method(value: Any) -> str:
    if value == CONTACT_CHAT:
        return "в чате"
    if value == CONTACT_PHONE:
        return "по телефону"
    return str(value or "—")


def render_review(data: dict[str, Any]) -> str:
    """Компактное ревью для старых незавершённых диалогов и ручного редактирования."""
    d = clean_data(data)

    def e(value: Any) -> str:
        return escape(str(value if value not in (None, "") else "—"))

    children = str(d.get("children_count", "—"))
    ages = d.get("children_ages")
    if ages and not _is_zero_children(children):
        children = f"{children} (возраст: {ages})"

    phone = d.get("phone")
    contact = _display_contact_method(d.get("contact_method"))
    if phone:
        contact += f" · {phone}"

    return (
        "✅ <b>Проверьте заявку</b>\n\n"
        f"🙂 Имя: <b>{e(d.get('client_name'))}</b>\n"
        f"🌍 Направление: <b>{e(d.get('destination'))}</b>\n"
        f"✈️ Город вылета: <b>{e(d.get('departure_city'))}</b>\n"
        f"👤 Взрослых: <b>{e(d.get('adults'))}</b>\n"
        f"👶 Дети: <b>{e(children)}</b>\n"
        f"🌙 Ночей: <b>{e(d.get('nights'))}</b>\n"
        f"📅 Даты: <b>{e(d.get('travel_dates'))}</b>\n"
        f"☎️ Связь: <b>{e(contact)}</b>\n\n"
        "Если всё верно — отправьте заявку."
    )


def review_edit_rows() -> list[list[Choice]]:
    return [
        [Choice("🙂 Имя", "name", "name"), Choice("🌍 Направление", "destination", "destination")],
        [Choice("✈️ Вылет", "departure_city", "departure_city"), Choice("👤 Взрослые", "adults", "adults")],
        [Choice("👶 Дети", "children_count", "children_count"), Choice("🌙 Ночи", "nights", "nights")],
        [Choice("📅 Даты", "dates", "dates"), Choice("☎️ Связь", "contact_method", "contact_method")],
    ]


def edit_step(data: dict[str, Any], step: str) -> ScenarioResult:
    if step not in STEP_ORDER or step == "review":
        return ScenarioResult(render_review(data), "review", data, validation_error=True)
    new_data = dict(data)
    new_data["_editing"] = True
    return ScenarioResult(render_step(step, new_data), step, new_data)


def go_back(step: str, data: dict[str, Any]) -> ScenarioResult:
    new_data = dict(data)
    if new_data.get("_editing"):
        new_data.pop("_editing", None)
        return ScenarioResult(render_review(new_data), "review", new_data)

    history = list(new_data.get("_history") or [])
    if not history:
        return ScenarioResult(render_step(step, new_data), step, new_data)
    previous = history.pop()
    new_data["_history"] = history
    return ScenarioResult(render_step(previous, new_data), previous, new_data)


def _advance(step: str, data: dict[str, Any]) -> str:
    editing = bool(data.get("_editing"))

    if step == "name":
        next_step = "destination"
    elif step == "destination":
        next_step = "departure_city"
    elif step == "departure_city":
        next_step = "adults"
    elif step == "adults":
        next_step = "children_count"
    elif step == "children_count":
        next_step = "nights" if _is_zero_children(str(data.get("children_count", ""))) else "children_ages"
    elif step == "children_ages":
        next_step = "nights"
    elif step == "nights":
        next_step = "dates"
    elif step == "dates":
        next_step = "contact_method"
    elif step == "contact_method":
        next_step = "phone" if data.get("contact_method") == CONTACT_PHONE else "complete"
    elif step == "phone":
        next_step = "complete"
    else:
        next_step = "complete"

    if editing:
        # Для зависимых шагов сначала собираем необходимое продолжение, затем возвращаемся к ревью.
        if step == "children_count" and next_step == "children_ages":
            return next_step
        if step == "contact_method" and next_step == "phone":
            return next_step
        if step in {"children_ages", "phone"} or next_step not in {"children_ages", "phone"}:
            data.pop("_editing", None)
            return "review"

    return next_step


def consume(step: str, value: str, data: dict[str, Any]) -> ScenarioResult:
    value = value.strip()
    if not value:
        return ScenarioResult(render_step(step, data), step, data, validation_error=True)

    new_data = dict(data)
    history = list(new_data.get("_history") or [])

    field_map = {
        "name": "client_name",
        "destination": "destination",
        "departure_city": "departure_city",
        "adults": "adults",
        "children_count": "children_count",
        "children_ages": "children_ages",
        "nights": "nights",
        "dates": "travel_dates",
        "phone": "phone",
    }

    if step == "contact_method":
        lowered = value.casefold()
        if value in {CONTACT_CHAT, CONTACT_PHONE}:
            normalized = value
        elif lowered in {"чат", "в чате", "chat", "💬 в чате"}:
            normalized = CONTACT_CHAT
        elif lowered in {"телефон", "по телефону", "звонок", "позвонить", "phone", "📞 по телефону"}:
            normalized = CONTACT_PHONE
        else:
            return ScenarioResult(render_step(step, new_data) + "\n\n⚠️ Выберите чат или телефон.", step, new_data, True)
        new_data["contact_method"] = normalized
    elif step in field_map:
        new_data[field_map[step]] = value
    else:
        return ScenarioResult(render_step(step, new_data), step, new_data, True)

    if not new_data.get("_editing"):
        history.append(step)
        new_data["_history"] = history

    next_step = _advance(step, new_data)
    if next_step == "review":
        return ScenarioResult(render_review(new_data), "review", new_data)
    if next_step == "complete":
        return ScenarioResult("", "complete", new_data)
    return ScenarioResult(render_step(next_step, new_data), next_step, new_data)
