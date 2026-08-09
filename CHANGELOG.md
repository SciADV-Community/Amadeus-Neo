# Changelog

---
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
