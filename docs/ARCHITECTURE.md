# Архитектура Suntour Bots

## Общая схема

```text
Telegram users ─┐
                ├─> adapters ─> shared scenario/services ─> PostgreSQL
MAX users ──────┘

                           ┌─> Telegram admins
Lead -> AdminNotifier ─────┤
                           └─> MAX admins
```

## Заявка

`Lead` — источник истины по состоянию заявки. Сообщения в мессенджерах не хранят собственный независимый статус.

`LeadMessage` хранит platform/chat/message ID каждой отправленной админу копии. После изменения `Lead.status` `AdminNotifier.sync_lead()` перерисовывает все известные копии.

Это обеспечивает одинаковый статус при дублировании заявки в Telegram и MAX.

## Рассылки

`Broadcast` хранит:

- автора;
- целевую платформу (`telegram`, `max`, `both`);
- текст;
- состояние `draft/sent/cancelled`;
- счётчики sent/failed/skipped;
- время отправки.

Рассылка выбирает только `User.subscribed = true`.

## Диалоги

`Conversation` хранит состояние клиентского мастера и коротких админских flow, например ожидание текста рассылки. Это позволяет пережить рестарт контейнера без потери текущего шага.

## Миграции

Alembic выполняется entrypoint'ом до запуска приложения. Первый revision является bootstrap migration и умеет подхватить старую pre-Alembic БД.

Последующие изменения схемы должны идти отдельными revision-файлами.
