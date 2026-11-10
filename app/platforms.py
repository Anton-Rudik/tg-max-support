import asyncio
from pathlib import Path
from urllib.parse import urlparse
import httpx

MAX_UPLOAD_TYPES = {"photo": "image", "video": "video", "document": "file", "audio": "audio", "voice": "audio"}
TG_METHODS = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument", "audio": "sendAudio", "voice": "sendVoice"}
TG_PARTS = {"photo": "photo", "video": "video", "document": "document", "audio": "audio", "voice": "voice"}


class PlatformError(RuntimeError):
    def __init__(self, description, retryable=True):
        self.retryable = retryable
        super().__init__(str(description)[:600])


class TelegramAPI:
    def __init__(self, token, http):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.http = http

    async def call(self, method, params=None, files=None):
        r = await self.http.post(f"{self.base}/{method}", data=params if files else None,
                                 json=params if not files else None, files=files, timeout=60)
        try:
            data = r.json()
        except ValueError as exc:
            raise PlatformError(f"Telegram invalid response: {r.status_code}") from exc
        if r.status_code >= 400 or not data.get("ok"):
            retry_after = data.get("parameters", {}).get("retry_after")
            if retry_after:
                await asyncio.sleep(min(int(retry_after), 5))
            raise PlatformError(data.get("description", f"Telegram HTTP {r.status_code}"),
                                retryable=r.status_code == 429 or r.status_code >= 500)
        return data.get("result")

    async def message(self, chat_id, text, thread=None, keyboard=None):
        data = {"chat_id": chat_id, "text": text[:4096]}
        if thread is not None:
            data["message_thread_id"] = thread
        if keyboard:
            data["reply_markup"] = {"inline_keyboard": keyboard}
        return await self.call("sendMessage", data)

    async def callback_answer(self, cb_id, text=""):
        return await self.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:200]})

    async def topic(self, group_id, title):
        return await self.call("createForumTopic", {"chat_id": group_id, "name": title[:128]})

    async def copy(self, chat_id, from_chat_id, message_id, thread=None):
        payload = {"chat_id": chat_id, "from_chat_id": from_chat_id,
                   "message_id": message_id}
        if thread is not None:
            payload["message_thread_id"] = thread
        return await self.call("copyMessage", payload)

    async def file_bytes(self, file_id, max_bytes=20 * 1024 * 1024):
        metadata = await self.call("getFile", {"file_id": file_id})
        if metadata.get("file_size", 0) > max_bytes:
            raise PlatformError("Файл слишком большой для загрузки из Telegram", False)
        path = metadata["file_path"]
        r = await self.http.get(f"{self.file_base}/{path}", timeout=60)
        r.raise_for_status()
        if len(r.content) > max_bytes:
            raise PlatformError("Файл превышает допустимый размер", False)
        return r.content

    async def media(self, chat_id, kind, ref, text="", thread=None, name="file"):
        method = TG_METHODS[kind]
        payload = {"chat_id": chat_id}
        if thread is not None:
            payload["message_thread_id"] = thread
        if text:
            payload["caption"] = text[:1024]
        if isinstance(ref, bytes):
            return await self.call(method, payload, files={TG_PARTS[kind]: (name, ref)})
        payload[TG_PARTS[kind]] = ref
        return await self.call(method, payload)


class MaxAPI:
    def __init__(self, token, http, base):
        self.http = http
        self.base = base.rstrip("/")
        self.headers = {"Authorization": token}

    async def call(self, method, path, params=None, data=None):
        r = await self.http.request(method, self.base + path, headers=self.headers,
                                    params=params, json=data, timeout=60)
        try:
            result = r.json()
        except ValueError as exc:
            raise PlatformError(f"MAX invalid response: HTTP {r.status_code}") from exc
        if r.status_code >= 400 or result.get("success") is False:
            err = result.get("message") or result.get("code") or f"MAX HTTP {r.status_code}"
            transient = r.status_code == 429 or r.status_code >= 500 or "not.ready" in str(err)
            raise PlatformError(err, transient)
        return result

    async def send(self, user_id, text="", buttons=None, media=None):
        payload = {"text": text[:4000]}
        attachments = []
        if media:
            attachments.append({"type": media[0], "payload": {"token": media[1]}})
        if buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})
        if attachments:
            payload["attachments"] = attachments
        return await self.call("POST", "/messages", {"user_id": int(user_id)}, payload)

    async def callback_answer(self, callback_id):
        return await self.call("POST", "/answers", {"callback_id": callback_id}, {})

    async def upload(self, kind, content: bytes | Path, filename="file"):
        remote_kind = MAX_UPLOAD_TYPES.get(kind, "file")
        start = await self.call("POST", "/uploads", {"type": remote_kind})
        upload_url = start.get("url")
        if not upload_url:
            raise PlatformError("MAX did not return upload URL")
        # The upload URL is supplied by MAX; reject an unexpected host to avoid SSRF.
        host = (urlparse(upload_url).hostname or "").lower()
        if not (host == "oneme.ru" or host.endswith(".oneme.ru") or
                host == "max.ru" or host.endswith(".max.ru")):
            raise PlatformError("Unexpected MAX upload host", False)
        if isinstance(content, Path):
            # Stream a local MAX-optimized video; never load 250 MB in RAM.
            with content.open("rb") as source:
                r = await self.http.post(upload_url, files={"data": (filename[:200], source)}, timeout=600)
        else:
            r = await self.http.post(upload_url, files={"data": (filename[:200], content)}, timeout=120)
        if r.status_code >= 400:
            raise PlatformError(f"MAX upload error {r.status_code}", r.status_code >= 500)
        result = r.json()
        token = result.get("token") or start.get("token")
        if remote_kind == "image":
            photos = result.get("photos") or {}
            token = token or next((v.get("token") for v in photos.values() if isinstance(v, dict) and v.get("token")), None)
        if not token:
            raise PlatformError("MAX media token missing")
        return token


def tg_attachment(msg: dict):
    if msg.get("photo"):
        return "photo", msg["photo"][-1]["file_id"], "photo.jpg"
    for kind in ("video", "document", "audio", "voice"):
        if msg.get(kind):
            part = msg[kind]
            return kind, part["file_id"], part.get("file_name") or kind + ".bin"
    return None


def max_attachment(msg: dict):
    body = msg.get("body") or {}
    for part in body.get("attachments") or []:
        kind = part.get("type")
        if kind in ("image", "video", "file", "audio"):
            payload = part.get("payload") or {}
            url = payload.get("url")
            if url:
                return {"kind": {"image": "photo", "file": "document"}.get(kind, kind), "url": url}
    return None


CLIENT_TEXTS = {
    "ru": {
        "welcome": "Здравствуйте! Добро пожаловать в {project_name}. Если возникнут вопросы, напишите нам в этот чат.",
        "materials": "📚 Материалы",
        "contact": "💬 Написать менеджеру",
        "prompt": "Напишите ваш вопрос прямо в этот чат — наша команда ответит.",
        "done": "Готово",
    },
    "en": {
        "welcome": "Welcome to {project_name}! Feel free to message us here if you have any questions.",
        "materials": "📚 Resources",
        "contact": "💬 Contact support",
        "prompt": "Send your question in this chat and our team will respond.",
        "done": "Done",
    },
}


def client_text(language, key, **kwargs):
    lang = language if language in CLIENT_TEXTS else "ru"
    return CLIENT_TEXTS[lang][key].format(**kwargs)


def tg_menu(language="ru"):
    return [[{"text": client_text(language, "materials"), "callback_data": "client:materials"}],
            [{"text": client_text(language, "contact"), "callback_data": "client:contact"}]]


def max_menu(language="ru"):
    return [[{"type": "callback", "text": client_text(language, "materials"), "payload": "client:materials"}],
            [{"type": "callback", "text": client_text(language, "contact"), "payload": "client:contact"}]]
