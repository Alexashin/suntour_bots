# Солнечные туры — Telegram + MAX bot

Рабочий backend для турагентства «Солнечные туры»: один сценарий подбора тура, единая PostgreSQL-база и два клиентских адаптера — Telegram и MAX.

## Что умеет бот

### Полноценная заявка на подбор тура

Бот собирает:

1. город вылета;
2. страну / направление;
3. количество взрослых;
4. количество детей;
5. возраст детей, если они есть;
6. минимальную категорию отеля;
7. тип питания;
8. количество ночей;
9. даты поездки;
10. имя клиента;
11. предпочтительный способ связи;
12. телефон, если выбран звонок.

На каждом шаге есть inline-кнопки с частыми вариантами. Ручной ввод остаётся доступным: пользователь может просто написать свой вариант сообщением.

Перед отправкой показывается аккуратное резюме заявки. Пользователь может:

- отправить заявку;
- изменить конкретный параметр;
- вернуться назад;
- отменить подбор.

После подтверждения заявка сохраняется в PostgreSQL и отправляется администраторам.

### UX

Telegram и MAX используют один и тот же сценарий, но нативные возможности каждой платформы:

- Telegram — callback inline keyboard (`CallbackQuery`), редактирование текущей карточки через `editMessageText`;
- MAX — callback inline keyboard (`message_callback`) и `POST /answers`, который обновляет текущее сообщение;
- если клиент вводит значение вручную, бот редактирует текущую карточку; в Telegram его введённое сообщение best-effort удаляется после успешного принятия ответа, чтобы мастер выглядел как одна аккуратная карточка;
- номер телефона можно написать вручную;
- Telegram умеет принять номер через `request_contact` reply-кнопку;
- MAX использует нативную inline-кнопку `request_contact` и проверяет HMAC контакта.

### Аэропорты вылета

В быстрых вариантах сейчас используются только актуальные рабочие аэропорты, полезные для основной аудитории агентства:

- Москва;
- Санкт-Петербург;
- Калуга;
- Тамбов;
- Ярославль;
- другой город (ручной ввод);
- без перелёта.

Воронеж намеренно не предлагается как быстрый вариант. Список кнопок — UX-подсказка, а не ограничение: любой другой город можно ввести вручную.

## Подписка на рассылку

Новый пользователь **не подписан по умолчанию**.

- `/subscribe` — включить рассылку;
- `/unsubscribe` — отключить рассылку;
- также доступны кнопки главного меню.

В БД фиксируются `subscribed_at` и `unsubscribed_at`.

MAX-рассылка дополнительно защищена feature flag:

```env
MAX_MARKETING_BROADCAST_ENABLED=false
```

Для включения:

```env
MAX_MARKETING_BROADCAST_ENABLED=true
```

## Архитектура

```text
Telegram ─┐
          ├── adapters ──> shared scenario/services ──> PostgreSQL
MAX ──────┘

                           ├── Telegram admin(s)
new lead ── notifier ──────└── MAX admin(s)
```

Один backend означает:

- одинаковую логику заявки в двух мессенджерах;
- общую клиентскую базу;
- единую историю заявок;
- отсутствие копипаста бизнес-логики;
- возможность позже добавить web-админку без переделки ботов.

## Структура проекта

```text
.
├── app/
│   ├── bots/
│   │   ├── telegram_bot.py
│   │   └── max_bot.py
│   ├── api.py
│   ├── config.py
│   ├── content.py
│   ├── db.py
│   ├── logging_config.py
│   ├── main.py
│   ├── models.py
│   ├── notifier.py
│   ├── scenario.py
│   ├── services.py
│   └── telegram_factory.py
├── certs/
│   ├── Russian_Trusted_Root_CA.cer
│   ├── Russian_Trusted_Sub_CA.cer
│   └── Russian_Trusted_Sub_CA_2024.cer
├── docs/
│   └── ARCHITECTURE.md
├── tests/
│   └── test_scenario.py
├── Caddyfile
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

## Локальная разработка

### 1. Окружение

Используем Python 3.12 — та же версия стоит в Docker image.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Если Windows/VPN задаёт SOCKS-системный proxy и `pip` пишет `Missing dependencies for SOCKS support`:

```powershell
$env:NO_PROXY="*"
$env:no_proxy="*"
```

Установка:

```powershell
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 2. `.env`

```powershell
Copy-Item .env.example .env
```

Минимальный dev-конфиг:

```env
APP_ENV=dev
LOG_LEVEL=DEBUG

DATABASE_URL=postgresql+asyncpg://suntour:suntour@db:5432/suntour

TELEGRAM_TOKEN=...
TELEGRAM_ADMIN_IDS=123456789
TELEGRAM_ENABLE_POLLING=true
TELEGRAM_PROXY=socks5://host.docker.internal:10808

MAX_TOKEN=...
MAX_API_BASE=https://platform-api2.max.ru
MAX_MODE=polling
MAX_ADMIN_USER_IDS=...
MAX_ADMIN_CHAT_IDS=
MAX_MARKETING_BROADCAST_ENABLED=false
```

Если Telegram доступен напрямую, `TELEGRAM_PROXY` оставить пустым.

### 3. Запуск

```powershell
docker compose up --build
```

После старта в логах выводятся ссылки на Telegram- и MAX-бота.

Проверки:

```text
GET http://localhost:8000/health
GET http://localhost:8000/ready
```

## Telegram proxy

Proxy нужен только для окружения, где `api.telegram.org` недоступен напрямую.

Пример для SOCKS на Windows-хосте:

```env
TELEGRAM_PROXY=socks5://host.docker.internal:10808
```

На зарубежном VPS обычно:

```env
TELEGRAM_PROXY=
```

Код менять не нужно.

## MAX и сертификаты

MAX API используется через:

```text
https://platform-api2.max.ru
```

В `certs/` лежат:

- Russian Trusted Root CA;
- Russian Trusted Sub CA;
- Russian Trusted Sub CA 2024.

Dockerfile добавляет их в системное хранилище CA при сборке image.

## MAX: polling и webhook

Для разработки:

```env
MAX_MODE=polling
```

Для production рекомендуется webhook:

```env
MAX_MODE=webhook
PUBLIC_DOMAIN=bot.example.ru
MAX_WEBHOOK_SECRET=long_random_value
```

Caddy проксирует:

```text
https://bot.example.ru/webhooks/max -> app:8000/webhooks/max
```

Если используется webhook, публичный endpoint должен иметь HTTPS.

## Production

Рекомендуемый VPS для небольшого турагентства:

- 1 vCPU;
- 2 GB RAM;
- 15–20 GB SSD;
- Ubuntu/Debian;
- Docker + Docker Compose;
- статический публичный IP.

Production `.env`:

```env
APP_ENV=prod
LOG_LEVEL=INFO
TELEGRAM_PROXY=
MAX_MODE=webhook
```

Если MAX временно остаётся на polling, приложение это поддерживает, но режим предусмотрен прежде всего для разработки.

## Логирование

### Development

```env
LOG_LEVEL=DEBUG
```

DEBUG содержит:

- raw MAX updates;
- callback payload;
- текст ручного ввода;
- сетевые запросы MAX без токена;
- техническую диагностику редактирования сообщений.

### Production

```env
LOG_LEVEL=INFO
```

INFO оставляет только полезные события:

- старт компонентов;
- получение сообщения без содержимого текста;
- переходы сценария;
- подписка / отписка;
- создание заявки;
- доставка заявки администраторам.

WARNING:

- некорректный callback;
- чужой/невалидный контакт;
- временная ошибка сети;
- неавторизованная admin-команда.

ERROR:

- необработанные ошибки API/БД;
- ошибки доставки, требующие внимания.

Токены в лог не пишутся.

## Команды Telegram

Клиентские:

```text
/start
/menu
/subscribe
/unsubscribe
/whoami
```

Админские:

```text
/stats
/broadcast <текст>
```

`/broadcast` отправляет сообщение только пользователям с активной подпиской.

## Администраторы

Telegram ID:

```text
/whoami
```

MAX ID:

```text
/whoami
```

Можно указать несколько ID через запятую:

```env
TELEGRAM_ADMIN_IDS=111,222
MAX_ADMIN_USER_IDS=333,444
```

Заявка может одновременно дублироваться в Telegram и MAX.

## База данных

Сейчас используются таблицы:

- `users` — пользователи и подписки;
- `conversations` — состояние активного мастера;
- `leads` — завершённые заявки.

Расширенные поля заявки хранятся в `Lead.raw_payload` JSONB. Это позволяет добавлять новые параметры анкеты без немедленного изменения схемы БД.

Для следующего production-этапа рекомендуется подключить Alembic для управляемых миграций.

## Тесты

```powershell
$env:PYTHONPATH="."
python -m pytest -q
```

Проверяется:

- полный сценарий заявки;
- ветка телефонной связи;
- пропуск возраста при отсутствии детей;
- callback choices;
- редактирование параметров;
- переход назад.

## Безопасность

- `.env` не коммитить;
- токены не хранить в README;
- если токен попал в публичный репозиторий — перевыпустить;
- SSL verification не отключать;
- в production использовать `LOG_LEVEL=INFO`;
- данные контакта MAX проверяются через HMAC;
- номер телефона запрашивается только при выборе связи по телефону.

## Что логично добавить дальше

Архитектура уже позволяет без переписывания ядра добавить:

- web-админку;
- категории рассылок;
- загрузку фото/баннера в рассылку;
- CSV/XLSX-выгрузку;
- расширенную статистику;
- интеграцию с CRM;
- календарь выбора дат;
- автоматическое распределение заявок между менеджерами;
- UTM/source tracking.


## Тесты и build gate

Локально:

```powershell
pytest -q
```

Тесты также автоматически являются частью Docker-сборки. Команда:

```powershell
docker compose build
```

сначала собирает test-stage и запускает `pytest -q`. Если хотя бы один тест падает, production runtime image не создаётся. Поэтому обычный `docker compose up --build` одновременно проверяет бизнес-сценарий перед запуском.

Для быстрой проверки только тестов через Docker:

```powershell
docker build --target test -t suntour-bots-test .
```

## Администраторы и проверка заявок

Telegram:

1. Напишите боту `/whoami`.
2. Скопируйте `Telegram user_id`.
3. Укажите его в `.env`:

```env
TELEGRAM_ADMIN_IDS=123456789
```

Несколько администраторов перечисляются через запятую:

```env
TELEGRAM_ADMIN_IDS=123456789,987654321
```

MAX аналогично поддерживает `/whoami`; полученный ID указывается в `MAX_ADMIN_USER_IDS`. Заявка может одновременно дублироваться в Telegram и MAX.

После изменения `.env` достаточно перезапустить контейнеры:

```powershell
docker compose up -d --force-recreate
```
