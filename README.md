# Amadeus-Neo

Amadeus-Neo is a Discord bot created for the [Beyond the Gate](https://discord.gg/YBmZzfA) Discord server.
Written in Python and leveraging `discord.py` and sqlite, it provides a simple and modular bot framework.

---
## Features

Amadeus-Neo is built around optional per-server modules. Cogs are loaded from
the configured environment and each module can be enabled or disabled in Discord
with `/amadeus module enable <module>` and `/amadeus module disable <module>`.

| Module | What it does                                                                                                                                                                                                                                         |
|---|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [bouncer](docs/modules/bouncer.md) | CAPTCHA verification for new members. It posts a verification panel, DMs users a CAPTCHA, grants the configured verified role on success, kicks after too many failed attempts, and can bulk-backfill verified roles.                                |
| [honeypot](docs/modules/honeypot.md) | Honeypot channel moderation. Any user who posts in the configured honeypot channel has their message deleted and receives the configured action: remove role, timeout, kick, or ban. Optional alerts are sent to the admin channel.                  |
| [boost](docs/modules/boost.md) | Automates server boost perks. Boosters are guided through a DM flow for custom role names, role icons, role colors for double boosts, and emoji suggestions. Requests are sent to the admin channel for approval before roles or emojis are created. |
| [activity](docs/modules/activity.md) | Assigns roles based on counted message activity. Admins can configure message-count milestones, include/exclude channel filters, and a per-user cooldown to reduce spam farming.                                                                     |

### Core bot features:

- Per-server module enable/disable controls.
- Configurable admin channel for alerts and approval requests.
- Configurable admin role for privileged Amadeus actions.
- SQLite-backed persistent configuration and module state.
- Docker-friendly deployment with environment-selected cogs.

---

## Bot Invite

Create the bot application in the Discord Developer Portal, then replace `YOUR_CLIENT_ID` in this URL:

```text
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=1103001349142&scope=bot%20applications.commands
```

This invite includes the baseline permissions needed for all current modules:

- View Channels
- Send Messages
- Embed Links
- Attach Files
- Use Application Commands
- Manage Roles
- Manage Emojis and Stickers
- Manage Channels
- Manage Messages
- Moderate Members
- Kick Members
- Ban Members

You can remove permissions from the invite if you do not plan to enable the modules that use them.

---

## Required Permissions

### Bot (all servers)

| Permission | Reason |
|---|---|
| Send Messages | Sending responses and panel embeds |
| Use Application Commands | Slash command support |

### Module permissions

See each module's documentation for its specific permission requirements.

| Module | Permissions needed |
|---|---|
| [bouncer](docs/modules/bouncer.md) | Manage Roles, Kick Members, Manage Channel, Manage Messages |
| [honeypot](docs/modules/honeypot.md) | Manage Channel, Manage Messages, plus permissions for the chosen action |
| [boost](docs/modules/boost.md) | Manage Roles, Manage Emojis and Stickers |
| [activity](docs/modules/activity.md) | Manage Roles |

### Privileged Gateway Intents

| Intent | Reason |
|---|---|
| Server Members | Required for `/bouncer verify-all` (iterating all guild members) and guild join events |

---

## Docker Deployment

The Docker setup pulls the latest published image from GitHub Container Registry and runs the bot with a read-only container filesystem plus one writable bind mount for SQLite data.

Edit `docker-compose.yml` and set the service `environment:` values:

```yaml
environment:
  DISCORD_TOKEN: "your-bot-token"
  AMADEUS_OWNER_ID: "your-discord-user-id"
  AMADEUS_COGS: "cogs.bouncer,cogs.honeypot,cogs.boost,cogs.activity"
  AMADEUS_DB_PATH: "/app/data/amadeus.sqlite3"
```

`AMADEUS_COGS` controls which optional modules are loaded. Leave it blank to load no optional modules.

The compose file uses:

```yaml
image: ghcr.io/sciadv-community/amadeus-neo:latest
```

Before starting the container, create the data directory and make it writable by the container user:

```bash
sudo install -d -m 0770 -o 10001 -g 10001 /srv/amadeus-neo/data
```

The compose file maps `/srv/amadeus-neo/data` on the host to `/app/data` in the container. SQLite stores `amadeus.sqlite3` and its WAL/journal files there.

Pull and start the bot:

```bash
docker compose pull
docker compose up -d
```

If the package is private, log in on the server first with a GitHub token that has package read access:

```bash
docker login ghcr.io
```

---

## Tests

Unit tests run automatically in GitHub Actions on every push and pull request.

To run the same test suite locally:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

If you already have a virtual environment active, only run:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

The test suite is offline. It does not require a Discord token, a `.env` file, or a live Discord server.

---

## AI Notice
The creation of this bot leveraged both LLM as a validation layer for human-written code. They assisted in
- Scanning for security vulnerabilities
- Creation of Github workflows for automated testing
- Writing documentation against an established format
- Cleaning up and consolidating duplicate code within cogs

**A human thoroughly reviewed any code contributed by a model prior to commit.**
