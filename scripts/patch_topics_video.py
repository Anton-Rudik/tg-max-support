#!/usr/bin/env python3
"""Create a user topic on /start and notify their topic on watch-more click."""
import ast
import re
from datetime import datetime
from pathlib import Path

path = Path("app/service.py")
source = path.read_text(encoding="utf-8")
if "async def mark_watch_more(self, db, c):" in source:
    raise SystemExit("Изменения уже внесены. Повторная правка не требуется.")

start_anchor = "    async def on_tg(self, data):"
max_anchor = "    async def on_max(self, event):"
callback_anchor = "    async def on_tg_callback(self, q):"
if any(source.count(marker) != 1 for marker in (start_anchor, max_anchor, callback_anchor)):
    raise SystemExit("Нужный обработчик не найден. Файл не изменён.")
tg_start = source.index(start_anchor)
max_start = source.index(max_anchor)
if max_start <= tg_start:
    raise SystemExit("Неожданный порядок обработчиков. Файл не изменён.")
tg_part = source[tg_start:max_start]
tg_welcome = "                initial = enqueue_welcome(db, c)"
if tg_part.count(tg_welcome) != 1:
    raise SystemExit("Не найден старт Telegram. Файл не изменён.")
tg_part = tg_part.replace(
    tg_welcome,
    '                if (msg.get("text") or "").split(" ", 1)[0].split("@", 1)[0] == "/start":\n'
    '                    await self.ensure_topic(db, c)\n'
    + tg_welcome, 1
)
source = source[:tg_start] + tg_part + source[max_start:]

max_started = '            if t == "bot_started":\n                db.commit()'
if source.count(max_started) != 1:
    raise SystemExit("Не найден старт MAX. Файл не изменён.")
source = source.replace(
    max_started,
    '            if t == "bot_started":\n'
    '                await self.ensure_topic(db, c)\n'
    '                db.commit()', 1
)

max_start_text = '            if text in ("/start", "/help"):\n                db.commit()'
if source.count(max_start_text) != 1:
    raise SystemExit("Не найдена команда MAX /start. Файл не изменён.")
source = source.replace(
    max_start_text,
    '            if text in ("/start", "/help"):\n'
    '                if text == "/start":\n'
    '                    await self.ensure_topic(db, c)\n'
    '                db.commit()', 1
)

helper = '''    async def mark_watch_more(self, db, c):
        await self.ensure_topic(db, c)
        already_sent = db.scalar(
            select(Audit.id).where(
                Audit.contact_id == c.id,
                Audit.event == "watch_more_clicked",
            ).limit(1)
        )
        if already_sent is not None:
            return
        await self.tg.message(
            self.settings.tg_group_id,
            "✅ Все видео просмотрены",
            thread=c.topic_id,
        )
        db.add(Audit(
            contact_id=c.id,
            actor=f"{c.platform}:{c.external_id}",
            event="watch_more_clicked",
            detail="Пользователь нажал кнопку «Посмотреть»",
        ))

'''
source = source.replace(callback_anchor, helper + callback_anchor, 1)
pattern = re.compile(r"(?m)^([ \t]+)queue_materials\(db, c, gated=True\)[ \t]*$")
source, count = pattern.subn(
    lambda m: m.group(0) + "\n" + m.group(1) +
    "await self.mark_watch_more(db, c)", source
)
if count != 2:
    raise SystemExit(
        f"Ожидалось 2 обработчика «Посмотреть», найдено {count}. Файл не изменён."
    )

ast.parse(source, filename=str(path))
backup = path.with_name("service.py.bak_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
path.write_text(source, encoding="utf-8")
print("Готово. Изменён:", path)
print("Резервная копия:", backup)
print("Темы создаются при /start; уведомление по «Посмотреть» отправляется один раз.")
