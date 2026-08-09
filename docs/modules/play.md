# Module: play

Creates Visual Novel playthrough posts in configured Discord forum channels. Members run `/play new`, choose a configured game and optional spoiler tags in a modal, create a forum post in that game's forum, apply selected spoiler tags, ping the member in the starter post, and then mark the thread as a Spoiler Channel. Replay posts also apply the matching game tag automatically.

## Enable / Disable

```
/amadeus module enable play
/amadeus module disable play
```

## Setup

1. `/amadeus play set-forum <forum>` - optionally set the default forum used when adding games.
2. `/amadeus play add-game <name> [forum] [tag_name] [order]` - add a game to `/play new` and bind it to a forum channel.
3. `/amadeus play remove-forum` - remove a configured playthrough forum after reviewing the affected games.
4. `/amadeus play config` - verify the forums, game count, archive duration, and bot permissions.

`add-game` links an existing forum tag by name. If the tag does not exist, the bot creates it in the selected forum channel. If `forum` is omitted, the default forum from `/amadeus play set-forum` is used. If `tag_name` is omitted, the bot uses the game name as the tag name. If `order` is omitted, the game is added to the end of the `/play new` Visual Novel dropdown. Discord forum tag names are limited to 20 characters, so longer game names need an explicit shorter `tag_name`.

`remove-forum` lists only configured playthrough forums that currently have games assigned. The first confirmation removes those games from Amadeus play configuration. The bot then asks **ARE YOU SURE?** before attempting to delete the selected Discord forum channel.

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
- Manage Messages
- Read Message History

`Manage Channels` is required because Spoiler Channel is currently exposed by Discord as a channel flag. discord.py does not expose that setting as a public `ForumChannel.create_thread` or `Thread.edit` parameter yet, so the module uses discord.py's HTTP client to patch the created thread's flags.

`Read Message History` is used by the monthly archived-post lock sweep. If that permission is missing, members can still create playthrough posts, but the bot will skip archived locking for that forum and log a warning.

`Manage Messages` is used only for the `Delete Message` and `Pin Message` context menu actions.

Members must also have **View Channels** and **Send Messages in Threads** in the configured forum. `/play new` will not create a post in a forum the member cannot access.

## Member Commands

| Command | Description |
|---|---|
| `/play new` | Open a modal to create a personal playthrough post |
| `/play delete` | Open a modal to permanently delete one of your active playthrough posts |
| `/play end` | Archive and lock one of your active playthrough posts |
| `/play unlock` | Open a modal to reopen one of your locked or archived playthrough posts |

The `/play new` modal lists configured games, a replay selector, and configured spoiler tags from play forums. First playthroughs can apply up to five selected spoiler tags. Replays apply the selected game's tag automatically and can apply up to four additional spoiler tags. Discord select menus support at most 25 options.

`/play delete` scans active threads for configured playthrough posts named for your Discord username and opens a modal select. Each option includes the post's last post date. Submitting permanently deletes the selected post.

`/play end` first checks the current channel. If it is one of your unarchived configured playthrough posts, the bot opens a modal with that post selected. Otherwise, the bot scans active threads for configured playthrough posts named for your Discord username and opens a modal select. Each option includes the post's last post date. Submitting archives and locks the selected post.

`/play unlock` scans configured playthrough forums for locked or archived posts named for your Discord username and opens a modal select. Each option includes the post's last post date. Submitting unlocks and unarchives the selected post. If an active post for the same game already exists, the bot asks you to archive that active post before unlocking another.

## Message Context Menus

Discord message context commands cannot be registered for only some channels, so these commands appear globally and enforce the playthrough-post rules when used:

| Command | Description |
|---|---|
| `Delete Message` | Open a modal to confirm and delete a message from one of your unlocked playthrough posts |
| `Pin Message` | Open a modal to confirm and pin a message in one of your unlocked playthrough posts |

The selected message must be inside a configured playthrough forum post, and the post name must end with the command-runner's current Discord username in the `<game> | @username` format. Nicknames are not used for ownership checks. `Delete Message` and `Pin Message` show the message quote and confirmation question as display-only modal components; modal submit confirms, and closing the modal cancels.

## Admin Commands

| Command | Description |
|---|---|
| `/amadeus play set-forum <forum>` | Set the default forum channel used when adding games |
| `/amadeus play remove-forum` | Remove games for a configured forum, then confirm Discord forum deletion |
| `/amadeus play add-game <name> [forum] [tag_name] [order]` | Add or update a game and bind it to a forum channel, tag, and dropdown order |
| `/amadeus play set-order <game> <order>` | Move a game to a specific `/play new` dropdown position |
| `/amadeus play remove-game <game>` | Remove a game from `/play new` |
| `/amadeus play list-games` | List configured games with order numbers and tag bindings |
| `/amadeus play archive-duration <minutes>` | Set the auto-archive duration used for new playthrough posts |
| `/amadeus play auto-archive <configured-channel> [grace-days]` | Run the archived post lock sweep now for one configured forum |
| `/amadeus play config` | Show current configuration and permission status |

`/amadeus play` commands require Amadeus admin access and the `play` module must be enabled on the server.

## How It Works

1. Member runs `/play new` and opens the New Playthrough modal.
2. The member chooses a configured game, replay state, and spoiler tags. First playthroughs can select up to five tags; replays reserve one tag slot for the selected game's tag and can select up to four additional tags.
3. The bot resolves the game's configured forum and tag.
4. The bot checks only active guild threads in that forum. If active post names match the selected game and the member's Discord username, the bot shows an ephemeral confirmation with **No** and **Yes** buttons asking whether to archive the matching post or posts.
5. If no duplicate exists, or the member clicks **Yes**, the bot creates a forum post named `<game> | @username`, using the member's Discord username rather than their server nickname.
6. The starter post says `<@user> | Spoilers for <game>` plus any selected spoiler tags, which pings the member before spoiler gating is applied.
7. The bot patches the created thread with Discord's `IS_SPOILER_CHANNEL` flag.
8. The bot posts the playthrough guidance message inside the thread.

The duplicate check uses active threads only. It matches the selected game, forum, and current Discord username from the post name, not applied spoiler tags. If multiple active posts for the same game are found, the bot lists them and the **Yes** button archives all of them before creating the new post. The module does not store player sessions in SQLite, and archived threads are not queried during normal `/play new` usage.

## Archived Post Locking

Discord automatically archives playthrough posts after the configured inactive duration, usually 7 days. Archived posts that are not locked can be reopened by posting in them, so the play module performs a monthly maintenance sweep.

The sweep:

1. Reads configured playthrough forums only.
2. Lists archived forum posts from newest to oldest.
3. Considers only posts named like `<game> | @username` in a configured playthrough forum.
4. Skips posts whose last message is newer than `AMADEUS_PLAY_ARCHIVED_LOCK_GRACE_DAYS`.
5. Locks eligible archived posts with `archived=True` and `locked=True`.
6. Writes a per-forum checkpoint under `AMADEUS_CACHE_DIR/<guild_id>/play_lock_sweeps/<forum_id>.json`.

The default grace period is `14` days, which gives members another week beyond a 7-day auto-archive window before the bot locks the post. The checkpoint is a cache file, not server configuration; deleting it only causes the next sweep to rescan the recent sweep window.

Admins can run `/amadeus play auto-archive <configured-channel> [grace-days]` to trigger the same lock sweep for one configured playthrough forum immediately. The optional `grace-days` argument only affects that manual run.

## Database

| Table | Stores |
|---|---|
| `play_config` | Per-guild default forum channel and optional auto-archive duration |
| `play_game` | Per-guild game list, forum channel bindings, and forum tag bindings |

## Troubleshooting

**`/play new` says the game is missing its forum tag**
The configured tag was deleted or renamed. Re-run `/amadeus play add-game <name>` so the bot can link or recreate the tag.

**`/play new` says the game does not have a valid playthrough forum**
The configured forum was deleted, changed to another channel type, or the game was added before a forum was assigned. Re-run `/amadeus play add-game <name> <forum>`.

**A playthrough post was created and then archived immediately**
Discord rejected the Spoiler Channel flag update. Check that the bot has Manage Channels and Manage Threads in that game's configured forum channel.

**A member can create multiple posts for the same game after an old one archived**
This is expected. The duplicate check intentionally looks only at active threads so archived forum history does not become an ever-growing lookup path.

**Archived playthrough posts can still be reopened shortly after auto-archive**
This is expected during the grace period. By default, the monthly lock sweep skips posts whose last message is less than 14 days old.
