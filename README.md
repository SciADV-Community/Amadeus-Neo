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
