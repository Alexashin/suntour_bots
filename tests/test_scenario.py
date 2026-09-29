from app.scenario import (
    STEP_ORDER,
    choice_rows,
    consume,
    edit_step,
    go_back,
    render_review,
    render_step,
    resolve_choice,
    start_scenario,
)


def test_scenario_starts_with_name_and_reaches_complete():
    result = start_scenario()
    assert result.next_step == "name"

    data = result.data
    step = result.next_step
    values = [
        "Артём",          # name
        "Турция",         # destination
        "Москва",         # departure_city
        "2",              # adults
        "2",              # children_count
        "5, 9",           # children_ages
        "9–11",           # nights
        "10–20 октября",  # dates
        "chat",           # contact_method
    ]

    for value in values:
        result = consume(step, value, data)
        data = result.data
        step = result.next_step

    assert step == "complete"
    assert data["client_name"] == "Артём"
    assert data["children_ages"] == "5, 9"


def test_name_is_used_in_next_prompt():
    result = consume("name", "Алёна", {"_history": []})
    assert result.next_step == "destination"
    assert "Алёна, куда хотите отправиться?" in result.reply


def test_phone_flow_requires_phone_before_complete():
    data = {"_history": []}
    result = consume("contact_method", "phone", data)
    assert result.next_step == "phone"

    result = consume("phone", "+79050505172", result.data)
    assert result.next_step == "complete"
    assert result.data["phone"] == "+79050505172"


def test_no_children_skips_age_step():
    result = consume("children_count", "0", {"_history": []})
    assert result.next_step == "nights"


def test_choice_resolution():
    value = resolve_choice("destination", "tr", {"_history": []})
    assert value == "Турция"


def test_legacy_edit_returns_to_review_after_one_field():
    data = {
        "_history": [],
        "client_name": "Артём",
        "destination": "Турция",
        "departure_city": "Москва",
        "adults": "2",
        "children_count": "0",
        "nights": "9–11",
        "travel_dates": "октябрь",
        "contact_method": "chat",
    }
    edit = edit_step(data, "nights")
    assert edit.next_step == "nights"
    changed = consume("nights", "12–14", edit.data)
    assert changed.next_step == "review"
    assert changed.data["nights"] == "12–14"
    assert "12–14" in render_review(changed.data)


def test_back_uses_new_history_order():
    data = {
        "_history": ["name", "destination"],
        "client_name": "Артём",
        "destination": "Турция",
    }
    result = go_back("departure_city", data)
    assert result.next_step == "destination"


def test_departure_city_choices_are_customer_list():
    labels = [choice.value for row in choice_rows("departure_city", {"_history": []}) for choice in row]
    assert labels == ["Москва", "Минеральные Воды", "Краснодар", ""]


def test_destination_choices_are_customer_list():
    labels = [choice.value for row in choice_rows("destination", {"_history": []}) for choice in row]
    assert labels == [
        "Турция", "Египет", "Таиланд", "Мальдивы", "Китай", "Шри-Ланка", "Россия", "Нужна консультация"
    ]
    assert "Вьетнам" not in labels


def test_removed_questions_and_nights_any_option_are_gone():
    assert "hotel_category" not in STEP_ORDER
    assert "meal_plan" not in STEP_ORDER
    nights = [choice.value for row in choice_rows("nights", {}) for choice in row]
    assert "Не важно" not in nights


def test_render_review_is_compact():
    text = render_review({
        "client_name": "Алёна",
        "destination": "Мальдивы",
        "departure_city": "Москва",
        "adults": "2",
        "children_count": "0",
        "nights": "9–11",
        "travel_dates": "ноябрь",
        "contact_method": "chat",
    })
    assert "Отель" not in text
    assert "Питание" not in text
    assert "Мальдивы" in text
