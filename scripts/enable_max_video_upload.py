#!/usr/bin/env python3
"""Enable MAX admin media binding from a MAX chat, without SFTP."""
import ast
import hashlib
import hmac
import json
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

path = Path("app/service.py")
marker = "async def handle_max_admin_upload(self, db, uid, text, msg):"
if not path.is_file():
    raise SystemExit("Запустите скрипт из корня проекта: app/service.py не найден.")
src = path.read_text(encoding="utf-8")
if marker in src:
    print("Уже установлено. Повторное применение не требуется.")
    raise SystemExit(0)

for anchor in (
    "    async def on_max(self, event):",
    "    async def client_message(",
    "    async def admin_message(self, db, msg):",
    "    async def deliver_max(self, db, c, payload):",
    '        if text in ("/admin", "/manage"):',
):
    if src.count(anchor) != 1:
        raise SystemExit("Неподдерживаемая версия файла, изменений нет: " + anchor)

helper = '''    async def handle_max_admin_upload(self, db, uid, text, msg):
        """Manage secure pairing and reuse videos sent directly to MAX bot."""
        from .models import ConfigValue, set_setting

        text = (text or "").strip()
        admin_key = f"max:admin:{uid}"
        is_admin = db.get(ConfigValue, admin_key) is not None

        if text.startswith("/max_link"):
            parts = text.split()
            if len(parts) != 2:
                await self.mx.send(uid, "Формат: /max_link КОД_ИЗ_TELEGRAM")
                return True
            record = db.get(ConfigValue, "max:link_code")
            try:
                saved = json.loads(record.value) if record else {}
            except (ValueError, TypeError):
                saved = {}
            digest = hashlib.sha256(parts[1].encode()).hexdigest()
            valid = (saved.get("expires", 0) > time.time()
                     and hmac.compare_digest(saved.get("sha256", ""), digest))
            if not valid:
                await self.mx.send(uid, "Код неверен или истёк. В Telegram запросите /max_link снова.")
                return True
            if not is_admin:
                db.add(ConfigValue(key=admin_key, value="1"))
            db.delete(record)
            await self.mx.send(uid, "✅ MAX-админ привязан. Теперь отправьте /max_video ID из черновика Telegram, затем отправьте видео сюда.")
            return True

        if text.startswith("/max_video"):
            if not is_admin:
                await self.mx.send(uid, "Доступ закрыт. Сначала привяжите MAX через /max_link.")
                return True
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdecimal():
                await self.mx.send(uid, "Формат: /max_video ID_ИЗ_ЧЕРНОВИКА")
                return True
            m = db.get(Material, int(parts[1]))
            draft = version(db, False)
            if not m or not draft or m.version_id != draft.id or media_kind(m) != "video":
                await self.mx.send(uid, "Такого видео нет в черновике. В Telegram откройте /admin → Черновик.")
                return True
            set_setting(db, f"max:target:{uid}", str(m.id))
            await self.mx.send(uid, f"Теперь пришлите в этот чат видео для материала #{m.id} (до 250 МБ).")
            return True

        if not is_admin:
            return False
        target = db.get(ConfigValue, f"max:target:{uid}")
        if target is None:
            return False
        attachments = (msg.get("body") or {}).get("attachments") or []
        video = next((a for a in attachments if a.get("type") == "video"), None)
        if video is None:
            return False
        token = (video.get("payload") or {}).get("token")
        if not isinstance(token, str) or not token:
            await self.mx.send(uid, "MAX не передал токен видео. Пришлите обычное видео, не ссылку.")
            return True
        m = db.get(Material, int(target.value))
        draft = version(db, False)
        if not m or not draft or m.version_id != draft.id or media_kind(m) != "video":
            db.delete(target)
            await self.mx.send(uid, "Черновик изменился. Заново введите /max_video с актуальным ID.")
            return True
        m.max_token = token
        db.delete(target)
        db.add(Audit(contact_id=None, actor=f"max:{uid}",
                     event="max_video_bound", detail=f"material_id={m.id}"))
        await self.mx.send(uid, f"✅ Видео #{m.id} привязано! После загрузки всех видео опубликуйте черновик в Telegram.")
        return True

'''
src = "import hashlib\nimport hmac\nimport secrets\nimport time\n" + src
src = src.replace("    async def on_max(self, event):",
                  helper + "    async def on_max(self, event):", 1)

tg_patch = '''        if text == "/max_link":
            code = secrets.token_urlsafe(18)
            from .models import set_setting
            set_setting(db, "max:link_code", json.dumps({
                "sha256": hashlib.sha256(code.encode()).hexdigest(),
                "expires": time.time() + 600,
            }))
            await self.tg.message(int(uid),
                "🔐 Код привязки MAX действителен 10 минут. В личном чате MAX-бота отправьте:\\n"
                + "/max_link " + code + "\\nНикому не передавайте код.")
            return True
'''
src = src.replace('        if text in ("/admin", "/manage"):',
                  tg_patch + '        if text in ("/admin", "/manage"):', 1)
begin = src.index("    async def on_max(self, event):")
end = src.index("    async def client_message(", begin)
part = src[begin:end]
needle = '            text = body.get("text") or ""'
if part.count(needle) != 1:
    raise SystemExit("Структура on_max отличается, изменений нет.")
part = part.replace(
    needle,
    needle + '\n            if await self.handle_max_admin_upload(db, uid, text, msg):\n'
             '                db.commit()\n                return',
    1,
)
src = src[:begin] + part + src[end:]
try:
    ast.parse(src, filename="app/service.py")
except SyntaxError as exc:
    raise SystemExit("Синтаксис не прошёл проверку, изменений нет: " + str(exc))

backup = path.with_name("service.py.bak_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
shutil.copy2(path, backup)
path.write_text(src, encoding="utf-8")
print("✅ MAX-админ может загружать ролики прямо в MAX.")
print("Копия файла:", backup)
