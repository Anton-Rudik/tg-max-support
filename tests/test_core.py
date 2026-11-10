from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.config import Settings
from app.models import (
    Base, Contact, Material, Outbound, Staff, create_db, initialize,
    materials, publish, rollback, version
)
from app.service import SupportService, get_contact, enqueue_welcome, queue_materials
from app.main import inbound_key, delay


class FakeTelegram:
    def __init__(self):
        self.topics = []
        self.messages = []
        self.copies = []

    async def topic(self, group, title):
        self.topics.append((group, title))
        return {"message_thread_id": 100 + len(self.topics)}

    async def message(self, chat, text, thread=None, keyboard=None):
        self.messages.append((chat, text, thread, keyboard))

    async def copy(self, chat, from_chat, msgid, thread=None):
        self.copies.append((chat, from_chat, msgid, thread))


class FakeMax:
    async def send(self, *args, **kwargs):
        pass


def setup_db():
    engine, sessions = create_db("sqlite:///:memory:")
    settings = Settings(_env_file=None, admin_ids="111", tg_group_id=-100555,
                        database_url="sqlite:///:memory:")
    initialize(engine, sessions, settings)
    return engine, sessions, settings


def test_registration_uniqueness_and_welcome_once():
    eng, sessions, _ = setup_db()
    with sessions.begin() as db:
        c = get_contact(db, "tg", 444, "Мария")
        assert enqueue_welcome(db, c)
        assert not enqueue_welcome(db, c)
        assert get_contact(db, "tg", 444, "Мария").id == c.id
        assert get_contact(db, "max", 444, "Мария").id != c.id
    with sessions() as db:
        assert db.scalar(select(Outbound).where(Outbound.contact_id == 1)) is not None
        assert len(db.scalars(select(Outbound)).all()) == 1
    eng.dispose()


def test_material_publish_order_and_rollback():
    eng, sessions, _ = setup_db()
    with sessions.begin() as db:
        draft = version(db, False)
        db.add(Material(version_id=draft.id, position=20, kind="text", text="B"))
        db.add(Material(version_id=draft.id, position=10, kind="link", text="https://example.com"))
        published = publish(db)
        assert [m.kind for m in materials(db, version(db, True))] == ["link", "text"]
        draft = version(db, False)
        materials(db, draft)[0].text = "изменение"
        assert materials(db, version(db, True))[0].text == "https://example.com"
        publish(db)
        rollback(db, published)
        assert materials(db, version(db, True))[0].text == "https://example.com"
    eng.dispose()


@pytest.mark.asyncio
async def test_topic_reuse_and_reply_to_one_topic():
    eng, sessions, s = setup_db()
    tg = FakeTelegram()
    svc = SupportService(sessions, tg, FakeMax(), s)
    with sessions() as db:
        c = get_contact(db, "max", 901, "Иван")
        await svc.client_message(db, c, "Первый вопрос")
        await svc.client_message(db, c, "Второй вопрос")
        db.commit()
    assert len(tg.topics) == 1
    assert tg.messages[-1][2] == 101
    with sessions() as db:
        c = db.scalar(select(Contact))
        assert c.topic_id == 101
        assert c.status == "new"
    eng.dispose()


def test_inbound_idempotency_keys():
    assert inbound_key("tg", {"update_id": 10}) == "tg:10"
    obj = {"update_type": "message_created", "timestamp": 1, "message": {"body": {"mid": "abc"}}}
    assert inbound_key("max", obj) == "max:msg:abc"
    assert delay(100) <= 600


@pytest.mark.asyncio
async def test_manager_replies_shared_topic_without_exclusive_lock():
    eng, sessions, s = setup_db()
    tg = FakeTelegram()
    svc = SupportService(sessions, tg, FakeMax(), s)
    with sessions.begin() as db:
        db.add(Staff(telegram_id="222", role="manager"))
        c = get_contact(db, "tg", 999, "Олег")
        c.topic_id = 171
    for staff in (111, 222):
        await svc.on_tg({"message": {
            "message_id": staff, "message_thread_id": 171,
            "chat": {"id": -100555, "type": "supergroup"},
            "from": {"id": staff, "is_bot": False}, "text": f"Ответ {staff}"}})
    with sessions() as db:
        queued = db.scalars(select(Outbound).order_by(Outbound.id)).all()
        assert len(queued) == 2
        assert all(x.payload["kind"] == "tgcopy" for x in queued)
        assert queued[0].payload["actor"] != queued[1].payload["actor"]
    eng.dispose()
