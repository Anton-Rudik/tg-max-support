import json
import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import get_settings
from app.main import app
from app.models import Inbound
from app.platforms import TelegramAPI, MaxAPI


@pytest.mark.asyncio
async def test_telegram_media_upload_uses_multipart():
    async def respond(request):
        content = await request.aread()
        assert request.url.path == "/botTOKEN/sendPhoto"
        assert b'name="photo"' in content
        assert b"fakeimage" in content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        api = TelegramAPI("TOKEN", http)
        r = await api.media(19, "photo", b"fakeimage", "caption", name="photo.jpg")
        assert r["message_id"] == 1


@pytest.mark.asyncio
async def test_max_upload_then_send_media():
    seen = []

    async def respond(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/uploads":
            assert request.url.params["type"] == "image"
            return httpx.Response(200, json={"url": "https://iu.oneme.ru/uploadImage"})
        if request.url.path == "/uploadImage":
            return httpx.Response(200, json={"photos": {"img_1": {"token": "IMG_TOKEN"}}})
        assert request.url.path == "/messages"
        assert request.url.params["user_id"] == "123"
        body = json.loads(request.content)
        assert body["attachments"][0] == {"type": "image", "payload": {"token": "IMG_TOKEN"}}
        return httpx.Response(200, json={"body": {"mid": "aaa"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        api = MaxAPI("MAX_KEY", http, "https://platform-api2.max.ru")
        token = await api.upload("photo", b"photo-data", "example.png")
        await api.send(123, "photo", media=("image", token))
    assert len(seen) == 3


def test_webhooks_are_secured_and_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("TG_WEBHOOK_SECRET", "my_test_secret")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "max_test_secret")
    monkeypatch.setenv("TG_GROUP_ID", "0")
    monkeypatch.setenv("ADMIN_IDS", "111")
    get_settings.cache_clear()
    with TestClient(app) as client:
        event = {"update_id": 500}
        assert client.post("/webhooks/telegram", json=event).status_code == 403
        headers = {"X-Telegram-Bot-Api-Secret-Token": "my_test_secret"}
        assert client.post("/webhooks/telegram", headers=headers, json=event).status_code == 200
        assert client.post("/webhooks/telegram", headers=headers, json=event).status_code == 200
        assert client.post("/webhooks/max", json={"update_type": "bot_started"}).status_code == 403
        with app.state.sessions() as db:
            rows = db.scalars(select(Inbound).where(Inbound.key == "tg:500")).all()
            assert len(rows) == 1
    get_settings.cache_clear()
