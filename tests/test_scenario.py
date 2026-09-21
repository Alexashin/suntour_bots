from app.scenario import consume, edit_step, go_back, render_review, resolve_choice, start_scenario


def test_full_chat_scenario_reaches_review():
    result = start_scenario()
    data = result.data
    step = result.next_step

    values = [
        "Москва",        # departure_city
        "Турция",        # destination
        "2",             # adults
        "2",             # children_count
        "5, 9",          # children_ages
        "4★ и выше",     # hotel_category
        "Всё включено",  # meal_plan
        "9–11",          # nights
        "10–20 октября", # dates
        "Артём",         # name
        "chat",          # contact_method
    ]

    for value in values:
        result = consume(step, value, data)
        data = result.data
        step = result.next_step

    assert step == "review"
    assert "Турция" in render_review(data)
    assert data["children_ages"] == "5, 9"


def test_phone_flow_requires_phone_before_review():
    data = {"_history": []}
    result = consume("contact_method", "phone", data)
    assert result.next_step == "phone"

    result = consume("phone", "+79050505172", result.data)
    assert result.next_step == "review"
    assert result.data["phone"] == "+79050505172"


def test_no_children_skips_age_step():
    result = consume("children_count", "0", {"_history": []})
    assert result.next_step == "hotel_category"


def test_choice_resolution():
    value = resolve_choice("destination", "tr", {"_history": []})
    assert value == "Турция"


def test_edit_returns_to_review_after_one_field():
    data = {
        "_history": [],
        "departure_city": "Москва",
        "destination": "Турция",
        "adults": "2",
        "children_count": "0",
        "hotel_category": "4★ и выше",
        "meal_plan": "Всё включено",
        "nights": "9–11",
        "travel_dates": "октябрь",
        "client_name": "Артём",
        "contact_method": "chat",
    }
    edit = edit_step(data, "meal_plan")
    assert edit.next_step == "meal_plan"
    changed = consume("meal_plan", "Завтрак", edit.data)
    assert changed.next_step == "review"
    assert changed.data["meal_plan"] == "Завтрак"


def test_back_uses_history():
    data = {"_history": ["departure_city", "destination"], "departure_city": "Москва", "destination": "Турция"}
    result = go_back("adults", data)
    assert result.next_step == "destination"


def test_departure_airport_choices_are_current():
    from app.scenario import choice_rows

    labels = [choice.value for row in choice_rows("departure_city", {"_history": []}) for choice in row]
    assert "Москва" in labels
    assert "Санкт-Петербург" in labels
    assert "Калуга" in labels
    assert "Тамбов" in labels
    assert "Ярославль" in labels
    assert "Воронеж" not in labels
