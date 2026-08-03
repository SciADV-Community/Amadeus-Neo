# Module: activity

Assigns roles based on message activity. Members earn roles when their counted message total crosses configured thresholds. A per-user cooldown limits how often messages are counted to discourage spam.

**Requires Manage Roles permission.** The bot role must sit above every activity role it assigns.

## Enable / Disable

```
/amadeus module enable activity
/amadeus module disable activity
```

## Setup

1. `/amadeus activity tier-add <threshold> <role>` — add at least one milestone.
2. *(Optional)* `/amadeus activity channel-include` or `/amadeus activity channel-exclude` — filter which channels count.
3. *(Optional)* `/amadeus activity cooldown` — adjust the per-user cooldown (default 5s).
4. *(Optional)* `/amadeus activity role-swap true` — keep only the highest earned activity role.

## Configurable Settings

| Command | Range | Default | Description |
|---|---|---|---|
| `/amadeus activity cooldown` | 1–3600s | 5 | Seconds between counted messages per user |
| `/amadeus activity role-swap` | `true` / `false` | `false` | Whether earning a higher tier removes older activity tier roles |

## Member Commands

| Command | Description |
|---|---|
| `/activity status` | Show your counted messages, current activity role, and next role |
| `/activity leaderboard` | Show the top 10 members by counted messages |

## Tier Commands

| Command | Description |
|---|---|
| `/amadeus activity tier-add <threshold> <role>` | Add or update a milestone |
| `/amadeus activity tier-remove <threshold>` | Remove the tier at a threshold |
| `/amadeus activity tier-list` | List all configured tiers |
| `/amadeus activity role-swap <enabled>` | When enabled, members keep only their highest earned activity role |

## Channel Filtering

By default all channels count. You can filter with include or exclude lists:

| Command | Description |
|---|---|
| `/amadeus activity channel-include <channel>` | Whitelist — only included channels count |
| `/amadeus activity channel-exclude <channel>` | Blacklist — all channels except these count |
| `/amadeus activity channel-remove <channel>` | Remove from whichever list it's on |
| `/amadeus activity channel-list` | Show current filter config |

If any channel is on the include list, the include list takes priority and the exclude list is ignored.

## Admin Commands

| Command | Description |
|---|---|
| `/amadeus activity status <member>` | Show message count, tier progress, and next milestone |

`/amadeus activity` commands require Amadeus admin access. `/activity status` and `/activity leaderboard` are available to members when the activity module is enabled.

## Database

| Table | Stores |
|---|---|
| `activity_config` | Per-guild cooldown and role-swap settings |
| `activity_channels` | Per-guild channel include/exclude list |
| `activity_tiers` | Message count threshold → role mappings |
| `activity_counts` | Per-member counted message totals |

## Troubleshooting

**Role wasn't assigned when a member hit the threshold**
Check the bot's role hierarchy — the bot role must be above the tier role. Verify with `/amadeus activity tier-list` that the role still exists. Use `/amadeus activity status` to confirm the member's count.

**Old tier roles are not being removed**
Enable role swapping with `/amadeus activity role-swap true`. The bot must have Manage Roles and sit above every activity role it adds or removes.

**A member passed a threshold before the tier was configured**
`/amadeus activity status` will show a ⚠️ for tiers where the count is sufficient but the role wasn't assigned. The role will not be auto-assigned retroactively — use the Discord role panel to assign it manually, or the member will receive it naturally on their next counted message if they're still above the threshold.

**Members are farming messages to hit thresholds**
Increase the cooldown with `/amadeus activity cooldown`, or use `/amadeus activity channel-include` to restrict counting to specific channels.
