# Architecture

## Core principle

Telegram and MAX are transport adapters around one domain scenario.

```text
Telegram callback/text/contact ----\
                                   > scenario.py -> services.py -> PostgreSQL
MAX callback/text/contact --------/

                                    -> AdminNotifier -> Telegram/MAX managers
```

## Wizard state

`Conversation` stores:

- `step` — current logical step;
- `data` — accumulated answers + technical `_...` metadata.

Technical keys start with `_` and are removed before the finished lead payload is stored.

Important metadata:

- `_history` — back navigation;
- `_editing` — single-field edit mode;
- `_ui_message_id` — bot message used as the editable wizard card.

## Wizard flow

```text
departure_city
  -> destination
  -> adults
  -> children_count
      -> children_ages (only when children > 0)
  -> hotel_category
  -> meal_plan
  -> nights
  -> dates
  -> name
  -> contact_method
      -> phone (only when callback requested)
  -> review
  -> submit
```

## Telegram UI

- Inline callback keyboards for choices.
- The current wizard card is edited instead of sending a new bot message for every step.
- User text remains valid as an alternative input.
- `request_contact` requires Telegram reply keyboard, so it is used only for the phone step.

## MAX UI

- Inline callback buttons emit `message_callback`.
- `POST /answers` updates the same message after a callback.
- Manual text input edits the last bot wizard card using `PUT /messages` when its `mid` is known.
- `request_contact` is a native MAX inline button.

## Database compatibility

Extended tourism fields live in `Lead.raw_payload` JSONB. Existing scalar columns are still populated for backwards compatibility:

- destination;
- travel_dates;
- travelers;
- nights;
- client_name;
- contact_method;
- phone.

This avoids an immediate destructive schema migration while the form is still evolving.

## Failure isolation

Telegram polling, MAX polling and FastAPI run as separate asyncio tasks. Adapter network errors retry instead of intentionally terminating the other messenger.

## Production follow-up

Before a larger deployment:

1. Add Alembic migrations.
2. Add database backups.
3. Use webhook for MAX.
4. Add structured JSON logging if logs go to Loki/ELK.
5. Add an explicit outbound queue for large broadcasts.


## Build gate

Dockerfile содержит отдельный `test` stage. Runtime stage зависит от маркерного файла из него, поэтому BuildKit обязан выполнить `pytest -q` при обычной production-сборке. Неуспешные тесты блокируют создание runtime image.

## Wizard UI

Telegram хранит ID одной карточки мастера и редактирует её. Ручной ответ пользователя после успешного чтения удаляется best-effort. Ошибка удаления логируется только на DEBUG и не влияет на сценарий. MAX редактирует бот-карточку через callback API; пользовательские ручные сообщения сохраняются как обычная история чата.
