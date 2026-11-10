# TG + MAX Support Desk

**Open-source starter for a shared Telegram + MAX customer support inbox.**

[Русская документация](README.ru.md) · [Deploy with PM2 (RU)](docs/DEPLOY_RU.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

> **Community preview:** local automated tests pass, but real Telegram/MAX end-to-end API integrations have **not yet been validated**. This is not a production-ready customer-service system. Verify platform contracts, security, backups and data-retention requirements before using real customer data.

## What it does

A company runs a Telegram bot and optionally a MAX bot. Staff handle incoming messages in **one private Telegram supergroup with topics**. Each customer account gets one persistent topic when they or a staff member first initiate a conversation. Multiple staff members may reply in the same topic; customers see replies from the **bot**, not from individual employees.

```text
Telegram customers ─┐
                    ├── FastAPI webhooks → shared PostgreSQL + delivery queue
MAX customers ──────┘                             │
                                                  ▼
                                Private Telegram forum group
                                    └── One topic per account
                                        ├── Agent 1 replies
                                        └── Agent 2 replies
                                                  │
                                                  ▼
                                   Customer receives bot replies
```

Features included in the current prototype:

- First-contact greeting followed by an ordered pack of text, links, documents, images and video.
- Materials editor in a private Telegram admin chat: add, remove, reorder, preview, publish versions, restore earlier versions.
- Separate topics for incoming conversations; topics persist when tickets reopen.
- Any authorized staff member can answer, even if another agent marked the ticket as taken.
- Ticket status, staff search, 10- and 20-minute unanswered-message reminders.
- Durable SQL queue, webhook deduplication and retries for temporary delivery failures.
- Russian/English **client** menu and configurable brand/greeting. Staff administration UI currently uses Russian.
- One Python/FastAPI process under **PM2**, no Docker required.

## Quick start (Linux + PM2)

Prerequisites: Python 3.11+, PM2, PostgreSQL, a Telegram bot, a private Telegram supergroup with topics, a public HTTPS endpoint (for example Nginx + a trusted certificate). MAX credentials are optional until you enable its integration.

```bash
git clone https://github.com/Anton-Rudik/tg-max-support.git
cd tg-max-support
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env: Telegram token, group ID, admin IDs, webhook secret,
# DATABASE_URL and PUBLIC_BASE_URL; configure MAX only if available.
.venv/bin/python -m pytest -q
pm2 start ecosystem.config.cjs
pm2 save
```

Prepare the PostgreSQL database and HTTPS reverse proxy before the PM2 start. Sample reverse-proxy config: [`nginx.conf.example`](nginx.conf.example). By default the application listens on `127.0.0.1:8000`; set `APP_PORT` before starting PM2 to change it. Run one instance (`--workers 1`); this prototype uses an in-process worker.

Once the HTTPS endpoint responds, register the webhook(s):

```bash
.venv/bin/python -m scripts.register_webhooks --platform tg
# After MAX bot credentials are available:
.venv/bin/python -m scripts.register_webhooks --platform max
# Or register both: --platform all
```

Open the Telegram bot in a private chat with an admin account and send `/admin`. To authorize staff: `/add_manager 123456789`; to find a registered customer: `/find <name or ID>`.

**Never commit `.env`, token values, database dumps, or customer conversations.** The `.gitignore` excludes local secrets and data. Review the file before committing additional assets.

## Configuration

| Environment variable | Description |
| --- | --- |
| `TG_BOT_TOKEN`, `TG_GROUP_ID` | Telegram token and support forum supergroup ID |
| `TG_WEBHOOK_SECRET` | Telegram webhook secret header value |
| `ADMIN_IDS` | Comma-separated Telegram numeric IDs |
| `MAX_BOT_TOKEN`, `MAX_WEBHOOK_SECRET` | Optional MAX token and webhook secret |
| `PUBLIC_BASE_URL` | Public HTTPS origin used for webhook registration |
| `DATABASE_URL` | SQLAlchemy PostgreSQL connection URL |
| `STORAGE_DIR` | Directory for material files, outside Git |
| `PROJECT_NAME` | Organization label in the default welcome text |
| `UI_LANGUAGE` | `ru` (default) or `en` for client menus/messages |
| `WELCOME_TEXT` | Optional custom initial greeting (can also be changed in `/admin`) |

The first boot stores the welcome text in the database. Changing `WELCOME_TEXT` later will **not** overwrite an administrator-edited greeting. Change it via `/admin` instead.

## Project structure

```text
app/                      server, platform APIs, database models and support logic
scripts/register_webhooks.py
  webhook registration (tg/max/all)
tests/                    mocked API and core behavior tests
docs/DEPLOY_RU.md         detailed deployment walkthrough
.env.example              configuration template, no credentials
nginx.conf.example        example reverse proxy
ecosystem.config.cjs      PM2 single-process configuration
LICENSE                   MIT
```

## Tests & current limitations

```bash
.venv/bin/python -m pytest -q
```

Tests use mocked platform responses and SQLite; production uses PostgreSQL. Real platform media formats, large attachments, API throttling, subscription lifecycle, customer deletion, failover and backups need further integration testing and hardening. External APIs cannot guarantee exactly-once delivery after ambiguous network failures. Telegram group membership gives staff visibility into all group topics: **bot roles are not read permissions**.

See [SECURITY.md](SECURITY.md) before any public deployment and [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. This project is an independent community project; it is not affiliated with Telegram or MAX.

## License

[MIT](LICENSE) — usable and modifiable by individuals and organizations.
