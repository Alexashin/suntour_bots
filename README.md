# Солнечные туры — Telegram + MAX

Рабочий backend для турагентства: Telegram и MAX используют одну бизнес-логику, одну PostgreSQL-базу и общую систему заявок/подписок.

## Что умеет проект

### Пользовательская часть

При `/start` бот **не начинает анкету сразу**, а показывает главное меню:

- 🧳 Подобрать тур
- 🔥 Подписаться на рассылку / 🔕 Отписаться
- ℹ️ Контакты
- 🛠 Админ-панель — только для администраторов

Подбор тура работает как пошаговый мастер в одном редактируемом сообщении. Есть callback-кнопки и ручной ввод.

Заявка собирает:

- город вылета;
- направление;
- количество взрослых;
- количество детей;
- возраст детей;
- категорию отеля;
- питание;
- количество ночей;
- даты;
- имя;
- удобный способ связи;
- телефон, если выбран звонок.

В Telegram сообщения ручного ввода удаляются best-effort после обработки, чтобы мастер оставался визуально чистым.

### Заявки администраторам

Заявка может одновременно отправляться в Telegram и MAX.

Под каждой копией есть кнопка **«✅ Обработана»**. Статус хранится в БД, поэтому:

1. менеджер нажимает кнопку в одном мессенджере;
2. `Lead.status` меняется на `processed`;
3. сохраняются менеджер и время обработки;
4. все сохранённые копии заявки в Telegram/MAX редактируются;
5. появляется отметка `✅ ОБРАБОТАНА`;
6. вместо кнопки появляется `↩️ Вернуть в новые`.

Поддерживаемые статусы в модели:

- `new`;
- `in_progress` — зарезервирован на будущее;
- `processed`;
- `cancelled` — зарезервирован на будущее.

### Админ-панель

Открывается кнопкой **«🛠 Админ-панель»** в главном меню или `/admin`.

Сейчас доступны:

- 📊 статистика;
- 🆕 последние новые заявки;
- 📣 создание рассылки.

Рассылка создаётся через UI:

1. `📣 Создать рассылку`;
2. выбрать Telegram / MAX / оба мессенджера;
3. отправить текст обычным сообщением;
4. получить превью;
5. `🚀 Отправить` или `❌ Отменить`.

Рассылка идёт **только пользователям с активной подпиской**. `/unsubscribe` исключает пользователя из следующих рассылок.

Для MAX маркетинговая рассылка дополнительно управляется флагом:

```env
MAX_MARKETING_BROADCAST_ENABLED=true
```

Если он `false`, MAX-получатели будут пропущены.

## Стек

- Python 3.12
- aiogram 3
- aiohttp
- FastAPI
- SQLAlchemy 2 async
- PostgreSQL 16
- asyncpg
- Alembic
- Docker Compose
- Caddy для production webhook MAX
- pytest

## База данных и Alembic

Схема БД теперь управляется **Alembic**. `Base.metadata.create_all()` при запуске приложения больше не используется.

В проекте есть bootstrap-миграция, рассчитанная и на:

- новую пустую БД;
- уже существующую БД предыдущих версий проекта.

При старте Docker-контейнера выполняется:

```bash
alembic upgrade head
python -m app.main
```

Поэтому существующий volume PostgreSQL **удалять не нужно**.

Первая миграция добавляет в `leads` статус и поля обработки, а также создаёт:

- `lead_messages` — связи заявки с сообщениями администраторов;
- `broadcasts` — черновики и результаты рассылок;
- таблицу `alembic_version`.

Для следующих изменений модели:

```bash
docker compose exec app alembic revision --autogenerate -m "description"
docker compose exec app alembic upgrade head
```

Перед применением автоматически сгенерированной миграции её нужно просматривать.

## Настройка

Скопировать окружение:

```powershell
Copy-Item .env.example .env
```

Минимальный dev `.env`:

```env
APP_ENV=dev
LOG_LEVEL=DEBUG
DATABASE_URL=postgresql+asyncpg://suntour:suntour@db:5432/suntour

TELEGRAM_TOKEN=...
TELEGRAM_ADMIN_IDS=123456789
TELEGRAM_ENABLE_POLLING=true
TELEGRAM_PROXY=socks5://host.docker.internal:10808

MAX_TOKEN=...
MAX_MODE=polling
MAX_ADMIN_USER_IDS=123456789
MAX_ADMIN_CHAT_IDS=
MAX_MARKETING_BROADCAST_ENABLED=true
```

На зарубежном production-сервере Telegram proxy обычно отключается:

```env
APP_ENV=prod
LOG_LEVEL=INFO
TELEGRAM_PROXY=
```

### Как узнать ID администратора

Telegram:

```text
/whoami
```

Записать `user_id` в:

```env
TELEGRAM_ADMIN_IDS=123456789,987654321
```

MAX:

```text
/whoami
```

Записать ID менеджеров в:

```env
MAX_ADMIN_USER_IDS=123456789,987654321
```

Если заявки дополнительно отправляются в групповой MAX-чат, его ID можно добавить в:

```env
MAX_ADMIN_CHAT_IDS=...
```

> Для нажатия админских callback-кнопок в MAX менеджер всё равно должен быть указан в `MAX_ADMIN_USER_IDS`.

## Запуск

```powershell
docker compose build --no-cache
docker compose up
```

При старте в логе должны появиться Telegram и MAX bot username.

## Тесты

Тесты являются build gate: runtime image не собирается, пока test-stage не завершится успешно.

В Dockerfile:

```dockerfile
RUN pytest -q && touch /tmp/tests-passed
```

Отдельный запуск:

```powershell
docker build --target test -t suntour-bots-test .
```

Локально:

```powershell
pytest -q
```

`pytest.ini` добавляет корень проекта в `PYTHONPATH`, поэтому `app.*` корректно импортируется и локально, и в Docker.

## Логи

Разработка:

```env
LOG_LEVEL=DEBUG
```

Показывает raw MAX updates, payload и подробности сценариев.

Production:

```env
LOG_LEVEL=INFO
```

Показывает основные события без засорения raw payload'ами.

Уровни используются по назначению:

- `DEBUG` — API payload, ручной ввод, техническая трассировка;
- `INFO` — старт, заявки, подписки, рассылки, смена статуса;
- `WARNING` — временные сетевые проблемы, некорректные события, отсутствие админов;
- `ERROR` — ошибки, мешающие операции завершиться.

## MAX сертификаты

В `certs/` лежат:

- `Russian_Trusted_Root_CA.cer`
- `Russian_Trusted_Sub_CA.cer`
- `Russian_Trusted_Sub_CA_2024.cer`

Они устанавливаются в trust store Docker image при сборке. Никакого скачивания сертификатов на старте нет.

## Production MAX

Разработка:

```env
MAX_MODE=polling
```

Production рекомендуется переводить на webhook:

```env
MAX_MODE=webhook
PUBLIC_DOMAIN=bot.example.ru
MAX_WEBHOOK_SECRET=...
```

Caddy уже есть в `docker-compose.yml` под profile `prod`.

## Healthcheck

```text
GET /health
GET /ready
```

`/ready` дополнительно проверяет соединение с PostgreSQL.

## Основная структура

```text
app/
├── api.py
├── bots/
│   ├── telegram_bot.py
│   └── max_bot.py
├── config.py
├── content.py
├── db.py
├── logging_config.py
├── main.py
├── models.py
├── notifier.py
├── scenario.py
├── services.py
└── telegram_factory.py

alembic/
├── env.py
└── versions/
```

## Что логично добавить следующим

- изображения и вложения в рассылках;
- аудит действий менеджеров;
- отдельный статус «в работе» с кнопкой «взять заявку»;
- фильтры/сегменты рассылки по интересующим направлениям;
- Excel/CSV экспорт базы;
- retention/очистку технических логов.

---

# Rich broadcasts and analytics-ready database

## Рассылка

Администратор выбирает аудиторию и отправляет содержимое рассылки. В Telegram-админке поддерживаются:

- обычный текст;
- одно фото + подпись;
- одно видео + подпись;
- необязательная кнопка-ссылка;
- произвольный текст кнопки (по умолчанию «Подробнее»);
- предпросмотр перед запуском.

Для получателей MAX кнопка-ссылка также нативная inline-кнопка. Фото и видео, созданные из Telegram-админки, сохраняются в persistent volume `media_data`, один раз загружаются в MAX и затем рассылаются подписчикам.

Рассылка не выполняется внутри callback обработчика. После подтверждения она получает статус `queued`; отдельный `broadcast-worker` переводит её в `sending` и отправляет сообщения в фоне. Боты, заявки и меню продолжают работать во время рассылки. Результат каждого получателя сохраняется в `broadcast_deliveries`, поэтому позже можно показать прогресс, повторить ошибки или выгрузить отчёт.

MAX-админка также умеет текстовую рассылку и кнопку-ссылку. Создание rich-media рассылки рекомендуется делать из Telegram-админки: один локально сохранённый файл затем используется для обеих платформ.

## Контакт клиента в заявке

Контакт сохраняется снимком в самой заявке (`contact_value`, `contact_url`, `contact_platform`), чтобы изменение username пользователя позже не ломало старую заявку.

- Telegram + публичный username: `https://t.me/<username>`.
- Telegram без username: внутри Telegram используется `tg://user?id=...`; в копии MAX менеджеру показывается понятное имя и подсказка открыть Telegram-копию заявки.
- MAX: внутри MAX используется нативная ссылка `max://user/<id>`, поэтому менеджер нажимает на имя, а не копирует числовой ID. В Telegram-копии показывается username/имя и подсказка открыть MAX-копию.
- При выборе звонка сохраняется телефон и `tel:` ссылка.

## Аналитическая модель БД

В `users` сохраняются только данные, которые платформа реально отдаёт и которые могут быть полезны бизнесу:

- платформа, platform user id и chat id;
- username, display name, имя и фамилия;
- телефон, если пользователь сам его передал;
- language code (если доступен);
- Telegram Premium flag (если доступен);
- время последней активности MAX (если платформа его вернула);
- first seen / last seen;
- подписка и даты opt-in / opt-out;
- source/start parameter на будущее для рекламных меток;
- `profile_meta` для дополнительных несекретных полей платформы.

Пол пользователя Telegram/MAX Bot API не предоставляет. Бот его не угадывает по имени. Для будущей добровольной анкеты поле `declared_gender` уже предусмотрено и остаётся `NULL`, пока пользователь сам ничего не сообщил.

`user_events` хранит историю бизнес-событий: подписка/отписка, запуск сценария, ответы ручным вводом, создание заявки и дальнейшие события, которые будем добавлять. Полные параметры каждой завершённой заявки остаются в `leads.raw_payload`, поэтому история интереса к направлениям не теряется.

Для будущей выгрузки миграция создаёт два read-only SQL view:

- `analytics_users` — карточка пользователя + число заявок + первая/последняя заявка;
- `analytics_leads` — плоская строка заявки с пользователем, контактом, направлением, составом туристов, отелем, питанием и датами.

Будущая кнопка CSV/XLSX сможет делать обычный `SELECT` из этих views без переделки уже накопленной базы.

## Миграция v7

При запуске контейнера `entrypoint.sh` автоматически выполняет:

```bash
alembic upgrade head
```

Миграция `20260921_02` добавляет аналитические поля, `user_events`, `broadcast_deliveries`, rich-broadcast поля и export views. PostgreSQL volume удалять не нужно.

### v7.2 — Telegram media navigation fix

Admin navigation now safely returns to text menus after photo/video broadcast previews or results. Telegram text messages are edited in place; media messages are removed best-effort and replaced with a fresh text card, so the `⬅️ Главное меню` button does not fail on `edit_text()` for media messages.
