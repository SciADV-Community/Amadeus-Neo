# Module: honeypot

Trap channel for catching bots and compromised accounts. Any message posted in the configured channel is immediately deleted and a moderation action is taken against the sender. An optional alert is sent to the admin channel on each trigger.

## Enable / Disable

```
/amadeus module enable honeypot
/amadeus module disable honeypot
```

## Setup

1. `/honeypot set-channel <channel>` — designates the trap channel. The bot checks for **Manage Messages** and **Manage Channel** permissions and reports any that are missing. A 1-minute slow-mode is applied automatically if permissions allow.
2. `/honeypot set-action <action> [role] [reason] [delete-history]` — choose what happens when someone posts. `reason` is written to the audit log for `mute`, `kick`, and `ban`. `delete-history` can remove the member's recent messages from the last 1, 6, 12, or 24 hours.
3. `/honeypot enable-alerts <true|false>` — enable alerts to the admin channel (requires `/amadeus set-admin-channel` to be configured).
4. `/honeypot post` — posts a visible warning embed in the honeypot channel.

> For the remove-role action, the bot role must sit **above** the target role in the server's role hierarchy.

> Members with the configured Amadeus admin role are exempt from honeypot moderation actions. The `AMADEUS_OWNER_ID` user is also protected from honeypot bans.

> The bot's own messages in the honeypot channel are not deleted — this allows `/honeypot post` to display the warning embed.

## Configurable Settings

| Command | Options | Description |
|---|---|---|
| `/honeypot set-channel` | Any text channel | The channel that triggers the action |
| `/honeypot set-action` | `remove-role`, `mute`, `kick`, `ban`; optional `reason` for `mute`/`kick`/`ban`; optional `delete-history` of 1, 6, 12, or 24 hours | Action taken on the sender |
| `/honeypot enable-alerts` | `true` / `false` | Whether to alert the admin channel on each trigger |

**Actions:**

| Action | Effect | Permission Required |
|---|---|---|
| `remove-role` | Removes a specified role from the member | Manage Roles |
| `mute` | Applies a 28-day Discord timeout | Moderate Members |
| `kick` | Removes the member from the server | Kick Members |
| `ban` | Permanently bans the member | Ban Members |

When `delete-history` is configured, successful `remove-role`, `mute`, and `kick` actions manually delete matching messages from text channels the bot can access. `ban` uses Discord's native ban message deletion window.

## Database

| Table | Stores |
|---|---|
| `honeypot_config` | Per-guild trap channel, configured action, action reason, message deletion window, and alert setting |

## Troubleshooting

**Action fails silently**
Check the bot's role position. For `remove-role`, the bot role must be above the target role. For `mute`/`kick`/`ban`, the bot role must be above the member's highest role.

**Message history is not being deleted**
For `remove-role`, `mute`, and `kick`, confirm the bot has **Manage Messages** and can view each channel where messages should be removed. For `ban`, Discord handles the configured deletion window as part of the ban request.

**No alerts are arriving**
Confirm `/amadeus set-admin-channel` is set and the bot has Send Messages permission in that channel. Verify alerts are enabled with `/honeypot enable-alerts true`.

**Slow-mode wasn't applied / messages aren't being deleted**
Re-run `/honeypot set-channel` — it will report exactly which permissions are missing. Grant them and run the command again.
