import logging

from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import text

from app.bots.max_bot import MaxClient, process_max_update
from app.config import Settings
from app.db import SessionLocal

log = logging.getLogger(__name__)


def create_api(settings: Settings, max_client: MaxClient, notifier) -> FastAPI:
    app = FastAPI(title="Suntour Bots", version="1.0.0")

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "environment": settings.app_env,
            "telegram_configured": bool(settings.telegram_token),
            "max_configured": bool(settings.max_token),
            "max_mode": settings.max_mode,
        }

    @app.get("/ready")
    async def ready():
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            return {"ok": True, "database": "ready"}
        except Exception as exc:
            log.error("Readiness check failed", exc_info=True)
            raise HTTPException(status_code=503, detail="Database is not ready") from exc

    @app.post("/webhooks/max")
    async def max_webhook(
        request: Request,
        x_max_bot_api_secret: str | None = Header(default=None),
    ):
        if settings.max_mode != "webhook":
            raise HTTPException(status_code=404)

        if x_max_bot_api_secret != settings.max_webhook_secret:
            log.warning("Rejected MAX webhook with invalid secret")
            raise HTTPException(status_code=401, detail="Invalid MAX webhook secret")

        payload = await request.json()
        log.debug("MAX webhook payload=%r", payload)
        await process_max_update(max_client, payload, notifier)
        return {"ok": True}

    return app
