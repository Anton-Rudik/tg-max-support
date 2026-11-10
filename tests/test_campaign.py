"""Funnel semantics: text strategy, three automatic videos, gated video CTA."""
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Material, Outbound, Staff, create_db, initialize, publish, version
from app.service import SupportService, enqueue_welcome, get_contact, material_refs, queue_materials, set_material_refs


class FakeTelegram:
    def __init__(self):
        self.sent_media = []
        self.sent_messages = []
    async def media(self, chat_id, kind, ref, caption="", thread=None, name="file"):
        self.sent_media.append((chat_id, kind, ref, caption))
    async def message(self, chat_id, text, thread=None, keyboard=None):
        self.sent_messages.append((chat_id, text, keyboard))
    async def callback_answer(self, *_args):
        return None


class FakeMax:
    def __init__(self):
        self.uploaded = []
        self.sent = []
    async def upload(self, kind, content, filename="file"):
        self.uploaded.append((kind, content, filename))
        return "max-token"
    async def send(self, user_id, text="", buttons=None, media=None):
        self.sent.append((user_id, text, buttons, media))


def fixture_db():
    engine, sessions = create_db("sqlite:///:memory:")
    settings = Settings(_env_file=None, admin_ids="111", database_url="sqlite:///:memory:", tg_group_id=-100555)
    initialize(engine, sessions, settings)
    return engine, sessions, settings


def seed_funnel(db):
    draft = version(db, False)
    db.add(Material(version_id=draft.id, position=10, kind="text",
                    text="⚡️ Авторская стратегия 50.01%\n\nПолный текст стратегии без изображения."))
    for index in range(1, 4):
        db.add(Material(version_id=draft.id, position=(index + 1) * 10,
                        kind="video", text=f"Видео {index}", filename=f"v{index}.mp4",
                        filepath=json.dumps({"tg_file_id": f"TELEGRAM_ID_{index}"})))
    db.add(Material(version_id=draft.id, position=50, kind="gated_video",
                    text="О клубе", filename="v4.mp4",
                    filepath=json.dumps({"tg_file_id": "TELEGRAM_ID_4"})))
    publish(db)


def test_campaign_text_then_three_videos_then_button_then_last_video():
    engine, sessions, _ = fixture_db()
    with sessions.begin() as db:
        seed_funnel(db)
        user = get_contact(db, "tg", 999, "Клиент")
        assert enqueue_welcome(db, user)
    with sessions() as db:
        jobs = db.scalars(select(Outbound).order_by(Outbound.id)).all()
        assert [j.payload["kind"] for j in jobs] == ["welcome"] + ["material"] * 4 + ["campaign_cta"]
        assert jobs[-1].payload["button"] == "Посмотреть"
        sequence = [db.get(Material, job.payload["material_id"]).kind
                    for job in jobs if job.payload["kind"] == "material"]
        assert sequence == ["text", "video", "video", "video"]
        queue_materials(db, user, gated=True)
        db.commit()
    with sessions() as db:
        jobs = db.scalars(select(Outbound).order_by(Outbound.id)).all()
        assert jobs[-1].payload["kind"] == "material"
        assert db.get(Material, jobs[-1].payload["material_id"]).kind == "gated_video"
    engine.dispose()


@pytest.mark.asyncio
async def test_large_video_ref_does_not_trigger_download_or_forward():
    engine, sessions, settings = fixture_db()
    tg, mx = FakeTelegram(), FakeMax()
    svc = SupportService(sessions, tg, mx, settings)
    with sessions.begin() as db:
        seed_funnel(db)
    with sessions() as db:
        m = db.scalar(select(Material).where(Material.kind == "video").limit(1))
        await svc.send_tg_material(999, m)
    assert tg.sent_media[0] == (999, "video", "TELEGRAM_ID_1", "Видео 1")
    engine.dispose()


@pytest.mark.asyncio
async def test_max_needs_local_optimized_video(tmp_path):
    engine, sessions, settings = fixture_db()
    tg, mx = FakeTelegram(), FakeMax()
    svc = SupportService(sessions, tg, mx, settings)
    with sessions.begin() as db:
        seed_funnel(db)
        user = get_contact(db, "max", 909, "Клиент")
        m = db.scalar(select(Material).where(Material.kind == "video").limit(1))
        path = tmp_path / "v0.mp4"
        path.write_bytes(b"small max version")
        set_material_refs(m, max_path=str(path))
        db.flush()
        await svc.deliver_max(db, user, {"kind": "material", "material_id": m.id})
    assert mx.uploaded[0][0] == "video"
    assert isinstance(mx.uploaded[0][1], Path)
    assert mx.sent[0][-1] == ("video", "max-token")
    engine.dispose()


@pytest.mark.asyncio
async def test_campaign_button_routes_to_gated_material():
    engine, sessions, settings = fixture_db()
    tg, mx = FakeTelegram(), FakeMax()
    svc = SupportService(sessions, tg, mx, settings)
    with sessions.begin() as db:
        seed_funnel(db)
        get_contact(db, "tg", 999, "Клиент")
    await svc.on_tg_callback({"id": "callback001", "from": {"id": 999}, "data": "client:watch_more"})
    with sessions() as db:
        jobs = db.scalars(select(Outbound).order_by(Outbound.id)).all()
        assert len(jobs) == 1
        assert db.get(Material, jobs[0].payload["material_id"]).kind == "gated_video"
    engine.dispose()


@pytest.mark.asyncio
async def test_strategy_is_only_text_on_telegram_and_max():
    engine, sessions, settings = fixture_db()
    tg, mx = FakeTelegram(), FakeMax()
    svc = SupportService(sessions, tg, mx, settings)
    with sessions.begin() as db:
        seed_funnel(db)
        max_contact = get_contact(db, "max", 909, "Клиент")
        strategy = db.scalar(select(Material).where(Material.kind == "text").limit(1))
        await svc.send_tg_material(999, strategy)
        await svc.deliver_max(db, max_contact, {"kind": "material", "material_id": strategy.id})
    assert tg.sent_media == []
    assert tg.sent_messages[0][1].startswith("⚡️ Авторская стратегия")
    assert mx.uploaded == []
    assert mx.sent[0][1] == tg.sent_messages[0][1]
    engine.dispose()
