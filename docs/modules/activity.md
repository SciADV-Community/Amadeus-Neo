# Module: activity

Tracks both all-time and UTC calendar-month message activity. Members earn roles when their counted message total crosses configured all-time or monthly thresholds. A per-user cooldown limits how often messages are counted to discourage spam.

**Requires Manage Roles permission.** The bot role must sit above every activity role it assigns.

## Enable / Disable

```
/amadeus module enable activity
/amadeus module disable activity
```

## Setup

1. `/amadeus activity tier-add <threshold> <role>` — add at least one all-time milestone.
   Use `monthly: true` to make it a monthly milestone instead.
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
| `/activity status` | Show your all-time count, current activity role, and next role |
| `/activity leaderboard` | Privately show the top 30 all-time members, 10 per page |
| `/activity monthly status` | Show your count and tier progress for the current UTC month |
| `/activity monthly leaderboard` | Privately show the top 30 members for the current UTC month, 10 per page |

Leaderboard responses are ephemeral, so only the member who runs the command can see or paginate them. Use the **Previous** and **Next** buttons to move between pages.

## Tier Commands

| Command | Description |
|---|---|
| `/amadeus activity tier-add <threshold> <role> [monthly]` | Add or update an all-time tier; set `monthly: true` for a monthly tier |
| `/amadeus activity tier-remove <threshold> [monthly]` | Remove an all-time tier; set `monthly: true` to remove a monthly tier |
| `/amadeus activity tier-list` | List all configured tiers, grouped by all-time and monthly scope |
| `/amadeus activity role-swap <enabled>` | When enabled, members keep only their highest earned activity role |

Each activity tier must use a unique Discord role, including across all-time and monthly tiers. When a tier is removed or its role is replaced, Amadeus first removes the old role from its members. If any removal fails, the existing database entry is kept and an alert is sent so the cleanup can be retried safely.

Monthly counts use calendar months in UTC. At 00:00 UTC on the first day of each month, monthly tier roles are reconciled from the completed month's final counts: roles are removed from the prior holders and assigned to the members who qualified during the month that just ended. Winners keep those roles until the next monthly rollover. The same reconciliation runs after startup if the bot was offline at midnight. All-time counts and roles are unaffected.

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

`/amadeus activity` commands require Amadeus admin access. `/activity status`, `/activity leaderboard`, and the `/activity monthly` commands are available to members when the activity module is enabled.

## Database

| Table | Stores |
|---|---|
| `activity_config` | Per-guild cooldown and role-swap settings |
| `activity_channels` | Per-guild channel include/exclude list |
| `activity_tiers` | Scoped all-time/monthly message threshold → role mappings |
| `activity_counts` | Per-member totals in the permanent `all_time` period and retained `YYYY-MM` UTC periods |

On first startup after this update, legacy tier and count rows are migrated to the `all_time` scope automatically.

## Troubleshooting

**Role wasn't assigned when a member hit the threshold**
For an all-time tier, check the bot's role hierarchy and verify with `/amadeus activity tier-list` that the role still exists. Use `/amadeus activity status <member>` to inspect all-time progress. Monthly roles are not assigned immediately when the threshold is reached: `/activity monthly status` shows current progress, and qualifying roles are assigned when the month ends.

A configured role can go missing if someone deletes it directly in Discord after the tier was saved. This leaves a valid database reference pointing to an object Discord no longer has. Monthly rollover reports this to the admin channel and does not mark the month complete, so it can retry after the tier is replaced or removed.

**Old tier roles are not being removed**
Enable role swapping with `/amadeus activity role-swap true`. This setting applies independently within both all-time and monthly tiers. The bot must have Manage Roles and sit above every activity role it adds or removes.

**A member passed a threshold before the tier was configured**
For an all-time tier, the member will receive the role on their next counted message if they remain above the threshold. A monthly tier uses the completed month's final count and is assigned during the next monthly rollover.

**Members are farming messages to hit thresholds**
Increase the cooldown with `/amadeus activity cooldown`, or use `/amadeus activity channel-include` to restrict counting to specific channels.
