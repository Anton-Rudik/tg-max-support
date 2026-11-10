from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint, create_engine, select
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from .config import get_settings
from .platforms import client_text


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(12))
    external_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200), default="Клиент")
    username: Mapped[str] = mapped_column(String(150), default="")
    topic_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    welcomed: Mapped[bool] = mapped_column(Boolean, default=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="closed")
    owner_id: Mapped[str | None] = mapped_column(String(64))
    last_customer_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_answer_at: Mapped[datetime | None] = mapped_column(DateTime)
    reminded: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Staff(Base):
    __tablename__ = "staff"
    telegram_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="manager")


class Inbound(Base):
    __tablename__ = "inbound"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    platform: Mapped[str] = mapped_column(String(12))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_error: Mapped[str] = mapped_column(Text, default="")


class Outbound(Base):
    __tablename__ = "outbound"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_error: Mapped[str] = mapped_column(Text, default="")


class MaterialVersion(Base):
    __tablename__ = "material_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("material_versions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String(200), default="")
    filepath: Mapped[str] = mapped_column(String(500), default="")
    max_token: Mapped[str] = mapped_column(String(400), default="")


class ConfigValue(Base):
    __tablename__ = "config_values"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class AdminState(Base):
    __tablename__ = "admin_states"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(80), default="")


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    actor: Mapped[str] = mapped_column(String(64), default="system")
    event: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=now)


def create_db(url=None):
    url = url or get_settings().database_url
    opts = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, **opts)
    return engine, sessionmaker(engine, expire_on_commit=False)


def initialize(engine, sessions, settings):
    Base.metadata.create_all(engine)
    with sessions.begin() as db:
        for uid in settings.admins:
            staff = db.get(Staff, str(uid))
            if staff is None:
                db.add(Staff(telegram_id=str(uid), role="admin"))
            else:
                staff.role = "admin"
        if db.get(ConfigValue, "welcome") is None:
            db.add(ConfigValue(key="welcome", value=settings.welcome_text or client_text(
                settings.ui_language, "welcome", project_name=settings.project_name)))
        if db.query(MaterialVersion).count() == 0:
            db.add(MaterialVersion(id=1, published=True))
            db.add(MaterialVersion(id=2, published=False))


def setting(db, key, default=""):
    row = db.get(ConfigValue, key)
    return row.value if row else default


def set_setting(db, key, value):
    row = db.get(ConfigValue, key)
    if row:
        row.value = str(value)
    else:
        db.add(ConfigValue(key=key, value=str(value)))


def version(db, published=True):
    return db.scalars(select(MaterialVersion).where(MaterialVersion.published == published).order_by(MaterialVersion.id.desc())).first()


def materials(db, v):
    return db.scalars(select(Material).where(Material.version_id == v.id).order_by(Material.position, Material.id)).all()


def clone_version(db, source):
    new = MaterialVersion(published=False)
    db.add(new)
    db.flush()
    for m in materials(db, source):
        db.add(Material(version_id=new.id, position=m.position, kind=m.kind,
                        text=m.text, filename=m.filename, filepath=m.filepath,
                        max_token=m.max_token))
    return new


def publish(db):
    old = version(db, True)
    draft = version(db, False)
    old.published = False
    draft.published = True
    db.flush()
    clone_version(db, draft)
    return draft.id


def rollback(db, ver_id):
    target = db.get(MaterialVersion, ver_id)
    if target is None:
        raise ValueError("Версия не найдена")
    current = version(db, True)
    draft = version(db, False)
    current.published = False
    # Retain the old draft as an immutable archive; duplicate target to publish.
    copy = clone_version(db, target)
    copy.published = True
    db.flush()
    clone_version(db, copy)
    return copy.id
