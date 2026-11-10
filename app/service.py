import logging
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from .models import (
    AdminState, Audit, Contact, Inbound, Material, MaterialVersion, Outbound,
    Staff, clone_version, materials, now, publish, rollback, set_setting,
    setting, version
)
from .platforms import tg_attachment, max_attachment, tg_menu, max_menu, client_text, PlatformError

log = logging.getLogger(__name__)
MAX_SIZE = 20 * 1024 * 1024
MAX_VIDEO_SIZE = 250 * 1024 * 1024


def media_kind(material):
    return material.kind.removeprefix("gated_")


def material_refs(material):
    """Reuse legacy on-disk materials and versioned multi-platform references."""
    if not material.filepath:
        return {}
    if material.filepath.startswith("{"):
        try:
            return json.loads(material.filepath)
        except (ValueError, TypeError):
            return {}
    return {"local_path": material.filepath}


def set_material_refs(material, **updates):
    refs = material_refs(material)
    refs.update(updates)
    material.filepath = json.dumps(refs, ensure_ascii=False)


def local_material_path(material, for_max=False):
    refs = material_refs(material)
    value = refs.get("max_path") if for_max else refs.get("local_path")
    if for_max and not value:
        value = refs.get("local_path")
    return Path(value) if value else None


def is_gated(material):
    return material.kind.startswith("gated_")


def allowed(db, uid):
    return db.get(Staff, str(uid)) is not None


def admin(db, uid):
    x = db.get(Staff, str(uid))
    return x is not None and x.role == "admin"


def find_contact(db, platform, uid):
    return db.scalar(select(Contact).where(Contact.platform == platform,
                                           Contact.external_id == str(uid)))


def get_contact(db, platform, uid, name, username=""):
    contact = find_contact(db, platform, uid)
    if contact is None:
        contact = Contact(platform=platform, external_id=str(uid), name=(name or "Клиент")[:200],
                          username=(username or "")[:150])
        db.add(contact)
        db.flush()
    else:
        if name:
            contact.name = name[:200]
        if username:
            contact.username = username[:150]
        contact.available = True
    return contact


def push(db, contact, payload):
    db.add(Outbound(contact_id=contact.id, payload=payload))


def queue_materials(db, contact, v=None, gated=False):
    v = v or version(db, True)
    entries = materials(db, v)
    for m in entries:
        if is_gated(m) == gated:
            push(db, contact, {"kind": "material", "material_id": m.id})
    if not gated and any(is_gated(m) for m in entries):
        push(db, contact, {"kind": "campaign_cta",
                           "text": setting(db, "campaign_cta", "Хотите узнать больше о нашем клубе? Посмотрите дополнительное видео."),
                           "button": setting(db, "campaign_button", "Посмотреть")})


def enqueue_welcome(db, contact):
    if contact.welcomed:
        return False
    contact.welcomed = True
    push(db, contact, {"kind": "welcome", "text": setting(db, "welcome")})
    queue_materials(db, contact)
    return True


def ticket_keyboard(contact_id):
    return [[{"text": "✅ В работу", "callback_data": f"ticket:take:{contact_id}"},
             {"text": "🔒 Закрыть", "callback_data": f"ticket:close:{contact_id}"}],
            [{"text": "🔓 Открыть снова", "callback_data": f"ticket:reopen:{contact_id}"}]]


class SupportService:
    def __init__(self, sessions, tg, mx, settings):
        self.sessions = sessions
        self.tg = tg
        self.mx = mx
        self.settings = settings

    async def ensure_topic(self, db, c):
        if c.topic_id:
            return c.topic_id
        if not self.settings.tg_group_id:
            raise PlatformError("TG_GROUP_ID не настроен", False)
        label = "TG" if c.platform == "tg" else "MAX"
        created = await self.tg.topic(self.settings.tg_group_id, f"{label} | {c.name} | #{c.id}")
        c.topic_id = int(created["message_thread_id"])
        db.flush()
        await self.tg.message(self.settings.tg_group_id,
                              f"Карточка #{c.id}\nПлатформа: {label}\nИмя: {c.name}\n"
                              f"Статус: {c.status}\nМенеджер: не назначен\n"
                              "Все сообщения менеджеров в этой теме передаются клиенту.",
                              thread=c.topic_id, keyboard=ticket_keyboard(c.id))
        return c.topic_id

    def topic_link(self, topic_id):
        group = str(self.settings.tg_group_id)
        if not group.startswith("-100"):
            return ""
        return f"https://t.me/c/{group[4:]}/{topic_id}"

    async def notice(self, db, text):
        thread = setting(db, "notice_thread")
        if thread:
            await self.tg.message(self.settings.tg_group_id, text[:4096], thread=int(thread))
        elif self.settings.tg_group_id:
            await self.tg.message(self.settings.tg_group_id, text[:4096])

    async def ensure_internal_topics(self, db):
        if not self.settings.tg_group_id or not self.settings.tg_bot_token:
            return
        for key, title in (("notice_thread", "🔔 Новые обращения"), ("control_thread", "⚙️ Управление")):
            if not setting(db, key):
                result = await self.tg.topic(self.settings.tg_group_id, title)
                set_setting(db, key, result["message_thread_id"])
                db.commit()

    async def process(self, inbound):
        if inbound.platform == "tg":
            await self.on_tg(inbound.payload)
        elif inbound.platform == "max":
            await self.on_max(inbound.payload)

    async def on_tg(self, data):
        if "callback_query" in data:
            await self.on_tg_callback(data["callback_query"])
            return
        msg = data.get("message")
        if not msg or not msg.get("from") or msg["from"].get("is_bot"):
            return
        chat = msg.get("chat", {})
        uid = msg["from"]["id"]
        if chat.get("type") == "private":
            with self.sessions() as db:
                if allowed(db, uid):
                    handled = await self.admin_message(db, msg)
                    if not handled:
                        await self.tg.message(uid, "Вы зарегистрированы как сотрудник. "
                                                   "Используйте /admin или /find запрос.")
                    db.commit()
                    return
                c = get_contact(db, "tg", uid,
                                (msg["from"].get("first_name") or "Клиент") +
                                (" " + msg["from"]["last_name"] if msg["from"].get("last_name") else ""),
                                msg["from"].get("username", ""))
                initial = enqueue_welcome(db, c)
                text = msg.get("text", "")
                if text in ("/start", "/help", "Материалы", "/materials"):
                    if text in ("Материалы", "/materials") and not initial:
                        queue_materials(db, c)
                    db.commit()
                    return
                if text and text.startswith("/"):
                    db.commit()
                    return
                if not text and not tg_attachment(msg):
                    db.commit()
                    return
                await self.client_message(db, c, text, tg_msg=msg)
                db.commit()
            return
        if str(chat.get("id")) != str(self.settings.tg_group_id):
            return
        with self.sessions() as db:
            if not allowed(db, uid):
                return
            thread = msg.get("message_thread_id")
            if not thread:
                return
            c = db.scalar(select(Contact).where(Contact.topic_id == thread))
            if c is None:
                return
            text = msg.get("text") or msg.get("caption") or ""
            if text.startswith("/"):
                return
            attachment = tg_attachment(msg)
            if not text and not attachment:
                await self.tg.message(self.settings.tg_group_id,
                                      "Этот тип вложения не поддерживается для пересылки.", thread=thread)
                return
            if attachment and c.platform == "max":
                kind, file_id, filename = attachment
                push(db, c, {"kind": "tgmedia", "text": text, "media_kind": kind,
                             "file_id": file_id, "filename": filename, "actor": str(uid)})
            elif c.platform == "tg":
                push(db, c, {"kind": "tgcopy", "from_chat": chat["id"],
                             "message_id": msg["message_id"], "actor": str(uid)})
            else:
                push(db, c, {"kind": "text", "text": text, "actor": str(uid)})
            if c.status == "closed":
                c.status = "in_progress"
            db.add(Audit(contact_id=c.id, actor=str(uid), event="reply_queued", detail=text[:300]))
            db.commit()

    async def on_max(self, event):
        t = event.get("update_type")
        if t not in ("message_created", "bot_started", "bot_stopped", "message_callback"):
            return
        if t == "message_callback":
            cb = event.get("callback") or {}
            user = cb.get("user") or {}
            uid = user.get("user_id")
            if not uid:
                return
            with self.sessions() as db:
                c = get_contact(db, "max", uid, user.get("first_name") or "Клиент", user.get("username") or "")
                action = cb.get("payload")
                if action == "client:materials":
                    queue_materials(db, c)
                elif action == "client:watch_more":
                    queue_materials(db, c, gated=True)
                elif action == "client:contact":
                    push(db, c, {"kind": "text", "text": client_text(self.settings.ui_language, "prompt")})
                db.commit()
            if cb.get("callback_id"):
                try:
                    await self.mx.callback_answer(cb["callback_id"])
                except Exception:
                    log.warning("MAX callback acknowledgement failed", exc_info=True)
            return
        user = event.get("user") or (event.get("message") or {}).get("sender") or {}
        uid = user.get("user_id")
        if not uid or user.get("is_bot"):
            return
        with self.sessions() as db:
            c = get_contact(db, "max", uid, user.get("first_name") or "Клиент", user.get("username") or "")
            if t == "bot_stopped":
                c.available = False
                db.commit()
                return
            initial = enqueue_welcome(db, c)
            if t == "bot_started":
                db.commit()
                return
            msg = event.get("message") or {}
            if (msg.get("recipient") or {}).get("chat_type") not in (None, "dialog"):
                db.commit()
                return
            body = msg.get("body") or {}
            text = body.get("text") or ""
            if text in ("/start", "/help"):
                db.commit()
                return
            if text in ("/materials", "Материалы"):
                if not initial:
                    queue_materials(db, c)
                db.commit()
                return
            if text.startswith("/"):
                db.commit()
                return
            media = max_attachment(msg)
            if text or media:
                await self.client_message(db, c, text, max_media=media)
            db.commit()

    async def client_message(self, db, c, text, tg_msg=None, max_media=None):
        reopened = c.status in ("closed", "waiting")
        thread = await self.ensure_topic(db, c)
        c.status = "new" if reopened else (c.status if c.status != "closed" else "new")
        c.last_customer_at = now()
        c.reminded = 0
        if reopened:
            await self.notice(db, f"🔔 Обращение #{c.id} | {c.platform.upper()} | {c.name}\n"
                                   "Открыта заявка. Перейдите в клиентскую тему.\n" + self.topic_link(thread))
        if text:
            await self.tg.message(self.settings.tg_group_id, f"📩 Клиент:\n{text[:3900]}", thread=thread)
        if tg_msg and tg_attachment(tg_msg):
            await self.tg.copy(self.settings.tg_group_id, tg_msg["chat"]["id"],
                               tg_msg["message_id"], thread=thread)
        if max_media:
            try:
                await self.tg.media(self.settings.tg_group_id, max_media["kind"],
                                    max_media["url"], thread=thread)
            except Exception as exc:
                await self.tg.message(self.settings.tg_group_id,
                                      f"⚠️ Вложение MAX не удалось передать ({type(exc).__name__}).",
                                      thread=thread)
                raise
        db.add(Audit(contact_id=c.id, actor=f"{c.platform}:{c.external_id}",
                     event="client_message", detail=text[:300]))

    async def on_tg_callback(self, q):
        uid = q.get("from", {}).get("id")
        if not uid:
            return
        action = q.get("data", "")
        with self.sessions() as db:
            if action.startswith("client:"):
                c = find_contact(db, "tg", uid)
                if c:
                    if action == "client:materials":
                        queue_materials(db, c)
                    elif action == "client:watch_more":
                        queue_materials(db, c, gated=True)
                    elif action == "client:contact":
                        push(db, c, {"kind": "text", "text": client_text(self.settings.ui_language, "prompt")})
                    db.commit()
                    await self.tg.callback_answer(q["id"], client_text(self.settings.ui_language, "done"))
                return
            if not allowed(db, uid):
                await self.tg.callback_answer(q["id"], "Недостаточно прав")
                return
            if action.startswith("ticket:"):
                _, op, num = action.split(":")
                c = db.get(Contact, int(num))
                if not c:
                    return
                if op == "take":
                    if not c.owner_id:
                        c.owner_id = str(uid)
                    c.status = "in_progress"
                elif op == "close":
                    c.status = "closed"
                    c.reminded = 0
                elif op == "reopen":
                    c.status = "in_progress"
                db.add(Audit(contact_id=c.id, actor=str(uid), event=f"ticket_{op}"))
                db.commit()
                await self.tg.callback_answer(q["id"], "Статус обновлён")
                await self.tg.message(self.settings.tg_group_id,
                                      f"Статус #{c.id}: {c.status}. Ответственный: {c.owner_id or 'нет'}",
                                      thread=c.topic_id)
                return
            if action.startswith("open:"):
                c = db.get(Contact, int(action.split(":")[1]))
                if c:
                    await self.ensure_topic(db, c)
                    db.commit()
                    await self.tg.callback_answer(q["id"], "Диалог создан или найден")
                    await self.tg.message(uid, f"Диалог #{c.id} | {c.platform.upper()} | {c.name}:",
                        keyboard=[[{"text": "Перейти к теме", "url": self.topic_link(c.topic_id)}]])
                return
            if admin(db, uid):
                await self.admin_callback(db, uid, action)
                db.commit()
                await self.tg.callback_answer(q["id"], client_text(self.settings.ui_language, "done"))

    async def admin_message(self, db, msg):
        uid = str(msg["from"]["id"])
        text = (msg.get("text") or "").strip()
        can_admin = admin(db, uid)
        state = db.get(AdminState, uid)
        if state and state.action and can_admin and not text.startswith("/"):
            await self.apply_admin_state(db, msg, state)
            state.action = ""
            return True
        if text.startswith("/find "):
            await self.search(db, int(uid), text[6:].strip())
            return True
        if text == "/find":
            await self.tg.message(int(uid), "Используйте /find имя или /find номер")
            return True
        if not can_admin:
            return False
        if text in ("/admin", "/manage"):
            await self.admin_home(db, int(uid))
            return True
        if text.startswith("/add_manager "):
            mid = text.split(maxsplit=1)[1].strip()
            if mid.isdecimal():
                if not db.get(Staff, mid):
                    db.add(Staff(telegram_id=mid, role="manager"))
                await self.tg.message(int(uid), f"Менеджер {mid} добавлен")
            return True
        if text.startswith("/del_manager "):
            mid = text.split(maxsplit=1)[1].strip()
            if mid != uid and db.get(Staff, mid) and not admin(db, mid):
                db.delete(db.get(Staff, mid))
                await self.tg.message(int(uid), f"Менеджер {mid} удалён")
            return True
        if text.startswith("/max_file "):
            parts = text.split(maxsplit=2)
            if len(parts) != 3 or not parts[1].isdigit():
                await self.tg.message(int(uid), "Формат: /max_file ID имя_файла.mp4")
                return True
            item = db.get(Material, int(parts[1]))
            draft = version(db, False)
            if not item or item.version_id != draft.id:
                await self.tg.message(int(uid), "Материал не найден в черновике.")
                return True
            if media_kind(item) != "video":
                await self.tg.message(int(uid), "Альтернативное видео можно привязать только к видеоматериалу.")
                return True
            root = Path(self.settings.storage_dir).resolve()
            name = parts[2].strip()
            candidate = (root / "max" / name).resolve()
            if (not candidate.is_relative_to(root / "max") or
                    not candidate.is_file() or candidate.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm") or
                    candidate.stat().st_size > MAX_VIDEO_SIZE):
                await self.tg.message(int(uid), "Файл не найден в data/materials/max/ или превышает 250 МБ.")
                return True
            set_material_refs(item, max_path=str(candidate))
            item.max_token = ""
            await self.tg.message(int(uid), f"MAX-видео для материала #{item.id} обновлено. Опубликуйте черновик.")
            return True
        if text == "/versions":
            past = db.scalars(select(MaterialVersion).order_by(MaterialVersion.id.desc()).limit(8)).all()
            buttons = [[{"text": f"Версия {v.id}{' (опубликована)' if v.published else ''}",
                         "callback_data": f"adm:rollback:{v.id}"}] for v in past]
            await self.tg.message(int(uid), "Нажмите версию для возврата к её материалам:", keyboard=buttons)
            return True
        return False

    async def admin_home(self, db, uid):
        kb = [
            [{"text": "✏️ Приветствие", "callback_data": "adm:greeting"}],
            [{"text": "➕ Текст", "callback_data": "adm:add_text"},
             {"text": "➕ Ссылка", "callback_data": "adm:add_link"}],
            [{"text": "📎 Файл/фото/видео", "callback_data": "adm:add_file"}],
            [{"text": "🎬 Видео после кнопки", "callback_data": "adm:add_gated_video"}],
            [{"text": "🗣 Текст приглашения", "callback_data": "adm:campaign_cta"},
             {"text": "🔘 Текст кнопки", "callback_data": "adm:campaign_button"}],
            [{"text": "📋 Черновик", "callback_data": "adm:list"},
             {"text": "👁 Предпросмотр", "callback_data": "adm:preview"}],
            [{"text": "🚀 Опубликовать", "callback_data": "adm:publish"}],
            [{"text": "👥 Менеджеры", "callback_data": "adm:managers"},
             {"text": "🔎 Поиск", "callback_data": "adm:search"}],
        ]
        await self.tg.message(uid, "Администратор: изменения материалов делаются в черновике. "
                                  "Для выдачи клиентам нажмите «Опубликовать».", keyboard=kb)

    async def admin_callback(self, db, uid, action):
        if not action.startswith(("adm:", "m:")):
            return
        if action.startswith("m:"):
            _, operation, item = action.split(":")
            m = db.get(Material, int(item))
            draft = version(db, False)
            if not m or m.version_id != draft.id:
                return
            items = materials(db, draft)
            ix = next((i for i, x in enumerate(items) if x.id == m.id), -1)
            if operation == "delete":
                db.delete(m)
            elif operation == "edit":
                self.set_admin_state(db, uid, f"edit:{m.id}")
                await self.tg.message(uid, "Пришлите новый текст или ссылку.")
            elif operation in ("up", "down"):
                j = ix + (-1 if operation == "up" else 1)
                if 0 <= ix < len(items) and 0 <= j < len(items):
                    m.position, items[j].position = items[j].position, m.position
            if operation != "edit":
                await self.material_list(db, uid)
            return
        operation = action.split(":")[1]
        if operation in ("greeting", "add_text", "add_link", "add_file", "add_gated_video",
                         "campaign_cta", "campaign_button"):
            self.set_admin_state(db, uid, operation)
            help_text = {"greeting": "Пришлите новый текст приветствия.",
                         "add_text": "Пришлите текст нового материала.",
                         "add_link": "Пришлите HTTPS-ссылку.",
                         "add_file": "Пришлите фото, видео или документ (видео Telegram можно до 300 МБ). Подпись укажите в caption.",
                         "add_gated_video": "Пришлите дополнительное видео, которое будет выдаваться только по кнопке «Посмотреть», с подписью.",
                         "campaign_cta": "Пришлите текст приглашения перед кнопкой видео.",
                         "campaign_button": "Пришлите текст кнопки, например «Посмотреть»."}
            await self.tg.message(uid, help_text[operation])
        elif operation == "list":
            await self.material_list(db, uid)
        elif operation == "preview":
            await self.tg.message(uid, "Предпросмотр черновика:")
            await self.send_material_preview(db, uid)
        elif operation == "publish":
            ver = publish(db)
            await self.tg.message(uid, f"Опубликована версия материалов #{ver}. Новым клиентам она будет выдана автоматически.")
        elif operation == "rollback":
            ver = rollback(db, int(action.split(":")[2]))
            await self.tg.message(uid, f"Восстановлены материалы как новая версия #{ver}.")
        elif operation == "managers":
            items = db.scalars(select(Staff).order_by(Staff.telegram_id)).all()
            names = "\n".join(f"{s.telegram_id}: {s.role}" for s in items)
            await self.tg.message(uid, "Менеджеры:\n" + names +
                                  "\n\n/add_manager TELEGRAM_ID\n/del_manager TELEGRAM_ID")
        elif operation == "search":
            self.set_admin_state(db, uid, "search")
            await self.tg.message(uid, "Пришлите имя или внутренний ID клиента.")

    def set_admin_state(self, db, uid, action):
        state = db.get(AdminState, str(uid))
        if state is None:
            db.add(AdminState(user_id=str(uid), action=action))
        else:
            state.action = action

    async def apply_admin_state(self, db, msg, state):
        uid = int(state.user_id)
        action = state.action
        value = (msg.get("text") or "").strip()
        if action == "greeting":
            if not value:
                await self.tg.message(uid, "Нужен текст.")
                return
            set_setting(db, "welcome", value)
            await self.tg.message(uid, "Приветствие обновлено. Новые пользователи получат этот текст.")
            return
        if action == "search":
            await self.search(db, uid, value)
            return
        if action in ("campaign_cta", "campaign_button"):
            if not value:
                await self.tg.message(uid, "Пришлите непустой текст.")
                return
            set_setting(db, action, value)
            await self.tg.message(uid, "Текст сохранён. Следующие выдачи используют новую настройку.")
            return
        draft = version(db, False)
        if action.startswith("edit:"):
            m = db.get(Material, int(action.split(":")[1]))
            if m and m.version_id == draft.id:
                if media_kind(m) in ("text", "link") or value:
                    m.text = value
                    await self.tg.message(uid, "Текст или подпись материала изменены в черновике.")
            return
        if action == "add_link" and not re.fullmatch(r"https://[^\s]{3,2000}", value):
            await self.tg.message(uid, "Пришлите ссылку, начинающуюся с https://")
            return
        if action in ("add_text", "add_link") and not value:
            await self.tg.message(uid, "Нужно непустое содержимое.")
            return
        if action in ("add_file", "add_gated_video"):
            attachment = tg_attachment(msg)
            if not attachment:
                await self.tg.message(uid, "Нужен файл, фотография или видео.")
                return
            kind, file_id, name = attachment
            if action == "add_gated_video":
                if kind != "video":
                    await self.tg.message(uid, "Для материала после кнопки пришлите именно видео.")
                    return
                kind = "gated_video"
            folder = Path(self.settings.storage_dir)
            folder.mkdir(parents=True, exist_ok=True)
            safe_name = Path(name).name[:120]
            refs = {"tg_file_id": file_id}
            part = msg.get(kind.removeprefix("gated_"), {}) or {}
            declared_size = part.get("file_size") if isinstance(part, dict) else None
            if declared_size is None or declared_size <= MAX_SIZE:
                try:
                    content = await self.tg.file_bytes(file_id, MAX_SIZE)
                    path = folder / f"{uuid.uuid4().hex}_{safe_name}"
                    path.write_bytes(content)
                    refs["local_path"] = str(path)
                except PlatformError as exc:
                    if "слишком большой" not in str(exc).lower() and "превышает" not in str(exc).lower():
                        raise
            value = msg.get("caption") or ""
            name = safe_name
            path = json.dumps(refs, ensure_ascii=False)
        else:
            kind, path, name = action.removeprefix("add_"), "", ""
        if action not in ("add_text", "add_link", "add_file", "add_gated_video"):
            return
        position = (max((m.position for m in materials(db, draft)), default=0) + 10)
        db.add(Material(version_id=draft.id, position=position, kind=kind,
                        text=value, filename=name, filepath=str(path)))
        await self.tg.message(uid, "Материал добавлен в черновик. Опубликуйте комплект после проверки.")

    async def material_list(self, db, uid):
        draft = version(db, False)
        items = materials(db, draft)
        if not items:
            await self.tg.message(uid, "Черновик пока пуст.")
            return
        for i, item in enumerate(items):
            label = item.text[:90] if item.kind in ("text", "link") else item.filename
            buttons = [[{"text": "↑", "callback_data": f"m:up:{item.id}"},
                        {"text": "↓", "callback_data": f"m:down:{item.id}"},
                        {"text": "Удалить", "callback_data": f"m:delete:{item.id}"}]]
            buttons.append([{"text": "Изменить текст/подпись", "callback_data": f"m:edit:{item.id}"}])
            await self.tg.message(uid, f"{i+1}. [{item.kind}] {label}", keyboard=buttons)

    async def send_material_preview(self, db, uid):
        for m in materials(db, version(db, False)):
            if m.kind in ("text", "link"):
                await self.tg.message(uid, m.text)
            else:
                await self.send_tg_material(uid, m)

    async def search(self, db, uid, query):
        if not query:
            await self.tg.message(uid, "Введите запрос после /find.")
            return
        clause = Contact.id == int(query.lstrip("#")) if query.lstrip("#").isdigit() else Contact.name.ilike(f"%{query[:100]}%")
        matches = db.scalars(select(Contact).where(clause).limit(10)).all()
        if not matches:
            await self.tg.message(uid, "Ничего не найдено.")
            return
        for c in matches:
            await self.tg.message(uid, f"#{c.id} | {c.platform.upper()} | {c.name} | {c.status}",
                                  keyboard=[[{"text": "Открыть диалог", "callback_data": f"open:{c.id}"}]])

    async def delivery(self, job):
        with self.sessions() as db:
            c = db.get(Contact, job.contact_id)
            if not c or not c.available:
                raise PlatformError("Клиент недоступен", False)
            payload = job.payload
            kind = payload.get("kind")
            if c.platform == "tg":
                await self.deliver_tg(db, c, payload)
            else:
                await self.deliver_max(db, c, payload)
            if payload.get("actor"):
                c.last_answer_at = now()
                c.reminded = 0
                if c.status != "closed":
                    c.status = "waiting"
                db.add(Audit(contact_id=c.id, actor=payload["actor"], event="reply_sent"))
            db.commit()

    async def deliver_tg(self, db, c, payload):
        kind = payload.get("kind")
        if kind == "tgcopy":
            await self.tg.copy(int(c.external_id), payload["from_chat"], payload["message_id"])
        elif kind == "campaign_cta":
            await self.tg.message(int(c.external_id), payload["text"], keyboard=[[{
                "text": payload["button"], "callback_data": "client:watch_more"}]])
        elif kind == "material":
            m = db.get(Material, payload["material_id"])
            if m:
                await self.send_tg_material(c.external_id, m)
        elif kind == "welcome":
            await self.tg.message(int(c.external_id), payload["text"], keyboard=tg_menu(self.settings.ui_language))
        else:
            await self.tg.message(int(c.external_id), payload.get("text", ""))

    async def send_tg_material(self, uid, m):
        kind = media_kind(m)
        if kind in ("text", "link"):
            await self.tg.message(int(uid), m.text)
            return
        refs = material_refs(m)
        if refs.get("tg_file_id"):
            # sendVideo(file_id) is a NEW message, not a forwarded post.
            await self.tg.media(int(uid), kind, refs["tg_file_id"], m.text, name=m.filename)
            return
        path = local_material_path(m)
        if path and path.is_file():
            await self.tg.media(int(uid), kind, path.read_bytes(), m.text, name=m.filename)
            return
        raise PlatformError("Не найден файл материала для Telegram", False)

    async def deliver_max(self, db, c, payload):
        kind = payload.get("kind")
        if kind == "welcome":
            await self.mx.send(c.external_id, payload["text"], buttons=max_menu(self.settings.ui_language))
        elif kind == "campaign_cta":
            await self.mx.send(c.external_id, payload["text"], buttons=[[
                {"type": "callback", "text": payload["button"], "payload": "client:watch_more"}]])
        elif kind == "material":
            m = db.get(Material, payload["material_id"])
            if m:
                actual_kind = media_kind(m)
                if actual_kind in ("text", "link"):
                    await self.mx.send(c.external_id, m.text)
                else:
                    if not m.max_token:
                        path = local_material_path(m, for_max=True)
                        if not path or not path.is_file():
                            raise PlatformError(f"Нет MAX-файла для материала #{m.id}; добавьте через /max_file", False)
                        if actual_kind == "video" and path.stat().st_size > MAX_VIDEO_SIZE:
                            raise PlatformError("MAX видео превышает 250 МБ", False)
                        m.max_token = await self.mx.upload(actual_kind, path, m.filename)
                        db.commit()
                    from .platforms import MAX_UPLOAD_TYPES
                    await self.mx.send(c.external_id, m.text,
                                       media=(MAX_UPLOAD_TYPES[actual_kind], m.max_token))
        elif kind == "tgmedia":
            media_bytes = await self.tg.file_bytes(payload["file_id"], MAX_SIZE)
            token = await self.mx.upload(payload["media_kind"], media_bytes, payload["filename"])
            from .platforms import MAX_UPLOAD_TYPES
            await self.mx.send(c.external_id, payload.get("text", ""),
                               media=(MAX_UPLOAD_TYPES.get(payload["media_kind"], "file"), token))
        else:
            await self.mx.send(c.external_id, payload.get("text") or "")

    async def reminders(self):
        current = now()
        with self.sessions() as db:
            contacts = db.scalars(select(Contact).where(Contact.status.in_(["new", "in_progress"]),
                    Contact.last_customer_at.is_not(None))).all()
            for c in contacts:
                if c.last_answer_at and c.last_answer_at >= c.last_customer_at:
                    continue
                minutes = (current - c.last_customer_at).total_seconds() / 60
                stage = 2 if minutes >= 20 else 1 if minutes >= 10 else 0
                if stage > c.reminded:
                    await self.notice(db, f"⏰ Нет ответа на обращение #{c.id} "
                                          f"({c.platform.upper()} | {c.name}), {stage * 10} мин.\n"
                                          + (self.topic_link(c.topic_id) if c.topic_id else ""))
                    c.reminded = stage
            db.commit()
