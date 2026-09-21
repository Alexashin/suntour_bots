from dataclasses import dataclass
from typing import Any

STEPS = (
    "destination",
    "dates",
    "travelers",
    "nights",
    "name",
    "contact_method",
    "phone",
)

PROMPTS = {
    "destination": "Куда хотите поехать? 🌎",
    "dates": "Какие даты поездки? 📅",
    "travelers": "Сколько человек? Если есть дети — укажите их возраст 👶",
    "nights": "На сколько ночей планируете поездку? ☀️",
    "name": "Как к вам обращаться?",
    "contact_method": "Как удобнее продолжить общение?",
    "phone": "Оставьте номер телефона, пожалуйста 📞",
}

CONTACT_CHAT = "chat"
CONTACT_PHONE = "phone"


@dataclass(slots=True)
class ScenarioResult:
    reply: str
    next_step: str | None
    data: dict[str, Any]
    completed: bool = False


def start_scenario() -> ScenarioResult:
    return ScenarioResult(
        reply="Здравствуйте! Поможем подобрать тур 😊\n\n" + PROMPTS["destination"],
        next_step="destination",
        data={},
    )


def consume(step: str, text: str, data: dict[str, Any]) -> ScenarioResult:
    value = text.strip()
    if not value:
        return ScenarioResult(PROMPTS[step], step, data)

    new_data = dict(data)

    if step == "destination":
        new_data["destination"] = value
        return ScenarioResult(PROMPTS["dates"], "dates", new_data)

    if step == "dates":
        new_data["travel_dates"] = value
        return ScenarioResult(PROMPTS["travelers"], "travelers", new_data)

    if step == "travelers":
        new_data["travelers"] = value
        return ScenarioResult(PROMPTS["nights"], "nights", new_data)

    if step == "nights":
        new_data["nights"] = value
        return ScenarioResult(PROMPTS["name"], "name", new_data)

    if step == "name":
        new_data["client_name"] = value
        return ScenarioResult(PROMPTS["contact_method"], "contact_method", new_data)

    if step == "contact_method":
        lowered = value.lower()
        if lowered in {"чат", "в чате", "chat"}:
            new_data["contact_method"] = CONTACT_CHAT
            return ScenarioResult(
                "Спасибо! Заявка сформирована. Менеджер продолжит общение с вами в чате 😊",
                None,
                new_data,
                completed=True,
            )
        if lowered in {"телефон", "звонок", "позвонить", "phone"}:
            new_data["contact_method"] = CONTACT_PHONE
            return ScenarioResult(PROMPTS["phone"], "phone", new_data)
        return ScenarioResult(
            "Выберите, пожалуйста: «В чате» или «По телефону».",
            "contact_method",
            new_data,
        )

    if step == "phone":
        new_data["phone"] = value
        return ScenarioResult(
            "Спасибо! Заявка сформирована. Менеджер свяжется с вами по телефону 😊",
            None,
            new_data,
            completed=True,
        )

    return start_scenario()
