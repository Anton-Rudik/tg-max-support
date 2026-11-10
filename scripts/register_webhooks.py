"""Register selected Telegram and/or MAX webhooks. Requires valid .env and HTTPS."""
import argparse
import asyncio
import httpx
from app.config import get_settings


async def register(platform):
    s = get_settings()
    if not s.public_base_url.startswith("https://"):
        raise ValueError("PUBLIC_BASE_URL must use HTTPS")
    async with httpx.AsyncClient(timeout=30) as http:
        if platform in ("tg", "all"):
            if not s.tg_bot_token or not s.tg_webhook_secret:
                raise ValueError("TG_BOT_TOKEN and TG_WEBHOOK_SECRET are required")
            response = await http.post(f"https://api.telegram.org/bot{s.tg_bot_token}/setWebhook", json={
                "url": s.public_base_url.rstrip("/") + "/webhooks/telegram",
                "secret_token": s.tg_webhook_secret,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            })
            response.raise_for_status()
            print("Telegram:", response.json())
        if platform in ("max", "all"):
            if not s.max_bot_token or not s.max_webhook_secret:
                raise ValueError("MAX_BOT_TOKEN and MAX_WEBHOOK_SECRET are required")
            response = await http.post(s.max_api_base.rstrip("/") + "/subscriptions",
                headers={"Authorization": s.max_bot_token}, json={
                    "url": s.public_base_url.rstrip("/") + "/webhooks/max",
                    "secret": s.max_webhook_secret,
                    "update_types": ["message_created", "message_callback", "bot_started", "bot_stopped"],
                })
            response.raise_for_status()
            print("MAX:", response.json())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register bot webhooks")
    parser.add_argument("--platform", choices=["tg", "max", "all"], default="all")
    args = parser.parse_args()
    asyncio.run(register(args.platform))
