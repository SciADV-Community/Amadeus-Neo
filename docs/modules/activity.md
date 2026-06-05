# Module: activity

Assigns roles based on message activity. Members earn roles when their counted message total crosses configured thresholds. A per-user cooldown limits how often messages are counted to discourage spam.

**Requires Manage Roles permission.** The bot role must sit above every activity role it assigns.

## Enable / Disable

```
/amadeus module enable activity
/amadeus module disable activity
```

## Setup

1. `/activity tier add <threshold> <role>` — add at least one milestone.
2. *(Optional)* `/activity channel include/exclude` — filter which channels count.
3. *(Optional)* `/activity settings cooldown` — adjust the per-user cooldown (default 5s).

## Configurable Settings

| Command | Range | Default | Description |
|---|---|---|---|
| `/activity settings cooldown` | 1–3600s | 5 | Seconds between counted messages per user |

## Tier Commands

| Command | Description |
|---|---|
| `/activity tier add <threshold> <role>` | Add or update a milestone |
| `/activity tier remove <threshold>` | Remove the tier at a threshold |
| `/activity tier list` | List all configured tiers |

## Channel Filtering

By default all channels count. You can filter with include or exclude lists:

| Command | Description |
|---|---|
| `/activity channel include <channel>` | Whitelist — only included channels count |
| `/activity channel exclude <channel>` | Blacklist — all channels except these count |
| `/activity channel remove <channel>` | Remove from whichever list it's on |
| `/activity channel list` | Show current filter config |

If any channel is on the include list, the include list takes priority and the exclude list is ignored.

## Admin Commands

| Command | Description |
|---|---|
| `/activity admin status <member>` | Show message count, tier progress, and next milestone |

## Database

| Table | Stores |
|---|---|
| `activity_config` | Per-guild cooldown setting |
| `activity_channels` | Per-guild channel include/exclude list |
| `activity_tiers` | Message count threshold → role mappings |
| `activity_counts` | Per-member counted message totals |

## Troubleshooting

**Role wasn't assigned when a member hit the threshold**
Check the bot's role hierarchy — the bot role must be above the tier role. Verify with `/activity tier list` that the role still exists. Use `/activity admin status` to confirm the member's count.

**A member passed a threshold before the tier was configured**
`/activity admin status` will show a ⚠️ for tiers where the count is sufficient but the role wasn't assigned. The role will not be auto-assigned retroactively — use the Discord role panel to assign it manually, or the member will receive it naturally on their next counted message if they're still above the threshold.

**Members are farming messages to hit thresholds**
Increase the cooldown with `/activity settings cooldown`, or use `/activity channel include` to restrict counting to specific channels.
