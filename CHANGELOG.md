# Changelog

---
### 2.0.0
- Activity now keeps an all-time count and a separate count for each UTC month
- Added `/activity monthly status` and `/activity monthly leaderboard`
- Activity leaderboards now show the top 30 people, 10 at a time
  - They're ephemeral and have buttons to switch pages
- `/amadeus activity tier-add` has a `monthly` option for monthly roles
  - Monthly roles swap over at midnight UTC on the first of the month
  - If the bot was offline at midnight, it'll catch up when it starts again
- Existing activity counts and tiers get migrated to all-time automatically
  - The migration code is temporary and can be removed after everything has upgraded
- The same role can't be added to more than one activity tier, including between all-time and monthly
- Removing or replacing an activity tier removes the old role from everyone before cleaning up the database entry
  - If a role change fails, it'll send the failure to the configured alert channel
- Boost setup now uses buttons and modals instead of asking everything one message at a time
  - One boost gets a custom role with an icon and one emoji
  - Two boosts also get a custom role color and a second emoji
  - Finished buttons turn green instead of sending extra ephemeral messages
- The final boost review DM only shows the role and emoji names, with **Submit for Review** and **Start Over**
- Boost approval image previews now try to stay stacked vertically
  - The previews and their attachments are removed once the approval or denial actually goes through
  - Denials show the reason in the final admin channel message
- Fixed `/boost status` sending a dead setup panel after a forced request was denied
- Boost approvals don't remove the buttons or previews until the decision actually goes through now
  - If Discord says no to a role or emoji change, the request stays up so someone can try it again
  - This also stops two admins from processing the same request at the same time
- Rerunning the boost flow for someone who already has perks won't forget about their old role and emojis anymore
  - The new set gets finished before the old set is removed
  - If the old stuff can't be deleted, Amadeus keeps track of it and tries again later
- Boost grants are all-or-nothing now
  - If the role icon, role assignment, or an emoji fails, it'll clean up anything it already made instead of calling it successful
  - Failed removals stay in the database instead of leaving mystery roles and emojis behind
- Before sending the boost setup DM, Amadeus checks that the server has room for the role and every emoji
  - It also checks that the server can use role icons
  - If it can't start, it'll tell the admin channel who was affected so someone can reach out to them
  - It checks everything again at approval time in case the server filled up while they were doing the forms
- If a boost setup or confirmation DM fails, it'll alert the admin channel and leave the flow in a state `/boost status` can recover
- Denial reasons are included in the member's DM now, not just the admin channel
- Enabling the boost module checks that the admin channel can view, send, embed, and attach files
  - Failed approval posts also try to leave a plain error in the admin channel with the affected user
- Role icons are limited to Discord's actual 256 KB limit instead of letting a larger upload fail later
- Boost modal uploads require `discord.py>=2.7,<3`
- Monthly activity rollover no longer quietly ignores a role that was deleted in Discord
  - It'll alert the admin channel and keep trying the rollover until the tier is fixed or removed
  - It'll also fetch a qualifying member if they aren't in the member cache instead of just skipping them
- Activity tier roles are unique in the database now, not just checked by the command
- Database upgrades check what is actually missing instead of assuming every SQLite error means "the column already exists"
- Server data is deleted 90 days after Amadeus leaves the server
  - If Amadeus rejoins before then, the deletion gets cancelled and the old config is kept
- Server owners are ignored by every honeypot action
  - Discord wouldn't let the bot ban the owner anyway, and trying the other actions wasn't useful either
- Added `/amadeus health`
  - It checks the database, bot latency, enabled modules, admin role and channel, channel permissions, and any boost cleanup that's still waiting

### 1.8.2
- Added "Unpin Message" feature

### 1.8.1
- `/play unlock` could time out if it scans before responding to discord
- `/play unlock` has an ephemeral picker if it takes to long (the popup only works if the bot responds in under 3 seconds)
- `/play unlock` check was very inefficient with the database; it just does a lookup once and keeps it now.
- `/play unlock` checks would cry about timing out if there was an issue. It now gives something more useful
- `/play unlock` fallback for channel lookup no longer uses tags
- `/play end` also had a tag check. I think I had planned to do something with this and roles early on
- `/play end` will now attempt to set the current thread as default in the modal popup, if it belongs to the initiator
- `/play new` will give an ephemeral picker before the popup if there's more than one playthrough forum
   - One playthrough forum will still go straight to the picker
- `/play new` no longer shows the tag for the game you pick. If you pick replay, it will apply it automatically
    - It will also call you out directly if you try to forge it
- `/play new` now only takes 4 tags to make sure there's no issue with replay's 5th tag
- `/play new` logic caps forums at 20 games configured max (this is admin stuff, max 20 tags in a forum)
- `/play new` triple checks tags belong to the forum chosen; previously there was a bug where it'd try to show everything
- Duplicate playthrough checks once when prompting and then again after confirmation, preventing you from side-channeling an unlock in another channel
- new/end/delete/unlock has a limit of 25 entries shown; it will communicate to you if you somehow have more
- new/end/delete/unlock now properly sort channels by last post
- Added a check for some config failures related to the admin role deletion protection
- Removed some old code related to the right-click unlock function
- Removed some old code related to spoiler selection (this was screwing up the ordering)
- Automatic play cleanup now starts when the cog (module) loads
- Admin config check actually confirms all permissions on the forum channel when run


### 1.8.0
- Major overhaul of the `/play` module, adding modals and a new UI.

### 1.7.6
- Added a monthly archived playthrough lock sweep with a 14-day default grace period.
- Added `/amadeus play auto-archive` to manually run the archived lock sweep for a configured forum.
- Added `AMADEUS_CACHE_DIR` and `AMADEUS_PLAY_ARCHIVED_LOCK_GRACE_DAYS`.

### 1.7.5
- Changed `/play` forum post names to use Discord usernames instead of server nicknames.
- Changed active duplicate handling to offer archiving the existing playthrough before continuing setup.

### 1.7.4
- Fixed `/amadeus play` admin commands bypassing the server's module-enabled check.
- Fixed `/amadeus play set-forum` incorrectly warning that forum posts require Create Public Threads.

### 1.7.3
- Fixed `/play` creating duplicate playthroughs if a nickname changes.
  - Duplicate check also checks the player ID in the starting post.
- Fixed active thread lookup failures triggering "no existing post". It will now just fail, which is preferable.
- Fixed race condition for `/play` when concurrent instances run for the same game, bypassing duplicate check.
- Fixed missed spoiler tag matches when Discord returned applied tags as raw IDs.
- Fixed the additional spoiler tag picker leaving live buttons behind after it expired.
  - The prompt now reports that setup timed out.
- Fixed an issue where people could use `/play` to make posts in forums they cannot see or post in.

### 1.7.2
- Added an ephemeral additional spoiler tag picker to `/play`.

### 1.7.1
- Added per-game forum channel mappings for `/play`, allowing multiple playthrough forums per server.

### 1.7.0
- Added the optional `play` module for Visual Novel playthrough forum posts.
- Added `/play` for members and `/amadeus play` configuration commands for admins.
- Added Discord Spoiler Channel support for created forum posts via the raw channel flags API.

---
### v1.6.0
- Moved module admin/config commands under `/amadeus activity`, `/amadeus boost`, `/amadeus bouncer`, and `/amadeus honeypot`.
- Kept member-facing commands at `/activity status`, `/activity leaderboard`, `/boost status`, `/verify`, and `/code`.

---
### v1.5.0
- Added custom message for honeypot post
- Honeypot post will now attempt to update the previous post if it exists

### v1.4.0
- Added public `/activity status` and `/activity leaderboard` commands.
- Added `/activity tier role-swap` to keep only a member's highest earned activity role.

### v1.3.0
- Added optional honeypot message history cleanup windows for `/honeypot set-action`.
- The bouncer verification panel now includes configured Terms of Service and Privacy Policy links, plus a moderator assistance note.
- Added optional `AMADEUS_PRIVACY_POLICY_URL` and `AMADEUS_TERMS_OF_SERVICE_URL` startup sync for the bot application's bio.

### v1.2.1
- Reusing the bouncer Start Verification button now resends the active CAPTCHA image instead of only asking for the code.
- 100% test coverage for the bouncer verification panel.

### v1.2.0
- `/honeypot enable-alerts` will no longer warn you to set a channel if one is already set.
- Honeypot moderation actions no longer apply to members with the configured Amadeus admin role.
- Honeypot bans now skip the configured `AMADEUS_OWNER_ID` user.

### v1.1.1
- Added additional unit tests for multiple modules.

### v1.1.0
- Added support for `reason` in `/honeypot set-action` when using `mute`, `kick`, or `ban`

### v1.0.1
- Resolved honeypot race condition
