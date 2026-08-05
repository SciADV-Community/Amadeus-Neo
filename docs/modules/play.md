# Module: play

Creates Visual Novel playthrough posts in configured Discord forum channels. Members choose a configured game with `/play`; the bot asks whether to include additional spoiler tags, creates a forum post in that game's forum, applies the matching game tag plus selected extra tags, pings the member in the starter post, and then marks the thread as a Spoiler Channel.

## Enable / Disable

```
/amadeus module enable play
/amadeus module disable play
```

## Setup

1. `/amadeus play set-forum <forum>` - optionally set the default forum used when adding games.
2. `/amadeus play add-game <name> [forum] [tag_name]` - add a game to `/play` and bind it to a forum channel.
3. `/amadeus play config` - verify the forums, game count, archive duration, and bot permissions.

`add-game` links an existing forum tag by name. If the tag does not exist, the bot creates it in the selected forum channel. If `forum` is omitted, the default forum from `/amadeus play set-forum` is used. If `tag_name` is omitted, the bot uses the game name as the tag name. Discord forum tag names are limited to 20 characters, so longer game names need an explicit shorter `tag_name`.

Example layout:

| Forum channel | Games |
|---|---|
| `Chaos-Head` | `Chaos;Head NoAH`, `Chaos;Child` |
| `Steins-Gate` | `Steins;Gate`, `Steins;Gate 0`, `Steins;Gate: My Darling's Embrace`, `Steins;Gate Re:Boot` |
| `Robotics-Notes` | `Robotics;Notes`, `Robotics;Notes DaSH` |
| `Anonymous-Code` | `Anonymous;Code` |

## Required Permissions

The bot needs these permissions in every configured playthrough forum channel:

- View Channels
- Send Messages / Create Posts
- Send Messages in Threads
- Manage Threads
- Manage Channels

`Manage Channels` is required because Spoiler Channel is currently exposed by Discord as a channel flag. discord.py does not expose that setting as a public `ForumChannel.create_thread` or `Thread.edit` parameter yet, so the module uses discord.py's HTTP client to patch the created thread's flags.

Members must also have **View Channels** and **Send Messages in Threads** in the configured forum. `/play` will not create a post in a forum the member cannot access.

## Member Commands

| Command | Description |
|---|---|
| `/play <game> [replay]` | Create a personal playthrough post for a configured game |

The `game` option autocompletes from the server's configured games. After the command is submitted, the bot shows an ephemeral multi-select for additional spoiler tags from that game's forum. The tag that directly matches the selected game is applied automatically and is not selectable.

## Admin Commands

| Command | Description |
|---|---|
| `/amadeus play set-forum <forum>` | Set the default forum channel used when adding games |
| `/amadeus play add-game <name> [forum] [tag_name]` | Add or update a game and bind it to a forum channel and tag |
| `/amadeus play remove-game <game>` | Remove a game from `/play` |
| `/amadeus play list-games` | List configured games and tag bindings |
| `/amadeus play archive-duration <minutes>` | Set the auto-archive duration used for new playthrough posts |
| `/amadeus play config` | Show current configuration and permission status |

`/amadeus play` commands require Amadeus admin access and the `play` module must be enabled on the server.

## How It Works

1. Member runs `/play` and picks a configured game.
2. The bot resolves the game's configured forum and tag.
3. The bot checks only active guild threads and filters them to that forum and selected game tag. If the member already has an active matching post, the bot returns that thread link instead of creating another.
4. The bot asks, "Would you like to include any additional spoilers in this channel?" and shows a multi-select of the forum's other tags.
5. The member picks up to four additional tags, or skips the select and clicks **Create**.
6. The bot creates a forum post named `<game> | @username`.
7. The starter post says `<@user> | Spoilers for <game>` plus any selected spoiler tags, which pings the member before spoiler gating is applied.
8. The bot patches the created thread with Discord's `IS_SPOILER_CHANNEL` flag.
9. The bot posts the playthrough guidance message inside the thread.

The duplicate check uses active threads only. It matches the selected game, forum, and player ID from the starter post, so nickname changes do not create duplicate active posts. The module does not store player sessions in SQLite, and archived threads are not queried during normal `/play` usage.

## Database

| Table | Stores |
|---|---|
| `play_config` | Per-guild default forum channel and optional auto-archive duration |
| `play_game` | Per-guild game list, forum channel bindings, and forum tag bindings |

## Troubleshooting

**`/play` says the game is missing its forum tag**
The configured tag was deleted or renamed. Re-run `/amadeus play add-game <name>` so the bot can link or recreate the tag.

**`/play` says the game does not have a valid playthrough forum**
The configured forum was deleted, changed to another channel type, or the game was added before a forum was assigned. Re-run `/amadeus play add-game <name> <forum>`.

**A playthrough post was created and then archived immediately**
Discord rejected the Spoiler Channel flag update. Check that the bot has Manage Channels and Manage Threads in that game's configured forum channel.

**A member can create multiple posts for the same game after an old one archived**
This is expected. The duplicate check intentionally looks only at active threads so archived forum history does not become an ever-growing lookup path.
