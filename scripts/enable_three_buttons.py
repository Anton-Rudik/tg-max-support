#!/usr/bin/env python3
"""Update the deployed TG/MAX bot funnel without touching its DB or media files."""
import ast
import shutil
from datetime import datetime
from pathlib import Path

p = Path("app/service.py")
if not p.is_file():
    raise SystemExit("Запустите из корня проекта: app/service.py не найден")
s = p.read_text(encoding="utf-8")
if "def funnel_tg_menu():" in s:
    print("✅ Новое меню уже установлено.")
    raise SystemExit(0)

def replace_once(old, new, name):
    global s
    count = s.count(old)
    if count != 1:
        raise SystemExit(f"Неожиданная версия кода: {name} ({count} совпадений). Ничего не изменено.")
    s = s.replace(old, new, 1)

helper = '''# Client funnel: strategy, videos, account access.
def funnel_tg_menu():
    return [
        [{"text": "Стратегия 50.1%", "callback_data": "client:strategy"}],
        [{"text": "4 Видео", "callback_data": "client:videos"}],
        [{"text": "Доступ к счёту", "callback_data": "client:account"}],
    ]


def funnel_max_menu():
    return [
        [{"type": "callback", "text": "Стратегия 50.1%", "payload": "client:strategy"}],
        [{"type": "callback", "text": "4 Видео", "payload": "client:videos"}],
        [{"type": "callback", "text": "Доступ к счёту", "payload": "client:account"}],
    ]


def queue_strategy(db, contact):
    current = version(db, True)
    texts = [m for m in materials(db, current) if m.kind == "text"] if current else []
    chosen = next((m for m in texts if
                   "50.1" in m.text or "50,1" in m.text or "50.01" in m.text or
                   "стратег" in m.text.lower()), None)
    chosen = chosen or (texts[0] if texts else None)
    if chosen:
        push(db, contact, {"kind": "material", "material_id": chosen.id})
    else:
        push(db, contact, {"kind": "text", "text": "Материал стратегии пока не опубликован."})


def queue_four_video_funnel(db, contact):
    current = version(db, True)
    items = materials(db, current) if current else []
    for m in items:
        if m.kind == "video":
            push(db, contact, {"kind": "material", "material_id": m.id})
    if any(m.kind == "gated_video" for m in items):
        push(db, contact, {
            "kind": "campaign_cta",
            "text": setting(db, "campaign_cta", "Посмотрите заключительное видео 👇"),
            "button": setting(db, "campaign_button", "Посмотреть"),
        })
    elif not any(m.kind == "video" for m in items):
        push(db, contact, {"kind": "text", "text": "Видео пока не опубликованы."})


'''
replace_once("class SupportService:", helper + "class SupportService:", "функции меню")

# Do not auto-send material when a user starts the bot.
start = s.index("def enqueue_welcome(")
end = s.index("\ndef ticket_keyboard(", start)
block = s[start:end]
if "    queue_materials(db, contact)\n" in block:
    s = s[:start] + block.replace("    queue_materials(db, contact)\n", "", 1) + s[end:]

# Two distinct callback handlers: MAX and Telegram.
for indent in (" " * 16, " " * 20):
    old = (f'{indent}if action == "client:materials":\n'
           f'{indent}    queue_materials(db, c)')
    new = (f'{indent}if action in ("client:materials", "client:videos"):\n'
           f'{indent}    queue_four_video_funnel(db, c)\n'
           f'{indent}elif action == "client:strategy":\n'
           f'{indent}    queue_strategy(db, c)\n'
           f'{indent}elif action == "client:account":\n'
           f'{indent}    push(db, c, {{"kind": "text", "text": "🔧 Доступ к счёту — в разработке."}})')
    replace_once(old, new, "кнопки " + str(len(indent)))

replace_once("keyboard=tg_menu(self.settings.ui_language)",
             "keyboard=funnel_tg_menu()", "приветствие Telegram")
replace_once("buttons=max_menu(self.settings.ui_language)",
             "buttons=funnel_max_menu()", "приветствие MAX")

# After successful delivery of a gated video, queue a manager CTA.
replace_once(
    '            if payload.get("actor"):\n                c.last_answer_at = now()',
    '            if payload.get("kind") == "material":\n'
    '                sent_material = db.get(Material, payload.get("material_id"))\n'
    '                if sent_material is not None and is_gated(sent_material):\n'
    '                    push(db, c, {"kind": "manager_cta"})\n'
    '            if payload.get("actor"):\n                c.last_answer_at = now()',
    "кнопка менеджера после финального видео",
)

tg_start = s.index("    async def deliver_tg(")
tg_end = s.index("    async def send_tg_material(", tg_start)
tg_old = '''        elif kind == "campaign_cta":
            await self.tg.message(int(c.external_id), payload["text"], keyboard=[[{
                "text": payload["button"], "callback_data": "client:watch_more"}]])'''
tg_new = tg_old + '''
        elif kind == "manager_cta":
            await self.tg.message(int(c.external_id),
                "💬 Хотите обсудить материалы? Свяжитесь с менеджером 👇",
                keyboard=[[{"text": "Связаться с менеджером", "callback_data": "client:contact"}]])'''
tg_block = s[tg_start:tg_end]
if tg_block.count(tg_old) != 1:
    raise SystemExit("Обработчик Telegram изменён. Ничего не изменено.")
s = s[:tg_start] + tg_block.replace(tg_old, tg_new) + s[tg_end:]

mx_start = s.index("    async def deliver_max(")
mx_end = s.find("\n    async def ", mx_start + 1)
mx_end = len(s) if mx_end < 0 else mx_end
mx_old = '''        elif kind == "campaign_cta":
            await self.mx.send(c.external_id, payload["text"], buttons=[[
                {"type": "callback", "text": payload["button"], "payload": "client:watch_more"}]])'''
mx_new = mx_old + '''
        elif kind == "manager_cta":
            await self.mx.send(c.external_id,
                "💬 Хотите обсудить материалы? Свяжитесь с менеджером 👇",
                buttons=[[{"type": "callback", "text": "Связаться с менеджером", "payload": "client:contact"}]])'''
mx_block = s[mx_start:mx_end]
if mx_block.count(mx_old) != 1:
    raise SystemExit("Обработчик MAX изменён. Ничего не изменено.")
s = s[:mx_start] + mx_block.replace(mx_old, mx_new) + s[mx_end:]

ast.parse(s, filename="app/service.py")
backup = p.with_name("service.py.bak_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
shutil.copy2(p, backup)
p.write_text(s, encoding="utf-8")
print("✅ Три кнопки установлены в Telegram и MAX.")
print("✅ /start отправляет только приветствие и меню.")
print("✅ После успешной выдачи видео №4 появится кнопка менеджера.")
print("Резервная копия:", backup)
