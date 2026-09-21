import logging

from fastapi import FastAPI, Header, HTTPException, Request

from app.bots.max_bot import MaxClient, process_max_update
from app.config import Settings

log = logging.getLogger(__name__)


def create_api(settings: Settings, max_client: MaxClient) -> FastAPI:
    app = FastAPI(title="Suntour Bots", version="0.1.0")

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "telegram": bool(settings.telegram_token),
            "max": bool(settings.max_token),
            "max_mode": settings.max_mode,
        }

    @app.post("/webhooks/max")
    async def max_webhook(
        request: Request,
        x_max_bot_api_secret: str | None = Header(default=None),
    ):
        if settings.max_mode != "webhook":
            raise HTTPException(status_code=404)

        if x_max_bot_api_secret != settings.max_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid MAX webhook secret")

        payload = await request.json()
        # MAX должен быстро получить 200; MVP обрабатывает событие синхронно.
        # Для высоких нагрузок вынести обработку в очередь.
        await process_max_update(max_client, payload)
        return {"ok": True}

    return app
