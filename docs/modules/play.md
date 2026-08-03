# Module: play

Creates Visual Novel playthrough posts in a configured Discord forum channel. Members choose a configured game with `/play`; the bot creates a forum post, applies the matching game tag, pings the member in the starter post, and then marks the thread as a Spoiler Channel.

## Enable / Disable

```
/amadeus module enable play
/amadeus module disable play
```

## Setup

1. `/amadeus play set-forum <forum>` - set the forum channel where playthrough posts are created.
2. `/amadeus play add-game <name> [tag_name]` - add a game to `/play`.
3. `/amadeus play config` - verify the forum, game count, archive duration, and bot permissions.

`add-game` links an existing forum tag by name. If the tag does not exist, the bot creates it in the configured forum channel. If `tag_name` is omitted, the bot uses the game name as the tag name.

## Required Permissions

The bot needs these permissions in the configured forum channel:

- View Channels
- Send Messages
- Create Public Threads
- Send Messages in Threads
- Manage Threads
- Manage Channels

`Manage Channels` is required because Spoiler Channel is currently exposed by Discord as a channel flag. discord.py does not expose that setting as a public `ForumChannel.create_thread` or `Thread.edit` parameter yet, so the module uses discord.py's HTTP client to patch the created thread's flags.

## Member Commands

| Command | Description |
|---|---|
| `/play <game> [replay]` | Create a personal playthrough post for a configured game |

The `game` option autocompletes from the server's configured games.

## Admin Commands

| Command | Description |
|---|---|
| `/amadeus play set-forum <forum>` | Set the forum channel used for playthrough posts |
| `/amadeus play add-game <name> [tag_name]` | Add or update a game and bind it to a forum tag |
| `/amadeus play remove-game <game>` | Remove a game from `/play` |
| `/amadeus play list-games` | List configured games and tag bindings |
| `/amadeus play archive-duration <minutes>` | Set the auto-archive duration used for new playthrough posts |
| `/amadeus play config` | Show current configuration and permission status |

`/amadeus play` commands require Amadeus admin access. They can be used before the module is enabled so admins can configure the module first.

## How It Works

1. Member runs `/play` and picks a configured game.
2. The bot checks only active guild threads and filters them to the configured forum and selected game tag. If the member already has an active matching post, the bot returns that thread link instead of creating another.
3. The bot creates a forum post named `<game> | @username`.
4. The starter post says `<@user> | Spoilers for <game>`, which pings the member before spoiler gating is applied.
5. The bot patches the created thread with Discord's `IS_SPOILER_CHANNEL` flag.
6. The bot posts the playthrough guidance message inside the thread.

The module does not store player sessions in SQLite. Discord forum threads are the source of truth. Archived threads are not queried during normal `/play` usage.

## Database

| Table | Stores |
|---|---|
| `play_config` | Per-guild forum channel and optional auto-archive duration |
| `play_game` | Per-guild game list and forum tag bindings |

## Troubleshooting

**`/play` says the game is missing its forum tag**
The configured tag was deleted or renamed. Re-run `/amadeus play add-game <name>` so the bot can link or recreate the tag.

**A playthrough post was created and then archived immediately**
Discord rejected the Spoiler Channel flag update. Check that the bot has Manage Channels and Manage Threads in the configured forum channel.

**A member can create multiple posts for the same game after an old one archived**
This is expected. The duplicate check intentionally looks only at active threads so archived forum history does not become an ever-growing lookup path.
