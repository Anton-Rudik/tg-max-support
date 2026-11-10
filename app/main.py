import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import select, update, and_
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .models import Inbound, Outbound, create_db, initialize, now
from .platforms import TelegramAPI, MaxAPI, PlatformError
from .service import SupportService

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def inbound_key(platform, payload):
    if platform == "tg":
        return "tg:" + str(payload["update_id"])
    typ = payload.get("update_type", "unknown")
    if typ == "message_created":
        return "max:msg:" + str((payload.get("message") or {}).get("body", {}).get("mid") or
                                   f"{payload.get('timestamp')}:{(payload.get('message') or {}).get('sender', {}).get('user_id')}")
    if typ == "message_callback":
        return "max:cb:" + str((payload.get("callback") or {}).get("callback_id"))
    return f"max:{typ}:{payload.get('timestamp')}:{(payload.get('user') or {}).get('user_id')}"


def delay(attempts):
    return min(600, 2 ** min(attempts, 9))


async def handle_inbound_once(app):
    sessions = app.state.sessions
    with sessions() as db:
        item = db.scalar(select(Inbound).where(Inbound.status == "pending", Inbound.next_try <= now())
                         .order_by(Inbound.id).limit(1))
        if not item:
            return False
        ident = item.id
        item.status = "processing"
        db.commit()
    try:
        with sessions() as db:
            item = db.get(Inbound, ident)
            await app.state.support.process(item)
        with sessions.begin() as db:
            row = db.get(Inbound, ident)
            row.status = "done"
            row.last_error = ""
    except Exception as exc:
        log.exception("Inbound processing failed id=%s", ident)
        with sessions.begin() as db:
            row = db.get(Inbound, ident)
            row.attempts += 1
            row.last_error = str(exc)[:500]
            retryable = not isinstance(exc, PlatformError) or exc.retryable
            if row.attempts >= 8 or not retryable:
                row.status = "failed"
            else:
                row.status = "pending"
                row.next_try = now() + timedelta(seconds=delay(row.attempts))
    return True


async def handle_outbound_once(app):
    sessions = app.state.sessions
    prior = aliased(Outbound)
    with sessions() as db:
        item = db.scalar(select(Outbound).where(
            Outbound.status == "pending", Outbound.next_try <= now(),
            ~select(prior.id).where(prior.contact_id == Outbound.contact_id,
                                    prior.id < Outbound.id,
                                    prior.status.in_(["pending", "processing"])).exists()
        ).order_by(Outbound.id).limit(1))
        if not item:
            return False
        ident = item.id
        item.status = "processing"
        db.commit()
    try:
        with sessions() as db:
            job = db.get(Outbound, ident)
            await app.state.support.delivery(job)
        with sessions.begin() as db:
            job = db.get(Outbound, ident)
            job.status = "sent"
            job.last_error = ""
    except Exception as exc:
        log.exception("Outbound processing failed id=%s", ident)
        with sessions.begin() as db:
            job = db.get(Outbound, ident)
            job.attempts += 1
            job.last_error = str(exc)[:500]
            retryable = not isinstance(exc, PlatformError) or exc.retryable
            if job.attempts >= 8 or not retryable:
                job.status = "failed"
            else:
                job.status = "pending"
                job.next_try = now() + timedelta(seconds=delay(job.attempts))
        # Notify staff only on the terminal failure, not for transient retries.
        if job.status == "failed":
            try:
                from .models import Contact
                with sessions() as db:
                    c = db.get(Contact, job.contact_id)
                    if c:
                        await app.state.support.notice(db,
                            f"⚠️ Не удалось доставить сообщение клиенту #{c.id} "
                            f"({c.platform.upper()} | {c.name}). Причина: {job.last_error[:250]}")
            except Exception:
                log.exception("Could not notify staff of delivery failure")
    return True


async def worker_loop(app):
    last_reminder = 0.0
    last_setup = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            t = loop.time()
            if t - last_setup > 120:
                with app.state.sessions() as db:
                    try:
                        await app.state.support.ensure_internal_topics(db)
                        db.commit()
                    except Exception:
                        log.exception("Service topic setup failed")
                last_setup = t
            if t - last_reminder > 30:
                await app.state.support.reminders()
                last_reminder = t
            inbound_done = await handle_inbound_once(app)
            outbound_done = await handle_outbound_once(app)
            if outbound_done:
                # MAX permits at most 2 messages/s to one chat; serialized sends keep within limit.
                await asyncio.sleep(0.55)
            elif not inbound_done:
                await asyncio.sleep(0.6)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Worker loop error")
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine, sessions = create_db()
    initialize(engine, sessions, settings)
    with sessions.begin() as db:
        db.execute(update(Inbound).where(Inbound.status == "processing").values(status="pending"))
        db.execute(update(Outbound).where(Outbound.status == "processing").values(status="pending"))
    app.state.sessions = sessions
    async with httpx.AsyncClient(follow_redirects=False) as http:
        app.state.support = SupportService(sessions,
            TelegramAPI(settings.tg_bot_token, http),
            MaxAPI(settings.max_bot_token, http, settings.max_api_base), settings)
        task = asyncio.create_task(worker_loop(app))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            engine.dispose()


app = FastAPI(title="TG + MAX Support", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


def accept_event(app, platform, payload):
    key = inbound_key(platform, payload)
    with app.state.sessions.begin() as db:
        if db.scalar(select(Inbound.id).where(Inbound.key == key)) is not None:
            return False
        db.add(Inbound(key=key, platform=platform, payload=payload))
    return True


@app.post("/webhooks/telegram")
async def webhook_telegram(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    settings = get_settings()
    if not settings.tg_webhook_secret or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, settings.tg_webhook_secret
    ):
        raise HTTPException(403, "Invalid Telegram webhook secret")
    data = await request.json()
    if not isinstance(data, dict) or "update_id" not in data:
        raise HTTPException(400, "Invalid update")
    accept_event(request.app, "tg", data)
    return {"ok": True}


@app.post("/webhooks/max")
async def webhook_max(request: Request, x_max_bot_api_secret: str = Header(default="")):
    settings = get_settings()
    if not settings.max_webhook_secret or not hmac.compare_digest(
        x_max_bot_api_secret, settings.max_webhook_secret
    ):
        raise HTTPException(403, "Invalid MAX webhook secret")
    data = await request.json()
    if not isinstance(data, dict) or "update_type" not in data:
        raise HTTPException(400, "Invalid update")
    accept_event(request.app, "max", data)
    return {"ok": True}
